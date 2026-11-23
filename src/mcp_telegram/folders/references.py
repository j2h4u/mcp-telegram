"""Private, account-scoped references supplied by native folder rules."""

from __future__ import annotations

import json
import sqlite3
from typing import cast

from ..auth_scope import TelegramAuthScope
from ..dialog_identity import retained_dialog_metadata_ids
from ..entity_profile.contracts import ChannelReference, GroupReference, UserReference
from .contracts import FolderPeerReference, FolderRule


def retain_folder_references(  # noqa: PLR0913, PLR0917 - observation boundary is explicit
    conn: sqlite3.Connection,
    account_id: int,
    order: int,
    rule: FolderRule,
    references: tuple[FolderPeerReference, ...],
    scope: TelegramAuthScope | None,
) -> None:
    """Keep IDs and capabilities at the accepted rule's writer boundary."""
    capability: dict[int, tuple[str, int | None]] = {}
    if scope is not None and scope.account_id == account_id:
        for ref in references:
            if isinstance(ref, UserReference):
                capability[ref.user_id] = ("user", ref.access_hash)
            elif isinstance(ref, ChannelReference):
                capability[ref.channel_id] = ("channel", ref.access_hash)
            else:
                capability[ref.group_id] = ("group", None)
    serialized_scope = None if scope is None else json.dumps(scope.as_private_mapping(), sort_keys=True)
    peer_ids = {*rule.included_ids, *rule.pinned_ids, *rule.excluded_ids}
    retained = retained_dialog_metadata_ids(conn, peer_ids)
    for peer_id in peer_ids:
        kind, access_hash = capability.get(peer_id, ("unknown", None))
        conn.execute(
            "INSERT INTO folder_peer_references(account_id,dialog_id,kind,access_hash,auth_scope,observation_order,metadata_state) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(account_id,dialog_id) DO UPDATE SET "
            "metadata_state=CASE WHEN metadata_state IN ('unresolvable','denied') AND excluded.kind!='unknown' "
            "AND (kind IS NOT excluded.kind OR access_hash IS NOT excluded.access_hash "
            "OR auth_scope IS NOT excluded.auth_scope) THEN 'pending' ELSE metadata_state END, "
            "metadata_observed_at=CASE WHEN metadata_state IN ('unresolvable','denied') AND excluded.kind!='unknown' "
            "AND (kind IS NOT excluded.kind OR access_hash IS NOT excluded.access_hash OR auth_scope IS NOT excluded.auth_scope) "
            "THEN NULL ELSE metadata_observed_at END, "
            "metadata_source=CASE WHEN metadata_state IN ('unresolvable','denied') AND excluded.kind!='unknown' "
            "AND (kind IS NOT excluded.kind OR access_hash IS NOT excluded.access_hash OR auth_scope IS NOT excluded.auth_scope) "
            "THEN NULL ELSE metadata_source END, "
            "metadata_error_code=CASE WHEN metadata_state IN ('unresolvable','denied') AND excluded.kind!='unknown' "
            "AND (kind IS NOT excluded.kind OR access_hash IS NOT excluded.access_hash OR auth_scope IS NOT excluded.auth_scope) "
            "THEN NULL ELSE metadata_error_code END, "
            "metadata_candidate=CASE WHEN excluded.kind!='unknown' AND "
            "(kind IS NOT excluded.kind OR access_hash IS NOT excluded.access_hash OR auth_scope IS NOT excluded.auth_scope) "
            "THEN 'native' ELSE metadata_candidate END, "
            "kind=CASE WHEN excluded.kind='unknown' THEN kind ELSE excluded.kind END, "
            "access_hash=CASE WHEN excluded.kind='unknown' THEN access_hash ELSE excluded.access_hash END, "
            "auth_scope=CASE WHEN excluded.kind='unknown' THEN auth_scope ELSE excluded.auth_scope END, "
            "capability_revision=capability_revision+CASE WHEN excluded.kind!='unknown' AND "
            "(kind IS NOT excluded.kind OR access_hash IS NOT excluded.access_hash OR auth_scope IS NOT excluded.auth_scope) "
            "THEN 1 ELSE 0 END, observation_order=MAX(observation_order,excluded.observation_order) "
            "WHERE observation_order<=excluded.observation_order",
            (
                account_id,
                peer_id,
                kind,
                access_hash,
                serialized_scope if kind != "unknown" else None,
                order,
                "retained" if peer_id in retained else "pending",
            ),
        )


def read_folder_reference(
    conn: sqlite3.Connection, dialog_id: int, scope: TelegramAuthScope | None
) -> FolderPeerReference | None:
    if scope is None:
        return None
    row = cast(
        tuple[str, int | None] | None,
        conn.execute(
            "SELECT kind,access_hash FROM folder_peer_references WHERE account_id=? AND dialog_id=? AND auth_scope=? "
            "AND metadata_state!='unresolvable' AND metadata_candidate='native'",
            (scope.account_id, dialog_id, json.dumps(scope.as_private_mapping(), sort_keys=True)),
        ).fetchone(),
    )
    if row is None:
        return None
    kind, access_hash = row
    if kind == "user" and access_hash is not None:
        return UserReference(dialog_id, access_hash)
    if kind == "channel" and access_hash is not None:
        return ChannelReference(dialog_id, access_hash)
    if kind == "group":
        return GroupReference(dialog_id)
    return None
