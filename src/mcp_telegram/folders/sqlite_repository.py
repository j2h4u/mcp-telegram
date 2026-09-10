"""SQLite write adapter for the local Telegram folder snapshot."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable
from typing import cast

from .contracts import (
    DialogCategory,
    DialogFacts,
    FolderDialogCursor,
    FolderRule,
    FolderSourceSnapshot,
    FolderStagingSnapshot,
    FolderStagingStaleError,
)
from .ports import FolderSnapshotRepository

_STAGING_KEY = "folder_snapshot_staging_v1"
logger = logging.getLogger(__name__)


def _state_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def replace_folder_snapshot(
    conn: sqlite3.Connection,
    folders: Iterable[tuple[int, str]],
    memberships: Iterable[tuple[int, int]],
) -> None:
    """Replace folder rows for low-level test and maintenance fixtures."""
    with conn:
        conn.execute("DELETE FROM telegram_folder_members")
        conn.execute("DELETE FROM telegram_folders")
        conn.executemany("INSERT INTO telegram_folders(folder_id, title) VALUES (?, ?)", list(folders))
        conn.executemany("INSERT INTO telegram_folder_members(folder_id, dialog_id) VALUES (?, ?)", list(memberships))


class SQLiteFolderSnapshotRepository(FolderSnapshotRepository):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._staging_recovery_pending = False

    @property
    def staging_recovery_pending(self) -> bool:
        """Whether a malformed staging row was discarded since the last read."""
        return self._staging_recovery_pending

    def acknowledge_staging_recovery(self) -> None:
        """Allow the worker to consume the immediate retry requested by recovery."""
        self._staging_recovery_pending = False

    def replace_snapshot(
        self,
        snapshot: FolderSourceSnapshot,
        memberships: tuple[tuple[int, int], ...],
        *,
        completed_at: int,
        expected_generation: int | None = None,
    ) -> int:
        """Replace tables and success metadata in one SQLite transaction."""
        with self._conn:
            previous = cast(  # pyright: ignore[reportAny]
                tuple[object] | None,
                self._conn.execute(
                    "SELECT value FROM daemon_state WHERE key = 'folder_snapshot_generation'"
                ).fetchone(),
            )
            try:
                current_generation = int(cast(int | str, previous[0])) if previous is not None else 0
            except TypeError, ValueError:
                current_generation = 0
            if expected_generation is not None and current_generation != expected_generation:
                raise FolderStagingStaleError("folder staging generation is stale")
            generation = current_generation + 1
            self._conn.execute("DELETE FROM telegram_folder_members")
            self._conn.execute("DELETE FROM telegram_folders")
            self._conn.executemany(
                "INSERT INTO telegram_folders(folder_id, title) VALUES (?, ?)",
                ((folder.folder_id, folder.title) for folder in snapshot.folders),
            )
            self._conn.executemany(
                "INSERT INTO telegram_folder_members(folder_id, dialog_id) VALUES (?, ?)", memberships
            )
            _set_state_values(
                self._conn,
                {
                    "folder_snapshot_generation": generation,
                    "folder_snapshot_last_attempt_at": completed_at,
                    "folder_snapshot_last_success_at": completed_at,
                    "folder_snapshot_last_outcome": "success",
                    "folder_snapshot_next_retry_at": None,
                    "folder_snapshot_consecutive_failures": 0,
                },
            )
            self._conn.execute("DELETE FROM daemon_state WHERE key = ?", (_STAGING_KEY,))
        self._staging_recovery_pending = False
        return generation

    def record_attempt(
        self,
        *,
        attempted_at: int,
        outcome: str,
        next_retry_at: int | None,
        consecutive_failures: int,
    ) -> None:
        """Persist failure metadata without touching the last complete snapshot."""
        with self._conn:
            _set_state_values(
                self._conn,
                {
                    "folder_snapshot_last_attempt_at": attempted_at,
                    "folder_snapshot_last_outcome": outcome,
                    "folder_snapshot_next_retry_at": next_retry_at,
                    "folder_snapshot_consecutive_failures": consecutive_failures,
                },
            )

    def read_consecutive_failures(self) -> int:
        return _state_int(self._read_state_value("folder_snapshot_consecutive_failures")) or 0

    def read_generation(self) -> int | None:
        return _state_int(self._read_state_value("folder_snapshot_generation"))

    def read_last_outcome(self) -> str | None:
        return self._read_state_value("folder_snapshot_last_outcome")

    def read_last_success_at(self) -> int | None:
        return _state_int(self._read_state_value("folder_snapshot_last_success_at"))

    def read_next_retry_at(self) -> int | None:
        return _state_int(self._read_state_value("folder_snapshot_next_retry_at"))

    def read_staging(self) -> FolderStagingSnapshot | None:
        raw = self._read_state_value(_STAGING_KEY)
        if raw is None:
            return None
        try:
            decoded = cast(object, json.loads(raw))
            if not isinstance(decoded, dict):
                raise ValueError("staging payload must be an object")
            payload = cast(dict[str, object], decoded)
            if payload.get("version") != 1:
                raise ValueError("unsupported folder staging version")
            folders = tuple(_decode_folder(item) for item in cast(list[object], payload["folders"]))
            dialogs = tuple(_decode_dialog(item) for item in cast(list[object], payload["dialogs"]))
            raw_cursor = payload.get("cursor")
            cursor = None if raw_cursor is None else _decode_cursor(raw_cursor)
            started_at = int(cast(int | str, payload["started_at"]))
            raw_generation = payload.get("base_generation")
            base_generation = None if raw_generation is None else int(cast(int | str, raw_generation))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError) as exc:
            self._discard_staging_after_corruption(raw)
            self._staging_recovery_pending = True
            logger.warning("folder_snapshot staging corrupt; discarded and restarting acquisition: %s", exc)
            return None
        return FolderStagingSnapshot(folders, dialogs, cursor, started_at, base_generation)

    def save_staging(self, snapshot: FolderStagingSnapshot) -> None:
        payload = {
            "version": 1,
            "started_at": snapshot.started_at,
            "folders": [_encode_folder(folder) for folder in snapshot.folders],
            "dialogs": [_encode_dialog(dialog) for dialog in snapshot.dialogs],
            "cursor": None if snapshot.cursor is None else _encode_cursor(snapshot.cursor),
            "base_generation": snapshot.base_generation,
        }
        with self._conn:
            _set_state_values(self._conn, {_STAGING_KEY: json.dumps(payload, separators=(",", ":"))})

    def clear_staging(self) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM daemon_state WHERE key = ?", (_STAGING_KEY,))

    def _discard_staging_after_corruption(self, raw: str) -> None:
        """Delete only malformed staging state, preserving the published snapshot."""
        with self._conn:
            self._conn.execute(
                "DELETE FROM daemon_state WHERE key = ? AND value = ?",
                (_STAGING_KEY, raw),
            )

    def _read_state_value(self, key: str) -> str | None:
        row = cast(
            tuple[object] | None,
            self._conn.execute("SELECT value FROM daemon_state WHERE key = ?", (key,)).fetchone(),
        )
        return None if row is None or row[0] is None else str(row[0])


def _set_state_values(conn: sqlite3.Connection, values: dict[str, object | None]) -> None:
    conn.executemany(
        "INSERT INTO daemon_state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ((key, None if value is None else str(value)) for key, value in values.items()),
    )


def _encode_folder(folder: FolderRule) -> dict[str, object]:
    return {
        "folder_id": folder.folder_id,
        "title": folder.title,
        "included_ids": sorted(folder.included_ids),
        "pinned_ids": sorted(folder.pinned_ids),
        "excluded_ids": sorted(folder.excluded_ids),
        "categories": sorted(category.value for category in folder.categories),
        "exclude_archived": folder.exclude_archived,
        "exclude_read": folder.exclude_read,
        "exclude_muted": folder.exclude_muted,
        "explicit_only": folder.explicit_only,
    }


def _decode_folder(raw: object) -> FolderRule:
    value = cast(dict[str, object], raw)
    return FolderRule(
        folder_id=int(cast(int | str, value["folder_id"])),
        title=str(value["title"]),
        included_ids=frozenset(int(item) for item in cast(list[int | str], value["included_ids"])),
        pinned_ids=frozenset(int(item) for item in cast(list[int | str], value["pinned_ids"])),
        excluded_ids=frozenset(int(item) for item in cast(list[int | str], value["excluded_ids"])),
        categories=frozenset(DialogCategory(str(item)) for item in cast(list[object], value["categories"])),
        exclude_archived=bool(value["exclude_archived"]),
        exclude_read=bool(value["exclude_read"]),
        exclude_muted=bool(value["exclude_muted"]),
        explicit_only=bool(value["explicit_only"]),
    )


def _encode_dialog(dialog: DialogFacts) -> dict[str, object]:
    return {
        "dialog_id": dialog.dialog_id,
        "category": dialog.category.value,
        "archived": dialog.archived,
        "unread": dialog.unread,
        "muted": dialog.muted,
    }


def _decode_dialog(raw: object) -> DialogFacts:
    value = cast(dict[str, object], raw)
    return DialogFacts(
        dialog_id=int(cast(int | str, value["dialog_id"])),
        category=DialogCategory(str(value["category"])),
        archived=bool(value["archived"]),
        unread=bool(value["unread"]),
        muted=bool(value["muted"]),
    )


def _encode_cursor(cursor: FolderDialogCursor) -> dict[str, object]:
    return {
        "offset_date": cursor.offset_date,
        "offset_id": cursor.offset_id,
        "offset_peer_type": cursor.offset_peer_type,
        "offset_peer_id": cursor.offset_peer_id,
        "offset_peer_access_hash": cursor.offset_peer_access_hash,
    }


def _decode_cursor(raw: object) -> FolderDialogCursor:
    value = cast(dict[str, object], raw)
    offset_date = value["offset_date"]
    offset_peer_type = value["offset_peer_type"]
    return FolderDialogCursor(
        offset_date=None if offset_date is None else str(offset_date),
        offset_id=int(cast(int | str, value["offset_id"])),
        offset_peer_type=None if offset_peer_type is None else str(offset_peer_type),
        offset_peer_id=int(cast(int | str, value["offset_peer_id"])),
        offset_peer_access_hash=int(cast(int | str, value["offset_peer_access_hash"])),
    )
