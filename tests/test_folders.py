"""Custom Telegram folder snapshot and rule evaluation tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mcp_telegram.folders.contracts import (
    DialogCategory,
    DialogFacts,
    FolderRule,
    FolderSourceSnapshot,
    FolderSourceUnavailableError,
)
from mcp_telegram.folders.membership import matches
from mcp_telegram.folders.refresh import FolderRefresher
from mcp_telegram.folders.sqlite_repository import (
    SQLiteFolderSnapshotRepository,
    dialog_placement,
    folder_ids_by_dialog,
    list_folder_messages,
    list_folders,
    replace_folder_snapshot,
)
from mcp_telegram.folders.telegram_adapter import TelethonTelegramFolderGateway, _dialog_facts
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


def test_folder_messages_merge_local_rows_and_report_incomplete_dialogs(tmp_path: Path) -> None:
    conn = _connection(tmp_path / "sync.db")
    try:
        conn.executemany(
            "INSERT INTO dialogs(dialog_id, name) VALUES (?, ?)",
            [(10, "Alpha"), (20, "Beta")],
        )
        conn.executemany(
            "INSERT INTO messages(dialog_id, message_id, sent_at, text) VALUES (?, ?, ?, ?)",
            [(10, 1, 100, "older"), (20, 2, 200, "newer")],
        )
        conn.execute(
            "INSERT INTO synced_dialogs(dialog_id, status, sync_progress, total_messages) VALUES (10, 'synced', 10, 10)"
        )
        conn.commit()
        replace_folder_snapshot(conn, [(1, "Work")], [(1, 10), (1, 20)])

        assert list_folder_messages(conn, 1, 20) == {
            "folder_id": 1,
            "messages": [
                {
                    "dialog_id": 20,
                    "message_id": 2,
                    "sent_at": 200,
                    "text": "newer",
                    "dialog_name": "Beta",
                },
                {
                    "dialog_id": 10,
                    "message_id": 1,
                    "sent_at": 100,
                    "text": "older",
                    "dialog_name": "Alpha",
                },
            ],
            "partial": True,
            "incomplete_dialog_ids": [20],
            "next_navigation": None,
        }
    finally:
        conn.close()


def test_folder_rules_apply_exclude_then_explicit_include_then_categories() -> None:
    folder = FolderRule(
        folder_id=1,
        title="Folder",
        categories=frozenset({DialogCategory.CONTACT}),
        included_ids=frozenset({-1_000_000_000_020}),
        excluded_ids=frozenset({11}),
        exclude_archived=True,
    )

    assert matches(folder, DialogFacts(10, DialogCategory.CONTACT)) is True
    assert matches(folder, DialogFacts(11, DialogCategory.CONTACT)) is False
    assert matches(folder, DialogFacts(-1_000_000_000_020, DialogCategory.BROADCAST, archived=True)) is True
    assert matches(folder, DialogFacts(12, DialogCategory.CONTACT, archived=True)) is False


def test_chatlist_uses_only_explicit_membership() -> None:
    folder = FolderRule(
        folder_id=1,
        title="Folder",
        categories=frozenset({DialogCategory.CONTACT}),
        included_ids=frozenset({10}),
        explicit_only=True,
    )

    assert matches(folder, DialogFacts(10, DialogCategory.CONTACT)) is True
    assert matches(folder, DialogFacts(12, DialogCategory.CONTACT)) is False


def test_exclude_read_keeps_manually_marked_unread_dialog() -> None:
    folder = FolderRule(
        folder_id=1,
        title="Folder",
        categories=frozenset({DialogCategory.CONTACT}),
        exclude_read=True,
    )

    assert matches(folder, DialogFacts(10, DialogCategory.CONTACT, unread=True)) is True
    assert matches(folder, DialogFacts(11, DialogCategory.CONTACT)) is False


def test_telegram_adapter_counts_manual_unread_mark() -> None:
    dialog = type(
        "Dialog",
        (),
        {
            "id": 10,
            "entity": type("User", (), {"bot": False, "contact": True, "mutual_contact": False})(),
            "archived": False,
            "unread_count": 0,
            "unread_mentions_count": 0,
            "dialog": type("Inner", (), {"notify_settings": None, "unread_mark": True})(),
        },
    )()

    assert _dialog_facts(dialog).unread is True


class _SourceFailureClient:
    def __init__(self, failure: Exception) -> None:
        self._failure = failure

    async def __call__(self, request: object) -> object:
        del request
        raise self._failure

    async def iter_dialogs(self, **kwargs: object):
        del kwargs
        if False:
            yield None


async def test_telegram_adapter_maps_expected_source_failure() -> None:
    gateway = TelethonTelegramFolderGateway(_SourceFailureClient(TimeoutError("network unavailable")))

    with pytest.raises(FolderSourceUnavailableError) as exc_info:
        await gateway.fetch_snapshot()

    assert isinstance(exc_info.value.__cause__, TimeoutError)


async def test_telegram_adapter_does_not_map_programming_failure() -> None:
    gateway = TelethonTelegramFolderGateway(_SourceFailureClient(RuntimeError("broken invariant")))

    with pytest.raises(RuntimeError, match="broken invariant"):
        await gateway.fetch_snapshot()


class _Gateway:
    async def fetch_snapshot(self) -> FolderSourceSnapshot:
        return FolderSourceSnapshot(
            folders=(FolderRule(2, "Contacts", categories=frozenset({DialogCategory.CONTACT})),),
            dialogs=(
                DialogFacts(10, DialogCategory.CONTACT),
                DialogFacts(12, DialogCategory.NON_CONTACT),
            ),
        )


class _FailingGateway:
    async def fetch_snapshot(self) -> FolderSourceSnapshot:
        raise RuntimeError("Telegram unavailable")


async def test_refresh_replaces_catalog_and_membership_together(tmp_path: Path) -> None:
    conn = _connection(tmp_path / "sync.db")
    try:
        replace_folder_snapshot(conn, [(9, "Stale")], [(9, 999)])
        await FolderRefresher(_Gateway(), SQLiteFolderSnapshotRepository(conn)).refresh()

        assert list_folders(conn) == [{"id": 2, "title": "Contacts"}]
        assert folder_ids_by_dialog(conn) == {10: [2]}
    finally:
        conn.close()


async def test_refresh_failure_propagates_and_preserves_saved_snapshot(tmp_path: Path) -> None:
    conn = _connection(tmp_path / "sync.db")
    try:
        replace_folder_snapshot(conn, [(9, "Saved")], [(9, 999)])

        with pytest.raises(RuntimeError, match="Telegram unavailable"):
            await FolderRefresher(_FailingGateway(), SQLiteFolderSnapshotRepository(conn)).refresh()

        assert list_folders(conn) == [{"id": 9, "title": "Saved"}]
        assert folder_ids_by_dialog(conn) == {999: [9]}
    finally:
        conn.close()
