"""Public read-side API for Telegram folder placement snapshots."""

from __future__ import annotations

from .sqlite_repository import FolderReadConnection
from .sqlite_repository import dialog_placement as _dialog_placement
from .sqlite_repository import folders_by_dialog as _folders_by_dialog
from .sqlite_repository import list_folder_messages as _list_folder_messages
from .sqlite_repository import list_folders as _list_folders


def list_folders(conn: FolderReadConnection) -> list[dict[str, object]]:
    return _list_folders(conn)


def list_folder_messages(conn: FolderReadConnection, folder_id: int, limit: int) -> dict[str, object]:
    return _list_folder_messages(conn, folder_id, limit)


def folders_by_dialog(conn: FolderReadConnection) -> dict[int, list[dict[str, object]]]:
    return _folders_by_dialog(conn)


def dialog_placement(conn: FolderReadConnection, dialog_id: int) -> dict[str, object]:
    return _dialog_placement(conn, dialog_id)
