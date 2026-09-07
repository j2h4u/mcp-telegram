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
from mcp_telegram.folders.read_repository import (
    dialog_placement,
    folder_snapshot,
    folder_summaries,
    folders_by_dialog,
)
from mcp_telegram.folders.refresh import FolderRefresher
from mcp_telegram.folders.sqlite_repository import (
    SQLiteFolderSnapshotRepository,
)
from mcp_telegram.folders.telegram_adapter import TelethonTelegramFolderGateway, _dialog_facts
from mcp_telegram.sync_db import ensure_sync_schema


def _connection(path: Path) -> sqlite3.Connection:
    ensure_sync_schema(path)
    return sqlite3.connect(path)


def _replace_folder_snapshot(
    conn: sqlite3.Connection, folders: list[tuple[int, str]], memberships: list[tuple[int, int]]
) -> None:
    with conn:
        conn.execute("DELETE FROM telegram_folder_members")
        conn.execute("DELETE FROM telegram_folders")
        conn.executemany("INSERT INTO telegram_folders(folder_id, title) VALUES (?, ?)", folders)
        conn.executemany("INSERT INTO telegram_folder_members(folder_id, dialog_id) VALUES (?, ?)", memberships)


def test_snapshot_exposes_many_to_many_placement_and_archive_separately(tmp_path: Path) -> None:
    conn = _connection(tmp_path / "sync.db")
    try:
        conn.execute("INSERT INTO dialogs(dialog_id, archived) VALUES (10, 1)")
        conn.commit()
        _replace_folder_snapshot(conn, [(1, "Work"), (2, "Unread")], [(1, 10), (2, 10)])

        assert folders_by_dialog(conn) == {
            10: [{"id": 1, "title": "Work"}, {"id": 2, "title": "Unread"}],
        }
        assert dialog_placement(conn, 10) == {
            "archived": True,
            "folders": [{"id": 1, "title": "Work"}, {"id": 2, "title": "Unread"}],
        }
    finally:
        conn.close()


def test_failed_snapshot_replacement_rolls_back_to_previous_snapshot(tmp_path: Path) -> None:
    conn = _connection(tmp_path / "sync.db")
    try:
        _replace_folder_snapshot(conn, [(1, "Existing")], [(1, 10)])

        with pytest.raises(sqlite3.IntegrityError):
            _replace_folder_snapshot(conn, [(2, "Duplicate"), (2, "Duplicate")], [])

        assert folders_by_dialog(conn) == {10: [{"id": 1, "title": "Existing"}]}
    finally:
        conn.close()


def test_folder_snapshot_requires_generation_and_success_timestamp(tmp_path: Path) -> None:
    conn = _connection(tmp_path / "sync.db")
    try:
        conn.execute("INSERT INTO daemon_state(key, value) VALUES ('folder_snapshot_generation', '4')")
        conn.commit()
        assert folder_snapshot(conn, stale_after_seconds=10, now=100) == {
            "generation": 4,
            "status": "unavailable",
            "completed_at": None,
            "age_seconds": None,
            "complete": False,
        }
    finally:
        conn.close()


def test_folder_snapshot_is_stale_at_threshold(tmp_path: Path) -> None:
    conn = _connection(tmp_path / "sync.db")
    try:
        conn.executemany(
            "INSERT INTO daemon_state(key, value) VALUES (?, ?)",
            [("folder_snapshot_generation", "4"), ("folder_snapshot_last_success_at", "90")],
        )
        conn.commit()
        assert folder_snapshot(conn, stale_after_seconds=10, now=100)["status"] == "stale"
    finally:
        conn.close()


def test_folder_summaries_cover_empty_membership_unread_and_activity(tmp_path: Path) -> None:
    conn = _connection(tmp_path / "sync.db")
    try:
        conn.executemany(
            "INSERT INTO dialogs(dialog_id, name, unread_count, last_message_at) VALUES (?, ?, ?, ?)",
            [(10, "Alpha", 3, 100), (20, "Beta", 0, 200)],
        )
        conn.commit()
        _replace_folder_snapshot(conn, [(1, "Work"), (2, "Empty")], [(1, 10), (1, 20)])

        assert folder_summaries(conn) == [
            {
                "id": 1,
                "title": "Work",
                "dialog_count": 2,
                "unread_dialog_count": 1,
                "unread_count": 3,
                "last_message_at": 200,
            },
            {
                "id": 2,
                "title": "Empty",
                "dialog_count": 0,
                "unread_dialog_count": 0,
                "unread_count": 0,
                "last_message_at": None,
            },
        ]
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
        _replace_folder_snapshot(conn, [(9, "Stale")], [(9, 999)])
        await FolderRefresher(_Gateway(), SQLiteFolderSnapshotRepository(conn)).refresh()

        assert folders_by_dialog(conn) == {10: [{"id": 2, "title": "Contacts"}]}
    finally:
        conn.close()


async def test_refresh_failure_propagates_and_preserves_saved_snapshot(tmp_path: Path) -> None:
    conn = _connection(tmp_path / "sync.db")
    try:
        _replace_folder_snapshot(conn, [(9, "Saved")], [(9, 999)])

        with pytest.raises(RuntimeError, match="Telegram unavailable"):
            await FolderRefresher(_FailingGateway(), SQLiteFolderSnapshotRepository(conn)).refresh()

        assert folders_by_dialog(conn) == {999: [{"id": 9, "title": "Saved"}]}
    finally:
        conn.close()
