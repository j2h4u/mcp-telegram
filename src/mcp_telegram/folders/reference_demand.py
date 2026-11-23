"""Bounded directory metadata acquisition for retained folder peers."""

from __future__ import annotations

import sqlite3
import time
from typing import Protocol, cast

from telethon import utils  # type: ignore[import-untyped]
from telethon.errors import ChatAdminRequiredError, PeerIdInvalidError, RPCError  # type: ignore[import-untyped]
from telethon.tl import types  # type: ignore[import-untyped]
from telethon.tl.functions.messages import GetPeerDialogsRequest  # type: ignore[import-untyped]
from telethon.tl.types import TypeInputDialogPeer, TypeInputPeer  # type: ignore[import-untyped]

from ..auth_scope import TelegramAuthScope
from ..dialog_identity import publish_supplied_identities
from ..dialog_targeted_metadata import capture_dialog_revision, publish_targeted_categories, publish_targeted_dialog
from ..entity_profile.contracts import ChannelReference, GroupReference, UserReference
from ..image_authorization import ScopeToken
from ..observation_order import allocate_observation_order
from ..peer_access import PeerAccessRejectedError
from ..sqlite_writer import SQLiteWriter
from ..sync_transactions import write_transaction
from ..telegram_access import ACCESS_LOST_ERRORS
from ..telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    RpcAttemptBudget,
    acquisition_context,
    durable_demand_context,
)
from ..telegram_demand_progress import SliceOutcome, SliceOutcomeKind, WorkKey, WorkRevision
from ..telegram_rpc_consumers import DemandKind, TelegramRpcSource
from ..telegram_rpc_scheduler import rpc_attempt_budget, rpc_scope
from ..telethon_dialog import observe_dialog_identity
from .references import read_folder_reference
from .sqlite_repository import SQLiteFolderSnapshotRepository


class _Session(Protocol):
    def get_input_entity(self, peer: int) -> object: ...


class _Client(Protocol):
    session: _Session

    def retained_auth_scope(self) -> TelegramAuthScope | None: ...
    def capture_image_scope(self) -> ScopeToken | None: ...
    def claim_image_scope(self, token: ScopeToken) -> bool: ...
    async def __call__(self, request: object) -> object: ...


class FolderPeerMetadataDemandAdapter:
    """One peer and at most one physical request per durable slice."""

    demand_kind = DemandKind.FOLDER_PEER_METADATA

    def __init__(self, client: object, conn: sqlite3.Connection, writer: SQLiteWriter, account_id: int) -> None:
        self._client = cast(_Client, client)
        self._conn = conn
        self._writer = writer
        self._account_id = account_id

    def status(self, now: float, *, excluded: frozenset[WorkRevision] = frozenset()) -> DemandStatus | None:
        del now
        rows = cast(
            list[tuple[int, int]],
            self._conn.execute(
                "SELECT dialog_id,capability_revision FROM folder_peer_references "
                "WHERE account_id=? AND metadata_state='pending' AND NOT EXISTS "
                "(SELECT 1 FROM synced_dialogs s WHERE s.dialog_id=folder_peer_references.dialog_id AND s.status='access_lost') "
                "ORDER BY dialog_id",
                (self._account_id,),
            ).fetchall(),
        )
        for dialog_id, order in rows:
            selected = WorkRevision(WorkKey(self.demand_kind, self._account_id, str(dialog_id)), str(order))
            if selected not in excluded:
                return DemandStatus(0.0, selected=selected)
        return None

    async def run_slice(self, budget: RpcAttemptBudget) -> SliceOutcome:
        selected = budget.selected
        if selected is None:
            raise ValueError("folder peer demand requires a selection")
        dialog_id = int(selected.key.target)
        scope = self._client.retained_auth_scope()
        token = self._client.capture_image_scope()
        if scope is None or scope.account_id != self._account_id or token is None or token.scope != scope:
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        row = cast(
            tuple[str] | None,
            self._conn.execute(
                "SELECT metadata_candidate FROM folder_peer_references WHERE account_id=? AND dialog_id=? "
                "AND capability_revision=? AND metadata_state='pending'",
                (self._account_id, dialog_id, int(selected.revision)),
            ).fetchone(),
        )
        if row is None:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        return await self._run_selected(selected, token, scope, str(row[0]), budget)

    async def _run_selected(
        self,
        selected: WorkRevision,
        token: ScopeToken,
        scope: TelegramAuthScope,
        candidate: str,
        budget: RpcAttemptBudget,
    ) -> SliceOutcome:
        dialog_id = int(selected.key.target)
        peer = self._resolve_peer(dialog_id, scope, candidate)
        baseline_revision = capture_dialog_revision(self._conn, dialog_id)
        observed_at = int(time.time())
        outcome: (
            tuple[str, tuple[object, ...], int | None, object | None, tuple[object, ...], str | None] | SliceOutcomeKind
        )
        if peer is None:
            outcome = ("unresolvable", (), None, None, (), "local_reference_missing")
            source = "local_resolution"
        else:
            outcome = await self._acquire(peer, dialog_id, budget)
            if isinstance(outcome, SliceOutcomeKind):
                return SliceOutcome(selected, outcome)
            source = "telegram_get_peer_dialogs"
            if outcome[5] == "PeerIdInvalidError" and candidate == "native":
                fallback = self._session_peer(dialog_id)
                if fallback is not None and fallback != peer:
                    advanced = await self._writer.run(lambda conn: self._advance_candidate(conn, selected, token))
                    return (
                        SliceOutcome(selected, SliceOutcomeKind.PROGRESSED, str(int(selected.revision) + 1))
                        if advanced
                        else SliceOutcome(selected, SliceOutcomeKind.STALE)
                    )
        completed_at = int(time.time())
        accepted = await self._writer.run(
            lambda conn: self._finish(
                conn,
                selected,
                token,
                *outcome[:5],
                observed_at,
                baseline_revision,
                completed_at,
                source,
                outcome[5],
            )
        )
        return (
            SliceOutcome(selected, SliceOutcomeKind.TERMINAL, "settled")
            if accepted
            else SliceOutcome(selected, SliceOutcomeKind.STALE)
        )

    def _resolve_peer(self, dialog_id: int, scope: TelegramAuthScope, candidate: str) -> object | None:
        retained = _input_peer(read_folder_reference(self._conn, dialog_id, scope)) if candidate == "native" else None
        if retained is not None:
            return retained
        return self._session_peer(dialog_id)

    def _session_peer(self, dialog_id: int) -> object | None:
        try:
            peer = self._client.session.get_input_entity(dialog_id)
        except AttributeError, KeyError, TypeError, ValueError:
            peer = None
        if (
            not isinstance(peer, (types.InputPeerUser, types.InputPeerChannel, types.InputPeerChat))
            or utils.get_peer_id(peer) != dialog_id
        ):
            peer = None
        return peer

    def _advance_candidate(self, conn: sqlite3.Connection, selected: WorkRevision, token: ScopeToken) -> bool:
        with write_transaction(conn):
            if not self._client.claim_image_scope(token):
                return False
            cursor = conn.execute(
                "UPDATE folder_peer_references SET metadata_candidate='session',capability_revision=capability_revision+1, "
                "metadata_observed_at=?,metadata_source='telegram_get_peer_dialogs',metadata_error_code='PeerIdInvalidError' "
                "WHERE account_id=? AND dialog_id=? AND capability_revision=? AND metadata_state='pending' AND metadata_candidate='native'",
                (int(time.time()), self._account_id, int(selected.key.target), int(selected.revision)),
            )
            if not self._client.claim_image_scope(token):
                raise RuntimeError("folder peer acquisition scope revoked")
            return cursor.rowcount == 1

    async def _acquire(
        self, peer: object, dialog_id: int, budget: RpcAttemptBudget
    ) -> tuple[str, tuple[object, ...], int | None, object | None, tuple[object, ...], str | None] | SliceOutcomeKind:
        order = await self._writer.run(allocate_observation_order)
        try:
            with durable_demand_context(self.demand_kind), acquisition_context(AcquisitionKind.ENTITY_LOOKUP):
                with rpc_attempt_budget(budget), rpc_scope(TelegramRpcSource.DIALOG_SYNC):
                    peers: list[TypeInputDialogPeer] = [types.InputDialogPeer(cast(TypeInputPeer, peer))]
                    response = await self._client(GetPeerDialogsRequest(peers))
        except (*ACCESS_LOST_ERRORS, ChatAdminRequiredError) as exc:
            return "denied", (), None, None, (), type(exc).__name__
        except PeerIdInvalidError:
            return "unresolvable", (), None, None, (), "PeerIdInvalidError"
        except PeerAccessRejectedError:
            return SliceOutcomeKind.RETRYABLE_FAILURE
        except RPCError:
            return SliceOutcomeKind.RETRYABLE_FAILURE
        if not isinstance(response, types.messages.PeerDialogs):
            return SliceOutcomeKind.RETRYABLE_FAILURE
        entities = tuple(response.users) + tuple(response.chats)
        returned = next(
            (
                dialog
                for dialog in response.dialogs
                if isinstance(dialog, types.Dialog) and utils.get_peer_id(dialog.peer) == dialog_id
            ),
            None,
        )
        return "returned" if returned else "omitted", entities, order, returned, tuple(response.messages), None

    def _finish(  # noqa: PLR0913, PLR0917
        self,
        conn: sqlite3.Connection,
        selected: WorkRevision,
        token: ScopeToken,
        state: str,
        entities: tuple[object, ...],
        order: int | None,
        dialog: object | None,
        messages: tuple[object, ...],
        observed_at: int,
        baseline_revision: int | None,
        completed_at: int,
        source: str,
        error_code: str | None,
    ) -> bool:
        with write_transaction(conn):
            if not self._client.claim_image_scope(token):
                return False
            dialog_id = int(selected.key.target)
            cursor = conn.execute(
                "UPDATE folder_peer_references SET metadata_state=?,metadata_observed_at=?,metadata_source=?,metadata_error_code=? WHERE account_id=? AND dialog_id=? "
                "AND capability_revision=? AND metadata_state='pending'",
                (state, completed_at, source, error_code, self._account_id, dialog_id, int(selected.revision)),
            )
            if cursor.rowcount != 1:
                return False
            if order is not None:
                if dialog is not None:
                    publish_targeted_dialog(
                        conn,
                        dialog_id,
                        dialog,
                        messages,
                        observed_at=observed_at,
                        order=order,
                        baseline_revision=baseline_revision,
                    )
                facts = tuple(
                    fact
                    for entity in entities
                    if isinstance(entity, (types.User, types.Chat, types.Channel))
                    if (
                        fact := observe_dialog_identity(
                            entity, dialog_id=utils.get_peer_id(entity), source="profile", observed_at=observed_at
                        )
                    )
                    is not None
                )
                publish_supplied_identities(conn, facts, observation_order=order)
                publish_targeted_categories(conn, entities, observed_at, order)
                SQLiteFolderSnapshotRepository(conn).reproject_current_rules_in_transaction(now=observed_at)
            if not self._client.claim_image_scope(token):
                raise RuntimeError("folder peer acquisition scope revoked")
            return True


def _input_peer(reference: object) -> object | None:
    if isinstance(reference, UserReference):
        return types.InputPeerUser(reference.user_id, reference.access_hash)
    if isinstance(reference, ChannelReference):
        return types.InputPeerChannel(-reference.channel_id - 1000000000000, reference.access_hash)
    if isinstance(reference, GroupReference):
        return types.InputPeerChat(-reference.group_id)
    return None
