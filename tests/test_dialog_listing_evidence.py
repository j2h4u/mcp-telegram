"""Completed directory listing evidence stays separate from retained facts."""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest

import mcp_telegram.sync_db as sync_db
from mcp_telegram.dialog_availability import read_dialog_availabilities
from mcp_telegram.dialog_directory import _DirectoryState, _publish_directory_absence
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction


def test_completed_listing_retains_facts_and_respects_revision_fence(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE dialog_directory_state SET account_id=10")
    conn.executemany(
        "INSERT INTO dialogs(dialog_id,name,archived,snapshot_at) VALUES (?,?,1,12)", [(1, "old"), (2, "new")]
    )
    conn.execute("INSERT INTO dialog_directory_facts VALUES(1,'group',1,0,NULL,12)")
    conn.execute(
        "INSERT INTO folder_peer_references(account_id,dialog_id,kind,observation_order) VALUES (10,3,'unknown',1)"
    )
    conn.executemany(
        "INSERT INTO dialog_directory_baseline(generation,dialog_id,baseline_revision,seen,baseline_exists) VALUES(1,?,?,0,?)",
        [(1, 0, 1), (2, 0, 1), (3, 0, 0)],
    )
    conn.execute("UPDATE dialogs SET name='concurrent',revision=1 WHERE dialog_id=2")
    conn.commit()
    state = _DirectoryState(1, "in_progress", "complete", "complete", "complete", None, 100, None, 10, None, None)
    with write_transaction(conn):
        _publish_directory_absence(conn, state, 1)
    assert conn.execute("SELECT name,archived,snapshot_at FROM dialogs WHERE dialog_id=1").fetchone() == ("old", 1, 12)
    assert conn.execute("SELECT category,observed_at FROM dialog_directory_facts WHERE dialog_id=1").fetchone() == (
        "group",
        12,
    )
    result = read_dialog_availabilities(conn, 10, [1, 2, 3])
    assert result[3]["acquisition"] == {"state": "pending", "source": None, "observed_at": None, "error_code": None}
    assert [result[n]["listing"]["state"] for n in (1, 2, 3)] == ["not_returned", "not_yet_observed", "not_returned"]
    assert read_dialog_availabilities(conn, 11, [1])[1]["listing"]["state"] == "not_yet_observed"
    conn.close()


def test_access_evidence_is_account_scoped_and_new_episode_retires_old_cause(tmp_path: Path) -> None:
    from mcp_telegram.access_lifecycle import AccessLossEvidence, restore_access_after_revalidation, set_access_lost

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE dialog_directory_state SET account_id=10")
    conn.execute("INSERT INTO dialogs(dialog_id,name,snapshot_at) VALUES(1,'retained',1)")
    conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(1,'synced')")
    conn.commit()
    set_access_lost(conn, 1, 100, reason="Forbidden")
    set_access_lost(conn, 1, 110, evidence=AccessLossEvidence("Removed", "removed_by_admin", 2))
    result = read_dialog_availabilities(conn, 10, [1])[1]
    assert result["access"] == {
        "state": "access_lost",
        "scope": "membership",
        "reason_code": "Removed",
        "cause": "removed_by_admin",
        "observed_at": 110,
    }
    set_access_lost(conn, 1, 115, reason="HistoryDenied", access_scope="history")
    assert read_dialog_availabilities(conn, 10, [1])[1]["access"] == result["access"]
    assert read_dialog_availabilities(conn, 11, [1])[1]["access"]["state"] == "unknown"
    restore_access_after_revalidation(conn, 1, 120)
    assert read_dialog_availabilities(conn, 10, [1])[1]["access"]["state"] == "unknown"
    set_access_lost(conn, 1, 130, reason="Forbidden")
    assert read_dialog_availabilities(conn, 10, [1])[1]["access"]["cause"] is None
    conn.close()


def test_migration_does_not_turn_legacy_hidden_into_listing_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sync.db"
    with monkeypatch.context() as historical:
        historical.setattr(sync_db, "_CURRENT_SCHEMA_VERSION", 103)
        ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("INSERT INTO dialogs(dialog_id,name,hidden) VALUES(1,'retained',1)")
        conn.executemany(
            "INSERT INTO synced_dialogs(dialog_id,status,access_lost_at,access_next_revalidate_at) VALUES (?, 'access_lost', 10, 9999999999)",
            [(2,), (3,), (4,), (5,)],
        )
        conn.execute(
            "INSERT INTO conversation_history_events(kind,occurred_at,time_basis,dialog_id,access_change_cause) VALUES ('access_lost',10,'observed',3,'removed_by_admin')"
        )
        conn.execute("UPDATE dialog_directory_state SET account_id=10")
        conn.execute("UPDATE synced_dialogs SET access_lost_at=20 WHERE dialog_id=5")
        conn.execute(
            "INSERT INTO conversation_history_events(kind,occurred_at,time_basis,dialog_id,reason_code,access_change_cause) VALUES ('access_lost',10,'observed',5,'Removed','removed_by_admin')"
        )
        conn.execute(
            "INSERT INTO conversation_history_events(kind,occurred_at,time_basis,dialog_id,reason_code) VALUES ('access_lost',20,'observed',5,'LaterDenial')"
        )
        conn.executemany(
            "INSERT INTO dialog_fact_observations(dialog_id,field,observation_order) VALUES (5,?,?)",
            [("access_state", 3), ("access_recovery_witness", 2)],
        )
        conn.executemany(
            "INSERT INTO dialog_fact_observations(dialog_id,field,observation_order) VALUES (4,?,7)",
            [("access_state",), ("access_recovery_witness",)],
        )
        conn.commit()
    ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT dialog_id,name FROM dialogs").fetchall() == [(1, "retained")]
        assert conn.execute("SELECT * FROM dialog_listing_observations").fetchall() == []
        columns = cast(
            list[tuple[int, str, str, int, object, int]], conn.execute("PRAGMA table_info(dialogs)").fetchall()
        )
        assert "hidden" not in {row[1] for row in columns}
        rows = cast(
            list[tuple[int, str, str | None, int]],
            conn.execute(
                "SELECT dialog_id,status,access_loss_scope,access_next_revalidate_at FROM synced_dialogs ORDER BY dialog_id"
            ).fetchall(),
        )
        assert rows[0][0:3] == (2, "access_lost", None) and rows[0][3] < 9999999999
        assert rows[1] == (3, "access_lost", "membership", 9999999999)
        assert rows[2] == (4, "access_lost", None, 9999999999)
        assert rows[3][0:3] == (5, "access_lost", None) and rows[3][3] < 9999999999
        assert read_dialog_availabilities(conn, 10, [5])[5]["access"] == {
            "state": "access_lost",
            "scope": "unknown",
            "reason_code": "LaterDenial",
            "cause": None,
            "observed_at": 20,
        }


async def test_listing_witness_is_atomic_across_writer_and_reader_snapshot(tmp_path: Path) -> None:
    from mcp_telegram.sqlite_writer import SQLiteWriter

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE dialog_directory_state SET account_id=10")
    conn.execute("INSERT INTO dialogs(dialog_id,name,snapshot_at) VALUES(1,'retained',12)")
    conn.execute("INSERT INTO dialog_directory_baseline(generation,dialog_id,baseline_revision) VALUES(1,1,0)")
    conn.commit()
    conn.execute("BEGIN")
    assert read_dialog_availabilities(conn, 10, [1])[1]["listing"]["state"] == "not_yet_observed"
    writer = await SQLiteWriter.open(path)
    state = _DirectoryState(1, "in_progress", "complete", "complete", "complete", None, 100, None, 10, None, None)

    def publish(db: sqlite3.Connection) -> None:
        with write_transaction(db):
            _publish_directory_absence(db, state, 1)

    try:
        await writer.run(publish)
        assert read_dialog_availabilities(conn, 10, [1])[1]["listing"]["state"] == "not_yet_observed"
        conn.rollback()
        assert read_dialog_availabilities(conn, 10, [1])[1]["listing"]["state"] == "not_returned"
        assert conn.execute("SELECT name,snapshot_at FROM dialogs WHERE dialog_id=1").fetchone() == ("retained", 12)
    finally:
        conn.close()
        await writer.aclose()


def test_recovery_of_reference_only_peer_does_not_invent_dialog_facts(tmp_path: Path) -> None:
    from mcp_telegram.access_lifecycle import restore_access_after_revalidation

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(3,'access_lost')")
    conn.execute(
        "INSERT INTO folder_peer_references(account_id,dialog_id,kind,observation_order) VALUES(10,3,'unknown',1)"
    )
    conn.commit()
    restore_access_after_revalidation(conn, 3, 10)
    assert conn.execute("SELECT 1 FROM dialogs WHERE dialog_id=3").fetchone() is None
    conn.close()


def test_new_denial_after_recovery_witness_starts_new_episode(tmp_path: Path) -> None:
    from mcp_telegram.access_lifecycle import AccessLossEvidence, record_access_recovery_observation, set_access_lost

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE dialog_directory_state SET account_id=10")
    conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(1,'synced')")
    conn.commit()
    set_access_lost(
        conn,
        1,
        100,
        evidence=AccessLossEvidence("Removed", "removed_by_admin", 2),
        profile_owner_account_id=10,
        observation_order=1,
    )
    assert record_access_recovery_observation(conn, 1, 2)
    assert conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id=1").fetchone() == ("access_lost",)
    set_access_lost(
        conn,
        1,
        120,
        reason="ChannelPrivateError",
        access_scope="history",
        profile_owner_account_id=10,
        observation_order=3,
    )
    assert read_dialog_availabilities(conn, 10, [1])[1]["access"] == {
        "state": "access_lost",
        "scope": "history",
        "reason_code": "ChannelPrivateError",
        "cause": None,
        "observed_at": 120,
    }
    conn.close()
