"""Owned writes reject ambient SQLite transactions and preserve nested rollback."""

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from mcp_telegram.config import DraftRecoveryConfig
from mcp_telegram.drafts.sqlite_projection import SQLiteDraftProjection
from mcp_telegram.entity_profile.repository import EntityProfileRepository
from mcp_telegram.entity_store import EntitySnapshot, upsert_entity_snapshots
from mcp_telegram.folders.sqlite_repository import replace_folder_snapshot
from mcp_telegram.hydration_queue import HydrationJob, HydrationQueueRepository
from mcp_telegram.own_only import OwnOnlyBasis, OwnOnlyClassification, enroll_own_only_dialog
from mcp_telegram.read_state import apply_read_cursor
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import enable_runtime_writes, write_transaction
from mcp_telegram.telegram_fragments import FragmentContextService
from mcp_telegram.telegram_reading import FragmentFetchResult
from mcp_telegram.topics.sqlite_repository import SQLiteTopicMetadataRepository


@pytest.fixture
def guarded_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    enable_runtime_writes(conn)
    try:
        yield conn
    finally:
        conn.close()


def _neutral_writers(conn: sqlite3.Connection) -> tuple[Callable[[], object], ...]:
    return (
        lambda: upsert_entity_snapshots(conn, [EntitySnapshot(1, "user", "A", None, None, 1)]),
        lambda: apply_read_cursor(conn, 1, "inbox", 5),
        lambda: HydrationQueueRepository(conn).enqueue(HydrationJob("media_metadata", 1, 2, 3, 0)),
        lambda: SQLiteTopicMetadataRepository(conn).apply_topic_pin(1, 2, pinned=True, observed_at=3),
    )


def test_neutral_helpers_require_owner_and_never_commit(guarded_db: sqlite3.Connection) -> None:
    conn = guarded_db
    for writer in _neutral_writers(conn):
        with pytest.raises(RuntimeError, match="owned write_transaction"):
            writer()
    with pytest.raises(ValueError, match="rollback"):
        with write_transaction(conn):
            conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (1,'own_only')")
            for writer in _neutral_writers(conn):
                writer()
                assert conn.in_transaction
            raise ValueError("rollback")
    assert conn.execute("SELECT COUNT(*) FROM entities").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)
    assert conn.execute("PRAGMA query_only").fetchone() == (1,)


def test_nested_folder_and_enrollment_writes_rollback_with_outer_owner(guarded_db: sqlite3.Connection) -> None:
    conn = guarded_db
    with pytest.raises(ValueError, match="rollback"):
        with write_transaction(conn):
            replace_folder_snapshot(conn, [(1, "Folder")], [])
            enroll_own_only_dialog(conn, 55, OwnOnlyClassification(True, (OwnOnlyBasis.DIRECT_MESSAGE,)), now=1)
            assert conn.in_transaction
            raise ValueError("rollback")
    assert conn.execute("SELECT COUNT(*) FROM telegram_folders").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM own_only_dialogs").fetchone() == (0,)


def test_core_cas_rejection_rolls_back_only_nested_projection(
    guarded_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = guarded_db
    repo = EntityProfileRepository(conn, section_ttl_seconds=10)
    repo.save_core({"id": 42, "type": "user", "name": "Before"}, now=1)
    repo.mark_pending(42, now=10)
    cursor = repo.next_due_refresh(now=10)
    assert cursor is not None
    monkeypatch.setattr(repo, "_cursor_matches", lambda _: True)
    monkeypatch.setattr(repo, "_cursor_predicate", lambda _: ("entity_id=-1", ()))
    with write_transaction(conn):
        conn.execute("INSERT INTO entities(id,type,name,updated_at) VALUES (99,'user','Outer',1)")
        assert not repo.commit_core_acquisition(
            cursor, {"id": 42, "type": "user", "name": "Rejected"}, next_acquisition_cursor=1, now=11
        )
        assert conn.in_transaction
        assert conn.execute("SELECT name FROM entities WHERE id=42").fetchone() == ("Before",)
    assert conn.execute("SELECT name FROM entities WHERE id=99").fetchone() == ("Outer",)
    assert conn.execute("PRAGMA query_only").fetchone() == (1,)


def test_draft_writer_rejects_foreign_ambient_transaction(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("INSERT INTO entities(id,type,name,updated_at) VALUES (99,'user','Ambient',1)")
        repo = SQLiteDraftProjection(conn, DraftRecoveryConfig())
        with pytest.raises(RuntimeError, match="idle connection"):
            repo.bind_account(1)
        assert conn.in_transaction
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM entities").fetchone() == (0,)
        enable_runtime_writes(conn)
        repo.bind_account(1)
        assert not conn.in_transaction
        assert conn.execute("PRAGMA query_only").fetchone() == (1,)
    finally:
        conn.close()


class _FragmentGateway:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    async def fetch_context(self, dialog_id: int, anchor_message_id: int, window_size: int) -> FragmentFetchResult:
        assert not self.conn.in_transaction
        assert self.conn.execute("PRAGMA query_only").fetchone() == (1,)
        return FragmentFetchResult(messages=())


async def test_fragment_rpc_runs_after_write_owner_is_released(guarded_db: sqlite3.Connection) -> None:
    conn = guarded_db
    result = await FragmentContextService(conn, _FragmentGateway(conn)).fetch(1, 2, 3)
    assert result.ok
    assert conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id=1").fetchone() == ("fragment",)


def test_fragment_rejects_inherited_owner_before_rpc(guarded_db: sqlite3.Connection) -> None:
    conn = guarded_db
    with write_transaction(conn):
        with pytest.raises(RuntimeError, match="idle connection"):
            FragmentContextService(conn, _FragmentGateway(conn)).fetch(1, 2, 3).send(None)
        assert conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM synced_dialogs").fetchone() == (0,)


def test_native_runtime_ports_commit_one_coherent_domain_unit(guarded_db: sqlite3.Connection) -> None:
    conn = guarded_db
    topics = SQLiteTopicMetadataRepository(conn)
    queue = HydrationQueueRepository(conn)
    with write_transaction(conn):
        upsert_entity_snapshots(conn, [EntitySnapshot(1, "user", "Alice", "alice", "alice", 10)])
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (1,'synced')")
        assert apply_read_cursor(conn, 1, "inbox", 42) == 1
        topics.apply_topic_create(1, 7, title="Before", icon_emoji_id=None, date=10, observed_at=10)
        topics.apply_topic_edit(1, 7, title="After", icon_emoji_id=None, hidden=False, observed_at=11)
        topics.apply_topic_pin(1, 7, pinned=True, observed_at=12)
        job = HydrationJob("media_metadata", 1, 2, 3, 0)
        queue.enqueue(job)
        started = queue.start(job)
        assert started is not None
        assert started.attempts == 1
        enroll_own_only_dialog(conn, 1, OwnOnlyClassification(True, (OwnOnlyBasis.DIRECT_MESSAGE,)), now=12)
        replace_folder_snapshot(conn, [(2, "Folder")], [])
    assert conn.execute("SELECT name FROM entities WHERE id=1").fetchone() == ("Alice",)
    assert conn.execute("SELECT read_inbox_max_id,status FROM synced_dialogs WHERE dialog_id=1").fetchone() == (
        42,
        "synced",
    )
    assert conn.execute("SELECT title,pinned FROM topic_metadata WHERE dialog_id=1 AND topic_id=7").fetchone() == (
        "After",
        1,
    )
    assert conn.execute("SELECT attempts FROM hydration_jobs WHERE dialog_id=1").fetchone() == (1,)
    assert conn.execute("SELECT title FROM telegram_folders WHERE folder_id=2").fetchone() == ("Folder",)
    assert conn.execute("PRAGMA query_only").fetchone() == (1,)
    assert not conn.in_transaction
