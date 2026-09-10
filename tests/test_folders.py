"""Custom Telegram folder snapshot and rule evaluation tests."""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from telethon.tl.types import (
    Channel,
    Chat,
    ChatPhotoEmpty,
    InputPeerChannel,
    InputPeerChat,
    InputPeerEmpty,
    InputPeerUser,
    User,
)

from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.folders.contracts import (
    DialogCategory,
    DialogFacts,
    FolderDialogCursor,
    FolderDialogItem,
    FolderRule,
    FolderSourceSnapshot,
    FolderSourceUnavailableError,
    FolderStagingSnapshot,
)
from mcp_telegram.folders.membership import matches
from mcp_telegram.folders.ports import FolderSnapshotRepository
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
from mcp_telegram.folders.telegram_adapter import (
    TelethonTelegramFolderGateway,
    _dialog_facts,
    _offset_peer,
    _peer_cursor,
)
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.telegram_demand import AcquisitionKind, RpcAttemptBudget
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcSource, current_rpc_scope, rpc_attempt_budget


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


def test_corrupt_staging_is_discarded_without_touching_published_snapshot(tmp_path: Path) -> None:
    conn = _connection(tmp_path / "sync.db")
    try:
        repository = SQLiteFolderSnapshotRepository(conn)
        repository.replace_snapshot(
            FolderSourceSnapshot((FolderRule(9, "Saved"),), (DialogFacts(999, DialogCategory.CONTACT),)),
            ((9, 999),),
            completed_at=90,
        )
        with conn:
            conn.execute(
                "INSERT INTO daemon_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("folder_snapshot_staging_v1", "{not-json"),
            )

        assert repository.read_staging() is None
        assert folders_by_dialog(conn) == {999: [{"id": 9, "title": "Saved"}]}
        assert repository.read_generation() == 1
        assert repository.read_last_success_at() == 90
        assert repository.read_last_outcome() == "success"
        assert (
            conn.execute("SELECT value FROM daemon_state WHERE key = 'folder_snapshot_staging_v1'").fetchone() is None
        )
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


@pytest.mark.parametrize(
    ("entity", "expected"),
    [
        (None, (None, 0, 0)),
        (User(10, access_hash=11), ("user", 10, 11)),
        (Chat(20, "chat", ChatPhotoEmpty(), 0, None, 1), ("chat", 20, 0)),
        (
            Channel(30, "channel", ChatPhotoEmpty(), None, False, None, broadcast=True, access_hash=31),
            ("channel", 30, 31),
        ),
        (type("User", (), {"id": 40, "access_hash": 41})(), ("user", 40, 41)),
        (type("Peer", (), {"id": 50, "access_hash": 51})(), (None, 50, 51)),
    ],
)
def test_telegram_adapter_builds_cursor_peer_identity(entity: object, expected: tuple[str | None, int, int]) -> None:
    assert _peer_cursor(entity) == expected


@pytest.mark.parametrize(
    ("peer_type", "expected_type"),
    [
        ("user", InputPeerUser),
        ("chat", InputPeerChat),
        ("channel", InputPeerChannel),
        (None, InputPeerEmpty),
    ],
)
def test_telegram_adapter_reconstructs_cursor_peer(peer_type: str | None, expected_type: type[object]) -> None:
    cursor = FolderDialogCursor(None, 7, peer_type, 8, 9)
    peer = _offset_peer(cursor)

    assert isinstance(peer, expected_type)
    if isinstance(peer, InputPeerUser):
        assert (peer.user_id, peer.access_hash) == (8, 9)
    elif isinstance(peer, InputPeerChat):
        assert peer.chat_id == 8
    elif isinstance(peer, InputPeerChannel):
        assert (peer.channel_id, peer.access_hash) == (8, 9)


def _telegram_dialog(dialog_id: int, *, entity: object | None = None, message_id: int | None = None) -> object:
    if entity is None:
        entity = type("User", (), {"id": dialog_id, "access_hash": dialog_id + 100})()
    message = SimpleNamespace(
        id=dialog_id if message_id is None else message_id,
        date=dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.UTC),
    )
    return SimpleNamespace(
        id=dialog_id,
        entity=entity,
        message=message,
        archived=False,
        unread_count=0,
        unread_mentions_count=0,
        dialog=SimpleNamespace(notify_settings=None, unread_mark=False),
    )


class _AdapterClient:
    def __init__(self, pages: list[list[object]], filters: tuple[object, ...] = ()) -> None:
        self.pages = pages
        self.filters = filters
        self.dialog_calls: list[dict[str, object]] = []

    async def __call__(self, request: object) -> object:
        del request
        return SimpleNamespace(filters=self.filters)

    def iter_dialogs(self, **kwargs: object):
        self.dialog_calls.append(kwargs)
        page = self.pages.pop(0)

        async def _page():
            for dialog in page:
                yield dialog

        return _page()


async def test_telegram_adapter_iter_dialogs_maps_page_and_cursor_offsets() -> None:
    client = _AdapterClient([[_telegram_dialog(10)]])
    gateway = TelethonTelegramFolderGateway(client)

    items = [item async for item in gateway.iter_dialogs(None)]

    assert items[0].facts.dialog_id == 10
    assert items[0].cursor == FolderDialogCursor("2026-09-10T12:00:00+00:00", 10, "user", 10, 110)
    assert client.dialog_calls == [{"limit": 100, "ignore_pinned": True}]

    cursor = FolderDialogCursor("2026-09-09T11:00:00+00:00", 9, "user", 9, 109)
    client.pages.append([])
    assert [item async for item in gateway.iter_dialogs(cursor)] == []
    assert client.dialog_calls[-1] == {
        "limit": 100,
        "ignore_pinned": True,
        "offset_date": dt.datetime(2026, 9, 9, 11, 0, tzinfo=dt.UTC),
        "offset_id": 9,
        "offset_peer": InputPeerUser(9, 109),
    }


class _IterFailureClient:
    async def __call__(self, request: object) -> object:
        del request
        return SimpleNamespace(filters=())

    def __init__(self, failure: BaseException) -> None:
        self.failure = failure

    def iter_dialogs(self, **kwargs: object):
        del kwargs

        async def _page():
            raise self.failure
            yield None

        return _page()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (TimeoutError("network unavailable"), FolderSourceUnavailableError),
        (TelegramRpcThrottled(retry_after_seconds=4), TelegramRpcThrottled),
    ],
)
async def test_telegram_adapter_iter_dialogs_translates_expected_failures(
    failure: BaseException, expected: type[BaseException]
) -> None:
    gateway = TelethonTelegramFolderGateway(_IterFailureClient(failure))

    with pytest.raises(expected) as exc_info:
        _ = [item async for item in gateway.iter_dialogs(None)]

    if expected is FolderSourceUnavailableError:
        assert exc_info.value.__cause__ is failure


async def test_telegram_adapter_fetch_folders_preserves_throttling() -> None:
    failure = TelegramRpcThrottled(retry_after_seconds=4)

    with pytest.raises(TelegramRpcThrottled) as exc_info:
        await TelethonTelegramFolderGateway(_SourceFailureClient(failure)).fetch_folders()

    assert exc_info.value is failure


def _folder_filter(folder_id: int = 1) -> object:
    return type("DialogFilter", (), {"id": folder_id, "title": "Work"})()


async def test_telegram_adapter_fetch_snapshot_paginates_full_page_then_eof() -> None:
    client = _AdapterClient(
        [
            [_telegram_dialog(index) for index in range(100)],
            [],
        ],
        filters=(_folder_filter(), type("Ignored", (), {})()),
    )
    gateway = TelethonTelegramFolderGateway(client)

    snapshot = await gateway.fetch_snapshot()

    assert snapshot.folders[0].title == "Work"
    assert [dialog.dialog_id for dialog in snapshot.dialogs] == list(range(100))
    assert len(client.dialog_calls) == 2
    assert client.dialog_calls[1]["offset_id"] == 99


async def test_telegram_adapter_fetch_snapshot_stops_after_partial_page() -> None:
    client = _AdapterClient([[_telegram_dialog(7)]])

    snapshot = await TelethonTelegramFolderGateway(client).fetch_snapshot()

    assert [dialog.dialog_id for dialog in snapshot.dialogs] == [7]
    assert len(client.dialog_calls) == 1


async def test_telegram_adapter_fetch_snapshot_rejects_stalled_cursor() -> None:
    repeated = _telegram_dialog(7)
    client = _AdapterClient([[repeated] * 100, [repeated] * 100])

    with pytest.raises(FolderSourceUnavailableError, match="cursor did not advance"):
        await TelethonTelegramFolderGateway(client).fetch_snapshot()


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
    def __init__(self) -> None:
        self.scopes: list[tuple[TelegramRpcSource, DemandKind, AcquisitionKind | None]] = []

    async def fetch_snapshot(self) -> FolderSourceSnapshot:
        scope = current_rpc_scope()
        assert scope.demand_kind is not None
        self.scopes.append((scope.source, scope.demand_kind, scope.acquisition_kind))
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
        gateway = _Gateway()
        await FolderRefresher(gateway, SQLiteFolderSnapshotRepository(conn)).refresh()

        assert folders_by_dialog(conn) == {10: [{"id": 2, "title": "Contacts"}]}
        assert gateway.scopes == [
            (
                TelegramRpcSource.FOLDER_RECONCILIATION,
                DemandKind.FOLDER_SNAPSHOT,
                AcquisitionKind.FOLDER_SNAPSHOT,
            )
        ]
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


class _PagedGateway:
    def __init__(self, dialogs: tuple[DialogFacts, ...]) -> None:
        self._dialogs = dialogs
        self.fetch_calls = 0
        self.page_calls: list[int | None] = []

    async def fetch_folders(self) -> tuple[FolderRule, ...]:
        self.fetch_calls += 1
        current_rpc_scope().attempt_budget.debit()  # type: ignore[union-attr]
        return (FolderRule(2, "Contacts", categories=frozenset({DialogCategory.CONTACT})),)

    async def _page(self, cursor: FolderDialogCursor | None):
        self.page_calls.append(None if cursor is None else cursor.offset_id)
        budget = current_rpc_scope().attempt_budget
        assert budget is not None
        budget.debit()
        start = 0 if cursor is None else cursor.offset_id
        page = self._dialogs[start : start + 100]
        for index, facts in enumerate(page, start=start):
            yield FolderDialogItem(
                facts,
                FolderDialogCursor(None, index + 1, "user", facts.dialog_id, 1),
            )

    def iter_dialogs(self, cursor: FolderDialogCursor | None):
        return self._page(cursor)


class _SequencePagedGateway:
    def __init__(self, pages: list[tuple[FolderDialogItem, ...]]) -> None:
        self.pages = list(pages)
        self.fetch_calls = 0
        self.page_calls: list[int | None] = []

    async def fetch_folders(self) -> tuple[FolderRule, ...]:
        self.fetch_calls += 1
        current_rpc_scope().attempt_budget.debit()  # type: ignore[union-attr]
        return (FolderRule(2, "Contacts", categories=frozenset({DialogCategory.CONTACT})),)

    def iter_dialogs(self, cursor: FolderDialogCursor | None):
        self.page_calls.append(None if cursor is None else cursor.offset_id)
        page = self.pages.pop(0)

        async def _page():
            budget = current_rpc_scope().attempt_budget
            assert budget is not None
            budget.debit()
            for item in page:
                yield item

        return _page()


def _folder_dialog_item(dialog_id: int, category: DialogCategory, cursor_id: int) -> FolderDialogItem:
    return FolderDialogItem(
        DialogFacts(dialog_id, category),
        FolderDialogCursor(None, cursor_id, "user", dialog_id, 1),
    )


@pytest.mark.asyncio
async def test_bounded_folder_acquisition_stages_and_promotes_only_at_eof(tmp_path: Path) -> None:
    conn = _connection(tmp_path / "sync.db")
    try:
        _replace_folder_snapshot(conn, [(9, "Saved")], [(9, 999)])
        dialogs = tuple(DialogFacts(index, DialogCategory.CONTACT) for index in range(101))
        gateway = _PagedGateway(dialogs)
        repository = SQLiteFolderSnapshotRepository(conn)
        refresher = FolderRefresher(gateway, repository)

        first = RpcAttemptBudget(limit=1)
        with rpc_attempt_budget(first):
            result = await refresher.acquire_slice(first)
        assert result.complete is False
        assert result.projection is None
        assert repository.read_staging() == FolderStagingSnapshot(
            folders=(FolderRule(2, "Contacts", categories=frozenset({DialogCategory.CONTACT})),),
            dialogs=(),
            cursor=None,
            started_at=repository.read_staging().started_at,  # type: ignore[union-attr]
            base_generation=0,
        )
        assert folders_by_dialog(conn) == {999: [{"id": 9, "title": "Saved"}]}

        second = RpcAttemptBudget(limit=2)
        with rpc_attempt_budget(second):
            result = await refresher.acquire_slice(second)
        assert result.complete is False
        assert len(repository.read_staging().dialogs) == 100  # type: ignore[union-attr]
        assert folders_by_dialog(conn) == {999: [{"id": 9, "title": "Saved"}]}

        restarted = FolderRefresher(gateway, repository)
        third = RpcAttemptBudget(limit=2)
        with rpc_attempt_budget(third):
            result = await restarted.acquire_slice(third)
        assert result.complete is True
        assert result.projection is not None
        restarted.persist(result.projection, completed_at=123)
        assert repository.read_staging() is None
        assert folders_by_dialog(conn)[100] == [{"id": 2, "title": "Contacts"}]
        assert repository.read_generation() == 1
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_bounded_folder_acquisition_discards_stale_staging_generation(tmp_path: Path) -> None:
    conn = _connection(tmp_path / "sync.db")
    try:
        repository = SQLiteFolderSnapshotRepository(conn)
        repository.replace_snapshot(
            FolderSourceSnapshot((FolderRule(1, "Current"),), ()),
            (),
            completed_at=10,
        )
        repository.save_staging(
            FolderStagingSnapshot(
                folders=(FolderRule(8, "Stale"),),
                dialogs=(),
                cursor=None,
                started_at=1,
                base_generation=0,
            )
        )
        gateway = _PagedGateway(())
        refresher = FolderRefresher(gateway, repository)
        budget = RpcAttemptBudget(limit=2)
        with rpc_attempt_budget(budget):
            result = await refresher.acquire_slice(budget)
        assert result.complete is True
        assert result.projection is not None
        assert result.projection.source.folders[0].title == "Contacts"
        assert gateway.fetch_calls == 1
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_bounded_folder_acquisition_restarts_after_corrupt_staging(tmp_path: Path) -> None:
    conn = _connection(tmp_path / "sync.db")
    try:
        repository = SQLiteFolderSnapshotRepository(conn)
        repository.replace_snapshot(
            FolderSourceSnapshot((FolderRule(9, "Saved"),), (DialogFacts(999, DialogCategory.CONTACT),)),
            ((9, 999),),
            completed_at=90,
        )
        with conn:
            conn.execute(
                "INSERT INTO daemon_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("folder_snapshot_staging_v1", "{not-json"),
            )

        gateway = _PagedGateway(())
        refresher = FolderRefresher(gateway, repository)
        budget = RpcAttemptBudget(limit=2)
        with rpc_attempt_budget(budget):
            result = await refresher.acquire_slice(budget)

        assert result.complete is True
        assert result.projection is not None
        assert gateway.fetch_calls == 1
        assert folders_by_dialog(conn) == {999: [{"id": 9, "title": "Saved"}]}
        refresher.persist(result.projection, completed_at=123)
        assert folders_by_dialog(conn) == {}
        assert repository.read_generation() == 2
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_bounded_acquisition_replaces_duplicate_fact_without_merging_fields(tmp_path: Path) -> None:
    conn = _connection(tmp_path / "sync.db")
    try:
        gateway = _SequencePagedGateway(
            [
                (
                    _folder_dialog_item(10, DialogCategory.CONTACT, 1),
                    _folder_dialog_item(10, DialogCategory.NON_CONTACT, 2),
                ),
            ]
        )
        repository = SQLiteFolderSnapshotRepository(conn)
        refresher = FolderRefresher(gateway, repository)
        budget = RpcAttemptBudget(limit=3)
        with rpc_attempt_budget(budget):
            result = await refresher.acquire_slice(budget)

        assert result.complete is True
        assert result.projection is not None
        assert result.projection.source.dialogs == (DialogFacts(10, DialogCategory.NON_CONTACT),)
        assert result.projection.memberships == ()
        assert gateway.page_calls == [None]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_two_page_overlap_survives_restart_and_keeps_last_cursor(tmp_path: Path) -> None:
    conn = _connection(tmp_path / "sync.db")
    try:
        first_page = tuple(_folder_dialog_item(index, DialogCategory.CONTACT, index + 1) for index in range(100))
        second_page = (
            _folder_dialog_item(97, DialogCategory.NON_CONTACT, 201),
            _folder_dialog_item(98, DialogCategory.NON_CONTACT, 202),
            _folder_dialog_item(99, DialogCategory.NON_CONTACT, 203),
            _folder_dialog_item(100, DialogCategory.CONTACT, 204),
        )
        gateway = _SequencePagedGateway([first_page, second_page])
        repository = SQLiteFolderSnapshotRepository(conn)
        refresher = FolderRefresher(gateway, repository)

        first_budget = RpcAttemptBudget(limit=3)
        with rpc_attempt_budget(first_budget):
            first = await refresher.acquire_slice(first_budget)
        assert first.complete is False
        assert first.projection is None
        assert len(repository.read_staging().dialogs) == 100  # type: ignore[union-attr]

        restarted = FolderRefresher(gateway, repository)
        second_budget = RpcAttemptBudget(limit=2)
        with rpc_attempt_budget(second_budget):
            second = await restarted.acquire_slice(second_budget)
        assert second.complete is True
        assert second.projection is not None
        assert [dialog.dialog_id for dialog in second.projection.source.dialogs] == list(range(101))
        assert second.projection.source.dialogs[97] == DialogFacts(97, DialogCategory.NON_CONTACT)
        assert repository.read_staging().cursor.offset_id == 204  # type: ignore[union-attr]
        assert gateway.page_calls == [None, 100]

        restarted.persist(second.projection, completed_at=321)
        assert repository.read_staging() is None
        assert repository.read_generation() == 1
        assert conn.execute("SELECT COUNT(*) FROM telegram_folder_members").fetchone()[0] == 98
        assert folders_by_dialog(conn).get(97) is None
        assert folders_by_dialog(conn)[100] == [{"id": 2, "title": "Contacts"}]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_one_hundred_observations_with_overlap_continue_and_retain_cursor(tmp_path: Path) -> None:
    conn = _connection(tmp_path / "sync.db")
    try:
        page = tuple(
            _folder_dialog_item(index if index < 99 else 98, DialogCategory.CONTACT, index + 1) for index in range(100)
        )
        gateway = _SequencePagedGateway([page, ()])
        repository = SQLiteFolderSnapshotRepository(conn)
        refresher = FolderRefresher(gateway, repository)

        first_budget = RpcAttemptBudget(limit=3)
        with rpc_attempt_budget(first_budget):
            first = await refresher.acquire_slice(first_budget)
        assert first.complete is False
        assert first.projection is None
        staging = repository.read_staging()
        assert staging is not None
        assert len(staging.dialogs) == 99
        assert staging.cursor is not None and staging.cursor.offset_id == 100

        second_budget = RpcAttemptBudget(limit=1)
        with rpc_attempt_budget(second_budget):
            second = await refresher.acquire_slice(second_budget)
        assert second.complete is True
        assert second.projection is not None
        assert len(second.projection.source.dialogs) == 99
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_duplicate_staging_is_normalized_before_empty_page_completion(tmp_path: Path) -> None:
    conn = _connection(tmp_path / "sync.db")
    try:
        repository = SQLiteFolderSnapshotRepository(conn)
        cursor = FolderDialogCursor(None, 7, "user", 10, 1)
        repository.save_staging(
            FolderStagingSnapshot(
                folders=(FolderRule(2, "Contacts", categories=frozenset({DialogCategory.CONTACT})),),
                dialogs=(
                    DialogFacts(10, DialogCategory.CONTACT),
                    DialogFacts(10, DialogCategory.NON_CONTACT),
                ),
                cursor=cursor,
                started_at=123,
                base_generation=0,
            )
        )
        gateway = _SequencePagedGateway([()])
        refresher = FolderRefresher(gateway, repository)
        budget = RpcAttemptBudget(limit=1)
        with rpc_attempt_budget(budget):
            result = await refresher.acquire_slice(budget)

        assert result.complete is True
        assert result.projection is not None
        assert result.projection.source.dialogs == (DialogFacts(10, DialogCategory.NON_CONTACT),)
        normalized = repository.read_staging()
        assert normalized is not None
        assert normalized.dialogs == (DialogFacts(10, DialogCategory.NON_CONTACT),)
        assert normalized.folders[0].title == "Contacts"
        assert normalized.cursor == cursor
        assert normalized.started_at == 123
        assert normalized.base_generation == 0
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_duplicate_staging_is_normalized_even_when_budget_is_exhausted(tmp_path: Path) -> None:
    conn = _connection(tmp_path / "sync.db")
    try:
        repository = SQLiteFolderSnapshotRepository(conn)
        repository.save_staging(
            FolderStagingSnapshot(
                folders=(),
                dialogs=(
                    DialogFacts(10, DialogCategory.CONTACT),
                    DialogFacts(10, DialogCategory.NON_CONTACT),
                ),
                cursor=None,
                started_at=123,
                base_generation=0,
            )
        )
        gateway = _SequencePagedGateway([()])
        refresher = FolderRefresher(gateway, repository)
        budget = RpcAttemptBudget(limit=1, attempts=1)
        with rpc_attempt_budget(budget):
            result = await refresher.acquire_slice(budget)

        assert result.complete is False
        assert result.projection is None
        assert repository.read_staging().dialogs == (DialogFacts(10, DialogCategory.NON_CONTACT),)  # type: ignore[union-attr]
        assert gateway.page_calls == []
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_legacy_acquisition_normalizes_before_membership_projection() -> None:
    class _LegacyGateway:
        async def fetch_snapshot(self) -> FolderSourceSnapshot:
            return FolderSourceSnapshot(
                folders=(FolderRule(2, "Contacts", categories=frozenset({DialogCategory.CONTACT})),),
                dialogs=(
                    DialogFacts(10, DialogCategory.CONTACT),
                    DialogFacts(10, DialogCategory.NON_CONTACT),
                ),
            )

    projection = await FolderRefresher(_LegacyGateway(), cast(FolderSnapshotRepository, object())).acquire()

    assert projection.source.dialogs == (DialogFacts(10, DialogCategory.NON_CONTACT),)
    assert projection.memberships == ()
