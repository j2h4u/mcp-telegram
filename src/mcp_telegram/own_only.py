"""Classification substrate for the account's own-message coverage.

This module deliberately does not enroll dialogs or change message storage.  It
provides the reusable decision and the local candidate query that future sync
and read paths can consume.  Telegram entity fields are treated as untrusted
input; only the presence of creator/admin rights is used for ownership.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from .models import DialogType


class OwnOnlyBasis(StrEnum):
    """Machine-readable reason a dialog belongs to the own-only surface."""

    DIRECT_MESSAGE = "direct_message"
    PERSONAL_CHANNEL = "personal_channel"
    OWNED_CHANNEL = "owned_channel"
    PERSONAL_CHANNEL_DISCUSSION = "personal_channel_discussion"


@dataclass(frozen=True, slots=True)
class OwnOnlyClassification:
    """Decision and provenance for one dialog."""

    included: bool
    basis: tuple[OwnOnlyBasis, ...]

    @property
    def inclusion_basis(self) -> tuple[str, ...]:
        """Stable string values suitable for structured payloads and SQL adapters."""
        return tuple(item.value for item in self.basis)


@dataclass(frozen=True, slots=True)
class OwnOnlyContext:
    """Account facts needed by the classifier.

    ``personal_channel_id`` and ``linked_chat_id`` use Telegram's canonical
    peer-id form (``-100...``).  The classifier also accepts a positive raw
    channel id and normalizes it for callers that obtained it from UserFull.
    """

    account_id: int
    personal_channel_id: int | None = None
    personal_channel_linked_chat_id: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", int(self.account_id))
        object.__setattr__(self, "personal_channel_id", _peer_id(self.personal_channel_id))
        object.__setattr__(self, "personal_channel_linked_chat_id", _peer_id(self.personal_channel_linked_chat_id))


def _peer_id(value: int | None) -> int | None:
    """Normalize a raw channel id without requiring a Telethon entity."""
    if value is None:
        return None
    value = int(value)
    return value if value <= 0 else -1000000000000 - value


def _has_admin_rights(entity: object) -> bool:
    """Return true when Telegram says the account can post in the channel."""
    if bool(getattr(entity, "creator", False)):
        return True
    admin_rights = getattr(entity, "admin_rights", None)
    return bool(getattr(admin_rights, "post_messages", False))


def classify_own_only_dialog(
    *,
    dialog_id: int,
    dialog_type: str | DialogType,
    entity: object | None,
    context: OwnOnlyContext,
) -> OwnOnlyClassification:
    """Classify a dialog using account ownership and personal-channel facts.

    Direct user/bot dialogs are included because outgoing messages in a DM are
    account-owned.  Broadcast channels are included only when they are the
    account's personal channel or Telegram reports creator/admin rights.  A
    linked discussion group is included only when it is linked to that personal
    channel, avoiding the old all-channel linked-group expansion.
    """
    dialog_id = int(dialog_id)
    kind = DialogType.parse(dialog_type)
    basis: list[OwnOnlyBasis] = []

    if kind in (DialogType.USER, DialogType.BOT) and dialog_id != context.account_id:
        basis.append(OwnOnlyBasis.DIRECT_MESSAGE)

    if kind is DialogType.CHANNEL:
        if dialog_id == context.personal_channel_id:
            basis.append(OwnOnlyBasis.PERSONAL_CHANNEL)
        elif entity is not None and _has_admin_rights(entity):
            basis.append(OwnOnlyBasis.OWNED_CHANNEL)

    if (
        kind in (DialogType.SUPERGROUP, DialogType.FORUM)
        and context.personal_channel_linked_chat_id is not None
        and dialog_id == context.personal_channel_linked_chat_id
    ):
        basis.append(OwnOnlyBasis.PERSONAL_CHANNEL_DISCUSSION)

    return OwnOnlyClassification(included=bool(basis), basis=tuple(basis))


_OWN_ONLY_CANDIDATE_SQL = """
SELECT dialog_id, name, type, linked_chat_id, last_message_at
FROM dialogs
WHERE (
      type IN ('user', 'bot')
      OR type = 'channel'
      OR dialog_id IN (
          SELECT linked_chat_id
          FROM dialogs
          WHERE dialog_id = ? AND linked_chat_id IS NOT NULL
      )
  )
ORDER BY dialog_id
"""

_OWN_ONLY_DIALOGS_DDL = """
CREATE TABLE IF NOT EXISTS own_only_dialogs (
    dialog_id       INTEGER PRIMARY KEY,
    inclusion_basis TEXT NOT NULL,
    updated_at      INTEGER NOT NULL
)
"""


def _as_int(value: object) -> int:
    return int(cast(int | str, value))


def ensure_own_only_schema(conn: sqlite3.Connection) -> None:
    """Create the ownership cache used by scheduled reconciliation and reads."""
    conn.execute(_OWN_ONLY_DIALOGS_DDL)
    conn.commit()


def enroll_own_only_dialog(
    conn: sqlite3.Connection,
    dialog_id: int,
    classification: OwnOnlyClassification,
    *,
    now: int | None = None,
) -> None:
    """Persist an own-only classification without downgrading sync coverage."""
    if not classification.included:
        return
    timestamp = int(time.time()) if now is None else int(now)
    ensure_own_only_schema(conn)
    with conn:
        conn.execute(
            """
            INSERT INTO own_only_dialogs (dialog_id, inclusion_basis, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(dialog_id) DO UPDATE SET
                inclusion_basis = excluded.inclusion_basis,
                updated_at = excluded.updated_at
            """,
            (int(dialog_id), json.dumps(list(classification.inclusion_basis), separators=(",", ":")), timestamp),
        )
        conn.execute(
            "INSERT OR IGNORE INTO synced_dialogs (dialog_id, status) VALUES (?, 'own_only')",
            (int(dialog_id),),
        )


def own_only_basis_by_dialog(conn: sqlite3.Connection) -> dict[int, tuple[str, ...]]:
    """Return persisted ownership bases, tolerating pre-cache test databases."""
    try:
        rows = cast(
            list[tuple[object, object]],
            conn.execute("SELECT dialog_id, inclusion_basis FROM own_only_dialogs").fetchall(),
        )
    except sqlite3.OperationalError:
        return {}
    result: dict[int, tuple[str, ...]] = {}
    for dialog_id, raw_basis in rows:
        try:
            basis = cast(object, json.loads(str(raw_basis)))
        except TypeError, ValueError:
            continue
        if isinstance(basis, list) and all(isinstance(item, str) for item in basis):
            result[_as_int(dialog_id)] = tuple(cast(list[str], basis))
    return result


def query_own_only_candidates(
    conn: sqlite3.Connection,
    *,
    personal_channel_id: int | None,
) -> list[dict[str, object]]:
    """Return local dialog candidates for subsequent entity classification.

    Ownership/admin rights are not persisted in ``dialogs`` and therefore are
    intentionally not guessed by SQL.  The caller must pass each candidate's
    Telegram entity to :func:`classify_own_only_dialog`.
    """
    personal_id = _peer_id(personal_channel_id)
    rows = cast(
        list[tuple[object, object, object, object, object]],
        conn.execute(_OWN_ONLY_CANDIDATE_SQL, (personal_id,)).fetchall(),
    )
    return [
        {
            "dialog_id": _as_int(row[0]),
            "name": cast(str | None, row[1]),
            "type": cast(str | None, row[2]),
            "linked_chat_id": cast(int | None, row[3]),
            "last_message_at": cast(int | None, row[4]),
        }
        for row in rows
    ]


__all__ = [
    "OwnOnlyBasis",
    "OwnOnlyClassification",
    "OwnOnlyContext",
    "classify_own_only_dialog",
    "enroll_own_only_dialog",
    "ensure_own_only_schema",
    "own_only_basis_by_dialog",
    "query_own_only_candidates",
]
