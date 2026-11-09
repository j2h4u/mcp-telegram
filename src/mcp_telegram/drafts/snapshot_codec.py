"""Private, validated Telegram draft snapshot data encoding."""

from __future__ import annotations

from copy import copy
from hashlib import sha256

from telethon.extensions import BinaryReader  # type: ignore[import-untyped]
from telethon.tl import types  # type: ignore[import-untyped]

from mcp_telegram.drafts.contracts import RestoreSnapshot
from mcp_telegram.rich_content import capture_rich_message

MAX_RESTORE_SNAPSHOT_BYTES = 1_048_576
SNAPSHOT_PREFIX = b"TGDR\x01"


class DraftSnapshotError(ValueError):
    """Content-free reason why a retained composition cannot be used."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def snapshot_fingerprint(draft: types.DraftMessage | types.DraftMessageEmpty) -> str:
    composition = copy(draft)
    composition.date = None
    return sha256(bytes(composition)).hexdigest()


def decode_restore_snapshot(snapshot: RestoreSnapshot) -> types.DraftMessage | types.DraftMessageEmpty:
    """Decode the one supported envelope and validate its exact content fingerprint."""
    try:
        payload = snapshot.payload
        if len(payload) > MAX_RESTORE_SNAPSHOT_BYTES or not payload.startswith(SNAPSHOT_PREFIX):
            raise ValueError("invalid envelope")
        raw = payload[len(SNAPSHOT_PREFIX) :]
        with BinaryReader(raw) as reader:
            draft = reader.tgread_object()
            if not isinstance(draft, (types.DraftMessage, types.DraftMessageEmpty)) or reader.tell_position() != len(
                raw
            ):
                raise ValueError("invalid draft")
        if snapshot_fingerprint(draft) != snapshot.semantic_fingerprint:
            raise ValueError("invalid fingerprint")
        return draft
    except Exception:  # noqa: BLE001 - do not leak retained text or capabilities through parser errors.
        raise DraftSnapshotError("invalid_restore_snapshot") from None


def snapshot_rich_message(snapshot: RestoreSnapshot | None) -> str | None:
    """Recover observed rich facts without exposing the private snapshot."""
    if snapshot is None:
        return None
    try:
        draft = decode_restore_snapshot(snapshot)
        return capture_rich_message(draft.rich_message if isinstance(draft, types.DraftMessage) else None)
    except DraftSnapshotError, TypeError, ValueError:
        return None
