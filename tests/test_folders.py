"""Custom Telegram folder snapshot and rule evaluation tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.tl.types import PeerChannel, PeerUser  # type: ignore[import-untyped]

from mcp_telegram.folder_store import (
    dialog_placement,
    folder_ids_by_dialog,
    list_folders,
    replace_folder_snapshot,
)
from mcp_telegram.folder_sync import _matches, refresh_folder_snapshot
from mcp_telegram.sync_db import ensure_sync_schema


def _connection(path: Path) -> sqlite3.Connection:
    ensure_sync_schema(path)
    return sqlite3.connect(path)


def test_snapshot_exposes_many_to_many_placement_and_archive_separately(tmp_path: Path) -> None:
    conn = _connection(tmp_path / "sync.db")
    try:
        conn.execute("INSERT INTO dialogs(dialog_id, archived) VALUES (10, 1)")
        conn.commit()
        replace_folder_snapshot(conn, [(1, "Work"), (2, "Unread")], [(1, 10), (2, 10)])

        assert list_folders(conn) == [{"id": 1, "title": "Work"}, {"id": 2, "title": "Unread"}]
        assert folder_ids_by_dialog(conn) == {10: [1, 2]}
        assert dialog_placement(conn, 10) == {
            "archived": True,
            "folders": [{"id": 1, "title": "Work"}, {"id": 2, "title": "Unread"}],
        }
    finally:
        conn.close()


def test_failed_snapshot_replacement_rolls_back_to_previous_snapshot(tmp_path: Path) -> None:
    conn = _connection(tmp_path / "sync.db")
    try:
        replace_folder_snapshot(conn, [(1, "Existing")], [(1, 10)])

        with pytest.raises(sqlite3.IntegrityError):
            replace_folder_snapshot(conn, [(2, "Duplicate"), (2, "Duplicate")], [])

        assert list_folders(conn) == [{"id": 1, "title": "Existing"}]
        assert folder_ids_by_dialog(conn) == {10: [1]}
    finally:
        conn.close()


class User:
    def __init__(self, *, contact: bool = False, bot: bool = False) -> None:
        self.contact = contact
        self.mutual_contact = False
        self.bot = bot


class Channel:
    megagroup = False


def _dialog(dialog_id: int, entity: object, *, archived: bool = False, unread: int = 0) -> object:
    return SimpleNamespace(
        id=dialog_id,
        entity=entity,
        archived=archived,
        unread_count=unread,
        unread_mentions_count=0,
        dialog=SimpleNamespace(notify_settings=None),
    )


def _filter_class(name: str, **values: object) -> object:
    defaults: dict[str, object] = {
        "id": 1,
        "title": "Folder",
        "include_peers": [],
        "pinned_peers": [],
        "exclude_peers": [],
        "contacts": False,
        "non_contacts": False,
        "bots": False,
        "groups": False,
        "broadcasts": False,
        "exclude_archived": False,
        "exclude_read": False,
        "exclude_muted": False,
    }
    defaults.update(values)
    return type(name, (), defaults)()


def test_folder_rules_apply_exclude_then_explicit_include_then_categories() -> None:
    folder = _filter_class(
        "DialogFilter",
        contacts=True,
        include_peers=[PeerChannel(20)],
        exclude_peers=[PeerUser(11)],
        exclude_archived=True,
    )

    assert _matches(folder, _dialog(10, User(contact=True))) is True
    assert _matches(folder, _dialog(11, User(contact=True))) is False
    assert _matches(folder, _dialog(-1_000_000_000_020, Channel(), archived=True)) is True
    assert _matches(folder, _dialog(12, User(contact=True), archived=True)) is False


def test_chatlist_uses_only_explicit_membership() -> None:
    folder = _filter_class(
        "DialogFilterChatlist",
        contacts=True,
        include_peers=[PeerUser(10)],
    )

    assert _matches(folder, _dialog(10, User(contact=True))) is True
    assert _matches(folder, _dialog(12, User(contact=True))) is False


class _Client:
    def __init__(self, filters: list[object], dialogs: list[object]) -> None:
        self.filters = filters
        self.dialogs = dialogs

    async def __call__(self, request: object) -> object:
        del request
        return SimpleNamespace(filters=self.filters)

    async def iter_dialogs(self, **kwargs: object):
        del kwargs
        for dialog in self.dialogs:
            yield dialog


async def test_refresh_replaces_catalog_and_membership_together(tmp_path: Path) -> None:
    conn = _connection(tmp_path / "sync.db")
    try:
        replace_folder_snapshot(conn, [(9, "Stale")], [(9, 999)])
        client = _Client(
            [_filter_class("DialogFilter", id=2, title="Contacts", contacts=True)],
            [_dialog(10, User(contact=True)), _dialog(12, User(contact=False))],
        )

        await refresh_folder_snapshot(conn, client)

        assert list_folders(conn) == [{"id": 2, "title": "Contacts"}]
        assert folder_ids_by_dialog(conn) == {10: [2]}
    finally:
        conn.close()
