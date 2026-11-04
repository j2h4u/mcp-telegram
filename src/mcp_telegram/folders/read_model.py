"""Public read-side API for Telegram folder placement snapshots."""

from __future__ import annotations

from .read_repository import FolderReadConnection
from .read_repository import dialog_placement as _dialog_placement
from .read_repository import folder_snapshot as _folder_snapshot
from .read_repository import folder_summaries as _folder_summaries
from .read_repository import folders_by_dialog as _folders_by_dialog


def folder_summaries(conn: FolderReadConnection, *, account_id: int | None) -> list[dict[str, object]]:
    return _folder_summaries(conn, account_id=account_id)


def folder_snapshot(
    conn: FolderReadConnection,
    *,
    stale_after_seconds: int,
    account_id: int | None,
    now: int | None = None,
) -> dict[str, object]:
    return _folder_snapshot(conn, stale_after_seconds=stale_after_seconds, account_id=account_id, now=now)


def folders_by_dialog(conn: FolderReadConnection, *, account_id: int | None) -> dict[int, list[dict[str, object]]]:
    return _folders_by_dialog(conn, account_id=account_id)


def dialog_placement(conn: FolderReadConnection, dialog_id: int, *, account_id: int | None) -> dict[str, object]:
    return _dialog_placement(conn, dialog_id, account_id=account_id)
