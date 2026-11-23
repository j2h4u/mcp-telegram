"""Newly eligible retained facts are recovered by bounded background work."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import mcp_telegram.sync_db as sync_db
from mcp_telegram.history_enrollment import enable_history
from mcp_telegram.messages.sqlite_hydration_jobs import (
    repair_rich_composition_hydration_jobs,
    rich_composition_repair_revision,
)
from mcp_telegram.sync_transactions import write_transaction


@pytest.mark.parametrize("enrollment_first", [True, False])
def test_eligibility_orders_schedule_one_bounded_repair(tmp_path: Path, enrollment_first: bool) -> None:
    path = tmp_path / "sync.db"
    sync_db.ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO daemon_state(key,value) VALUES('rich_composition_hydration_cursor',?)",
            ('{"cursor":null,"done":true,"cleanup_done":true}',),
        )
        conn.executemany(
            "INSERT INTO messages(dialog_id,message_id,sent_at,edit_date,rich_message,is_deleted) VALUES(42,?,?,?,?,?)",
            [
                (1, 1777593600, None, None, 0),
                (2, 1777593599, None, None, 0),
                (3, 1, None, '{"part":true}', 0),
                (4, 1, 1777593600, None, 0),
                (5, 1777593601, None, "null", 0),
                (6, 1777593601, None, None, 1),
            ],
        )
        if enrollment_first:
            conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES(42,1,10)")
            conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(42,'syncing')")
        else:
            conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(42,'not_synced')")
            conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES(42,1,10)")
            conn.execute("UPDATE synced_dialogs SET status='syncing' WHERE dialog_id=42")
        conn.commit()
        assert conn.execute("SELECT cursor,generation FROM rich_dialog_repairs").fetchone() == (0, 1)
        assert rich_composition_repair_revision(conn) is not None
        with write_transaction(conn):
            assert repair_rich_composition_hydration_jobs(conn, due_at=100, max_jobs=2)
        assert conn.execute("SELECT cursor,generation FROM rich_dialog_repairs").fetchone() == (2, 1)
        with write_transaction(conn):
            assert repair_rich_composition_hydration_jobs(conn, due_at=100, max_jobs=2)
        with write_transaction(conn):
            assert not repair_rich_composition_hydration_jobs(conn, due_at=100, max_jobs=2)
        assert conn.execute(
            "SELECT message_id,priority FROM hydration_jobs WHERE kind='rich_composition' ORDER BY message_id"
        ).fetchall() == [(1, 0), (3, 0), (4, 0)]
        assert conn.execute("SELECT cursor,generation FROM rich_dialog_repairs").fetchone() == (-1, 1)
        conn.execute("UPDATE synced_dialogs SET status='synced' WHERE dialog_id=42")
        conn.execute("UPDATE full_history_enrollment SET enabled=1 WHERE dialog_id=42")
        assert conn.execute("SELECT cursor,generation FROM rich_dialog_repairs").fetchone() == (-1, 1)
        conn.execute("UPDATE synced_dialogs SET status='access_lost' WHERE dialog_id=42")
        conn.execute("UPDATE synced_dialogs SET status='synced' WHERE dialog_id=42")
        assert conn.execute("SELECT cursor,generation FROM rich_dialog_repairs").fetchone() == (0, 2)


def test_v105_upgrade_seeds_only_missing_eligible_unknown_and_preserves_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sync.db"
    with monkeypatch.context() as historical:
        historical.setattr(sync_db, "_CURRENT_SCHEMA_VERSION", 105)
        sync_db.ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(42,'synced')")
        conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES(42,1,10)")
        conn.executemany(
            "INSERT INTO messages(dialog_id,message_id,sent_at,rich_message,is_deleted) VALUES(42,?,?,?,?)",
            [
                (1, 1777593600, None, 0),
                (2, 1777593600, None, 0),
                (3, 1777593599, None, 0),
                (4, 1777593600, "null", 0),
                (5, 1777593600, None, 1),
                (6, 1777593600, None, 0),
                (7, 1777593600, None, 0),
            ],
        )
        conn.execute("INSERT INTO nonchannel_message_deletions(message_id,deleted_at) VALUES(6,1)")
        conn.execute(
            "INSERT INTO message_observations(dialog_id,message_id,observation_order,is_deleted) VALUES(42,7,1,1)"
        )
        conn.execute(
            "INSERT INTO hydration_jobs(kind,dialog_id,message_id,due_at,attempts,priority,message_sent_at,terminal,last_outcome,last_error_code) VALUES('rich_composition',42,2,10,5,0,1777593600,1,'terminal_error','missing_response')"
        )
        conn.commit()
    sync_db.ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute(
            "SELECT message_id,attempts,terminal,last_error_code FROM hydration_jobs WHERE kind='rich_composition' ORDER BY message_id"
        ).fetchall() == [(1, 0, 0, None), (2, 5, 1, "missing_response")]
        assert conn.execute("SELECT COUNT(*) FROM rich_dialog_repairs").fetchone() == (0,)
        sync_db.ensure_sync_schema(path)
        assert conn.execute("SELECT COUNT(*) FROM hydration_jobs WHERE kind='rich_composition'").fetchone() == (2,)


@pytest.mark.parametrize("entry", ["explicit", "automatic_dm", "automatic_group", "recovery"])
def test_real_eligibility_producers_schedule_once(tmp_path: Path, entry: str) -> None:
    from mcp_telegram.access_lifecycle import restore_access_after_revalidation
    from mcp_telegram.history_enrollment import ensure_automatic_dm_enrollment, record_automatic_group_decision

    path = tmp_path / "sync.db"
    sync_db.ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO daemon_state(key,value) VALUES('rich_composition_hydration_cursor',?)",
            ('{"cursor":null,"done":true,"cleanup_done":true}',),
        )
        conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at) VALUES(42,1,1777593600)")
        status = "access_lost" if entry == "recovery" else "not_synced"
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(42,?)", (status,))
        if entry == "recovery":
            conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES(42,1,1)")
        conn.commit()
        with write_transaction(conn):
            if entry == "explicit":
                enable_history(conn, 42, now=2)
            elif entry == "automatic_dm":
                ensure_automatic_dm_enrollment(conn, 42, now=2)
            elif entry == "automatic_group":
                record_automatic_group_decision(conn, 42, enabled=True, now=2)
                conn.execute("UPDATE synced_dialogs SET status='syncing' WHERE dialog_id=42")
            else:
                restore_access_after_revalidation(conn, 42, 2)
        assert conn.execute("SELECT cursor,generation FROM rich_dialog_repairs WHERE dialog_id=42").fetchone() == (0, 1)
        with write_transaction(conn):
            assert not repair_rich_composition_hydration_jobs(conn, due_at=3, max_jobs=20)
        assert conn.execute("SELECT message_id FROM hydration_jobs WHERE kind='rich_composition'").fetchall() == [(1,)]
        if entry == "explicit":
            with write_transaction(conn):
                enable_history(conn, 42, now=4)
            assert conn.execute("SELECT cursor,generation FROM rich_dialog_repairs WHERE dialog_id=42").fetchone() == (
                -1,
                1,
            )


def test_repair_respects_both_tombstones_and_channel_namespace(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    sync_db.ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO daemon_state(key,value) VALUES('rich_composition_hydration_cursor',?)",
            ('{"cursor":null,"done":true,"cleanup_done":true}',),
        )
        conn.executemany("INSERT INTO synced_dialogs(dialog_id,status) VALUES(?,'synced')", [(42,), (-1000000000001,)])
        conn.executemany(
            "INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES(?,1,1)",
            [(42,), (-1000000000001,)],
        )
        conn.executemany(
            "INSERT INTO messages(dialog_id,message_id,sent_at) VALUES(?,?,1777593600)",
            [(42, 2), (42, 3), (-1000000000001, 2)],
        )
        conn.execute("INSERT INTO nonchannel_message_deletions(message_id,deleted_at) VALUES(2,1)")
        conn.execute(
            "INSERT INTO message_observations(dialog_id,message_id,observation_order,is_deleted) VALUES(42,3,1,1)"
        )
        conn.commit()
        with write_transaction(conn):
            repair_rich_composition_hydration_jobs(conn, due_at=3, max_jobs=20)
        with write_transaction(conn):
            repair_rich_composition_hydration_jobs(conn, due_at=3, max_jobs=20)
        assert conn.execute(
            "SELECT dialog_id,message_id FROM hydration_jobs WHERE kind='rich_composition'"
        ).fetchall() == [(-1000000000001, 2)]


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["generation", "account"])
async def test_repair_worker_fences_revision_inside_writer_transaction(tmp_path: Path, race: str) -> None:
    import asyncio
    from collections.abc import Callable
    from typing import cast

    from mcp_telegram.fact_hydration import FactHydrationDemandAdapter, MessageFactHydrationWorker
    from mcp_telegram.hydration_queue import HydrationPriority
    from mcp_telegram.media_hydration import RichCompositionHydrationHandler
    from mcp_telegram.sqlite_writer import SQLiteWriter
    from mcp_telegram.telegram_demand import RpcAttemptBudget

    path = tmp_path / "sync.db"
    sync_db.ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO daemon_state(key,value) VALUES('rich_composition_hydration_cursor',?)",
            ('{"cursor":null,"done":true,"cleanup_done":true}',),
        )
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(42,'synced')")
        conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES(42,1,1)")
        conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at) VALUES(42,1,1777593600)")
        conn.commit()

        class RacingWriter:
            async def run[T](self, operation: Callable[[sqlite3.Connection], T]) -> T:
                with closing(sqlite3.connect(path)) as other:
                    if race == "account":
                        other.execute("UPDATE dialog_directory_state SET account_id=99 WHERE singleton=1")
                    else:
                        other.execute("UPDATE synced_dialogs SET status='access_lost' WHERE dialog_id=42")
                        other.execute("UPDATE synced_dialogs SET status='synced' WHERE dialog_id=42")
                    other.commit()
                return operation(conn)

        worker = MessageFactHydrationWorker(
            object(),
            conn,
            asyncio.Event(),
            writer=cast(SQLiteWriter, RacingWriter()),
            handlers=(RichCompositionHydrationHandler(batch_size=1),),
            interval_seconds=60,
            max_requests_per_cycle=3,
            max_jobs_per_cycle=1,
            retry_delay_seconds=1,
            circuit_retry_seconds=1,
            max_attempts=3,
            pause_between_requests_seconds=0,
            backfill_debt_limit=1,
            clock=lambda: 100.0,
            profile_owner_account_id=42,
        )
        adapter = FactHydrationDemandAdapter(worker, HydrationPriority.BACKFILL, account_id=42)
        selected = adapter.status(100)
        assert selected is not None and selected.selected is not None
        outcome = await adapter.run_slice(RpcAttemptBudget(limit=1, selected=selected.selected))
        assert outcome.kind.value == "stale"
        assert conn.execute("SELECT cursor,generation FROM rich_dialog_repairs").fetchone() == (
            0,
            1 if race == "account" else 2,
        )
        if race == "account":
            assert rich_composition_repair_revision(conn) == selected.selected.revision
        assert conn.execute("SELECT count(*) FROM hydration_jobs WHERE kind='rich_composition'").fetchone() == (0,)


@pytest.mark.asyncio
async def test_empty_global_scan_finalizes_before_dialog_partial_repair(tmp_path: Path) -> None:
    import asyncio

    from mcp_telegram.fact_hydration import FactHydrationDemandAdapter, MessageFactHydrationWorker
    from mcp_telegram.hydration_progress import queue_snapshots
    from mcp_telegram.hydration_queue import HydrationPriority
    from mcp_telegram.media_hydration import RichCompositionHydrationHandler
    from mcp_telegram.telegram_demand import RpcAttemptBudget
    from sqlite_writer_helpers import inline_writer

    path = tmp_path / "sync.db"
    sync_db.ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO daemon_state(key,value) VALUES('rich_composition_hydration_cursor',?)",
            ('{"cursor":null,"done":false,"cleanup_done":true}',),
        )
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(42,'synced')")
        conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES(42,1,1)")
        conn.execute(
            "INSERT INTO messages(dialog_id,message_id,sent_at,rich_message) VALUES(42,1,1,?)", ('{"part":true}',)
        )
        conn.commit()
        worker = MessageFactHydrationWorker(
            object(),
            conn,
            asyncio.Event(),
            writer=inline_writer(conn),
            handlers=(RichCompositionHydrationHandler(batch_size=1),),
            interval_seconds=60,
            max_requests_per_cycle=3,
            max_jobs_per_cycle=1,
            retry_delay_seconds=1,
            circuit_retry_seconds=1,
            max_attempts=3,
            pause_between_requests_seconds=0,
            backfill_debt_limit=1,
            clock=lambda: 100.0,
            profile_owner_account_id=42,
        )
        adapter = FactHydrationDemandAdapter(worker, HydrationPriority.BACKFILL, account_id=42)
        first = adapter.status(100)
        assert first is not None and first.selected is not None
        assert (await adapter.run_slice(RpcAttemptBudget(limit=1, selected=first.selected))).kind.value == "progressed"
        assert queue_snapshots(conn, 100, paused=False)[0]["scan_complete"] is False
        worker.next_repair_at = 100
        second = adapter.status(100)
        assert second is not None and second.selected is not None
        assert (await adapter.run_slice(RpcAttemptBudget(limit=1, selected=second.selected))).kind.value == "progressed"
        assert conn.execute("SELECT message_id FROM hydration_jobs WHERE kind='rich_composition'").fetchall() == [(1,)]
        assert queue_snapshots(conn, 100, paused=False)[0]["scan_complete"] is True


def test_v106_seed_failure_rolls_back_schema_and_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "sync.db"
    with monkeypatch.context() as historical:
        historical.setattr(sync_db, "_CURRENT_SCHEMA_VERSION", 105)
        sync_db.ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(42,'synced')")
        conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES(42,1,1)")
        conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at) VALUES(42,1,1777593600)")
        conn.execute(
            "CREATE TRIGGER reject_seed BEFORE INSERT ON hydration_jobs BEGIN SELECT RAISE(ABORT,'seed rejected'); END"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="seed rejected"):
            sync_db._apply_migration_106(conn, 105)
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='rich_dialog_repairs'").fetchone() is None
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone() == (105,)
        assert conn.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)
