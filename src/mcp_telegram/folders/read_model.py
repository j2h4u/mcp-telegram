"""Public read-side API for Telegram folder placement snapshots."""

from __future__ import annotations

from .read_repository import FolderReadConnection
from .read_repository import dialog_placement as _dialog_placement
from .read_repository import folder_snapshot as _folder_snapshot
from .read_repository import folders_by_dialog as _folders_by_dialog
from .read_repository import list_folder_messages as _list_folder_messages
from .read_repository import list_folders as _list_folders


def list_folders(conn: FolderReadConnection) -> list[dict[str, object]]:
    return _list_folders(conn)


def list_folder_messages(conn: FolderReadConnection, folder_id: int, limit: int) -> dict[str, object]:
    return _list_folder_messages(conn, folder_id, limit)


def folder_snapshot(
    conn: FolderReadConnection,
    *,
    stale_after_seconds: int,
    now: int | None = None,
) -> dict[str, object]:
    return _folder_snapshot(conn, stale_after_seconds=stale_after_seconds, now=now)


def folders_by_dialog(conn: FolderReadConnection) -> dict[int, list[dict[str, object]]]:
    return _folders_by_dialog(conn)


def dialog_placement(conn: FolderReadConnection, dialog_id: int) -> dict[str, object]:
    return _dialog_placement(conn, dialog_id)
