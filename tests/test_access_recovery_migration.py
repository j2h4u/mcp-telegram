"""Migration keeps access-loss evidence while discarding unwitnessed recovery work."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import cast

import pytest

import mcp_telegram.sync_db as sync_db
from mcp_telegram.delta_sync import AccessProbe, AccessProbePolicy, DeltaAccessProbeDemandAdapter, DeltaSyncWorker
from mcp_telegram.message_history.ports import ForwardGapPagePort
from mcp_telegram.sqlite_writer import SQLiteWriter


def _v95_database(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as historical:
        historical.setattr(sync_db, "_CURRENT_SCHEMA_VERSION", 95)
        sync_db.ensure_sync_schema(path)


def _seed_access_rows(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO synced_dialogs(dialog_id,status,access_lost_at,access_next_revalidate_at,delta_message_id,delta_retry_at) "
        "VALUES (?, 'access_lost', 100, ?, 500, 1400)",
        [(11, 1000), (12, 1500), (13, 1100), (14, None), (15, None), (16, None)],
    )
    conn.executemany(
        "INSERT INTO delta_access_recovery_state VALUES (?, 'gap_fill', 9, 200, ?, 210)",
        [(11, 1200), (12, 1300), (13, 1250), (14, None), (15, 900), (16, 50)],
    )
    conn.executemany(
        "INSERT INTO dialog_fact_observations VALUES (?, ?, ?)",
        [
            (12, "access_state", 9),
            (12, "access_recovery_witness", 8),
            (13, "access_state", 10),
            (13, "access_recovery_witness", 10),
            (14, "access_recovery_witness", 11),
            (15, "access_state", 0),
            (15, "access_recovery_witness", 0),
        ],
    )
    conn.executemany(
        "INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES (?, ?, 300)",
        [(11, 1), (12, 0), (13, 1), (14, 1), (15, 0), (16, 1)],
    )
    conn.execute("INSERT INTO entities(id,type,name,updated_at) VALUES (11,'user','retained',1)")
    conn.execute(
        "INSERT INTO entity_details(entity_id,detail_json,fetched_at,profile_owner_account_id) VALUES (11,'{}',1,77)"
    )
    conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at,text) VALUES (11,500,1,'retained')")
    conn.execute("INSERT INTO daemon_state(key,value) VALUES ('telegram_account_cooldown_until_utc','9999')")
    conn.execute(
        "INSERT INTO update_delivery(singleton,account_id,format,safe_snapshot,pending) VALUES (1,77,1,'{}',?)",
        (b"pending",),
    )
    conn.execute("INSERT INTO update_delivery_tokens(position,observation_order,reaction_sequence) VALUES (1,10,20)")
    conn.execute(
        "INSERT INTO scheduled_operation_receipts(account_id,operation_key,request_fingerprint,action,dialog_id,random_id,outcome,created_at,updated_at) "
        "VALUES (77,'op',?,'create',11,55,'pending',1,2)",
        ("a" * 64,),
    )
    conn.commit()


def _preserved_rows(conn: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    tables = (
        "messages",
        "entities",
        "entity_details",
        "full_history_enrollment",
        "daemon_state",
        "dialog_fact_observations",
        "update_delivery",
        "update_delivery_tokens",
        "scheduled_operation_receipts",
    )
    return {
        table: cast(list[tuple[object, ...]], conn.execute(f"SELECT * FROM {table}").fetchall()) for table in tables
    }


def test_migration_rearms_unwitnessed_access_recovery_without_losing_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sync.db"
    _v95_database(path, monkeypatch)
    conn = sync_db._open_sync_db(path)
    _seed_access_rows(conn)
    preserved = _preserved_rows(conn)
    original_synced = cast(
        list[tuple[object, ...]],
        conn.execute(
            "SELECT dialog_id,status,access_lost_at,access_next_revalidate_at,delta_message_id,delta_retry_at FROM synced_dialogs ORDER BY dialog_id"
        ).fetchall(),
    )
    witnessed = cast(
        tuple[object, ...] | None,
        conn.execute("SELECT * FROM delta_access_recovery_state WHERE dialog_id=13").fetchone(),
    )
    conn.close()
    sync_db.ensure_sync_schema(path)
    conn = sync_db._open_sync_db(path)
    try:
        assert _preserved_rows(conn) == preserved
        assert conn.execute("SELECT * FROM delta_access_recovery_state").fetchall() == [witnessed]
        after = conn.execute(
            "SELECT dialog_id,status,access_lost_at,access_next_revalidate_at,delta_message_id,delta_retry_at FROM synced_dialogs ORDER BY dialog_id"
        ).fetchall()
        assert after == [
            (*row[:3], {11: 1200, 12: 1500, 13: 1100, 14: None, 15: 900, 16: 50}[cast(int, row[0])], *row[4:])
            for row in original_synced
        ]
        worker = DeltaSyncWorker(
            cast(ForwardGapPagePort, object()), conn, asyncio.Event(), writer=cast(SQLiteWriter, object())
        )
        adapter = DeltaAccessProbeDemandAdapter(
            worker, AccessProbePolicy(60, 1, 100), cast(AccessProbe, object()), account_id=77
        )
        selected = adapter.status(0)
        assert selected is not None and selected.selected is not None
        assert selected.selected.key.target == "probe:14" and selected.release_at == 200
        second = adapter.status(0, excluded=frozenset({selected.selected}))
        assert second is not None and second.selected is not None
        assert second.selected.key.target == "probe:16" and second.release_at == 200
        sync_db.ensure_sync_schema(path)
        assert _preserved_rows(conn) == preserved
        assert conn.execute("SELECT * FROM delta_access_recovery_state").fetchall() == [witnessed]
    finally:
        conn.close()


def test_access_recovery_migration_rolls_back_floor_and_row_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sync.db"
    _v95_database(path, monkeypatch)
    conn = sync_db._open_sync_db(path)
    _seed_access_rows(conn)
    before = _preserved_rows(conn)
    recovery = conn.execute("SELECT * FROM delta_access_recovery_state ORDER BY dialog_id").fetchall()
    synced = conn.execute("SELECT * FROM synced_dialogs ORDER BY dialog_id").fetchall()
    conn.execute(
        "CREATE TRIGGER reject_recovery_migration BEFORE DELETE ON delta_access_recovery_state BEGIN SELECT RAISE(ABORT, 'reject migration'); END"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="reject migration"):
        sync_db._apply_migration_96(conn, 95)
    assert _preserved_rows(conn) == before
    assert conn.execute("SELECT * FROM delta_access_recovery_state ORDER BY dialog_id").fetchall() == recovery
    assert conn.execute("SELECT * FROM synced_dialogs ORDER BY dialog_id").fetchall() == synced
    assert conn.execute("SELECT 1 FROM schema_version WHERE version=96").fetchone() is None
    conn.execute("DROP TRIGGER reject_recovery_migration")
    conn.commit()
    assert sync_db._apply_migration_96(conn, 95) == 96
    conn.close()
