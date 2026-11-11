import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from mcp_telegram import sync_db
from mcp_telegram import telegram_update_store as store
from mcp_telegram.sync_transactions import write_transaction


def _record(sequence: int, when: int, payload: object, *, runtime: str = "one") -> store.UpdateRecord:
    value = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    return store.UpdateRecord(runtime, sequence, when, "push", "UpdateUser", value, len(value.encode()), "[]")


def test_upgrade_and_persistent_json_account_fences(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "sync.db"
    with monkeypatch.context() as old:
        old.setattr(sync_db, "_CURRENT_SCHEMA_VERSION", 99)
        sync_db.ensure_sync_schema(path)
    sync_db.ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT max(version) FROM schema_version").fetchone() == (sync_db._CURRENT_SCHEMA_VERSION,)
        generation, changed = store.bind_verified_account(conn, 42)
        assert (generation, changed) == (1, False)
        assert store.begin_update_run(conn, runtime_id="one", expected_generation=generation)
        item = _record(1, 1_000_000, {"_": "UpdateUser", "value": "data"})
        assert store.write_update_batch(
            conn,
            [item],
            now_ms=1_000_000,
            counters={"serialization_failures": 2},
            runtime_id="one",
            expected_generation=generation,
        )
        assert store.write_update_batch(
            conn,
            [item],
            now_ms=1_000_000,
            counters={"serialization_failures": 2},
            runtime_id="one",
            expected_generation=generation,
        )
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute(
            "SELECT count(*),json_extract(payload_json,'$._') FROM raw_telegram_updates"
        ).fetchone() == (1, "UpdateUser")
        assert store.bind_verified_account(conn, 42) == (1, False)
        assert store.bind_verified_account(conn, 43) == (2, True)
        assert conn.execute("SELECT count(*) FROM raw_telegram_updates").fetchone() == (0,)
        assert store.begin_update_run(conn, runtime_id="one", expected_generation=2)
        assert not store.write_update_batch(
            conn, [item], now_ms=1_000_001, counters={}, runtime_id="one", expected_generation=1
        )
        assert store.write_update_batch(
            conn, [item], now_ms=1_000_001, counters={}, runtime_id="one", expected_generation=2
        )
        assert store.reset_updates(conn) == 3
        assert not store.write_update_batch(
            conn, [item], now_ms=1_000_001, counters={}, runtime_id="one", expected_generation=2
        )
        assert conn.execute("SELECT count(*) FROM raw_telegram_updates").fetchone() == (0,)
        assert store.bind_verified_account(conn, 43) == (3, False)


def test_capacity_totals_survive_reopen_duplicates_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "totals.db"
    sync_db.ensure_sync_schema(path)
    monkeypatch.setattr(store, "MAX_ROWS", 2)
    first, second, third = (_record(i, i, "x" * i) for i in range(1, 4))
    with closing(sqlite3.connect(path)) as conn:
        generation, _ = store.bind_verified_account(conn, 42)
        assert store.begin_update_run(conn, runtime_id="one", expected_generation=generation)
        conn.execute(
            "INSERT INTO raw_telegram_updates(runtime_id,sequence,observed_at_ms,source,constructor,"
            "payload_json,payload_bytes,trace_ids_json) VALUES(?,?,?,?,?,?,?,?)",
            (
                first.runtime_id,
                first.sequence,
                first.observed_at_ms,
                first.source,
                first.constructor,
                first.payload_json,
                first.payload_bytes,
                first.trace_ids_json,
            ),
        )
        conn.commit()
        assert store.write_update_batch(
            conn, [first, second], now_ms=3, counters={}, expected_generation=generation, runtime_id="one"
        )
    with closing(sqlite3.connect(path)) as conn:
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        with pytest.raises(RuntimeError), write_transaction(conn):
            assert store.write_update_batch(
                conn, [third], now_ms=4, counters={}, expected_generation=generation, runtime_id="one"
            )
            raise RuntimeError("rollback")
        assert store.write_update_batch(
            conn, [third], now_ms=4, counters={}, expected_generation=generation, runtime_id="one"
        )
        conn.set_trace_callback(None)
        assert not any("SELECT count(*),COALESCE(sum(payload_bytes)" in sql for sql in statements)
        assert conn.execute("SELECT sequence FROM raw_telegram_updates ORDER BY sequence").fetchall() == [(2,), (3,)]
        assert store._capacity_totals(conn) == (2, second.payload_bytes + third.payload_bytes)
        assert store.bind_verified_account(conn, 43) == (2, True)
        assert store._capacity_totals(conn) == (0, 0)


def test_prunes_oldest_whole_rows_by_bytes_and_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "sync.db"
    sync_db.ensure_sync_schema(path)
    monkeypatch.setattr(store, "MAX_ROWS", 2)
    monkeypatch.setattr(store, "MAX_PAYLOAD_BYTES", 17)
    with closing(sqlite3.connect(path)) as conn:
        generation, _ = store.bind_verified_account(conn, 42)
        assert store.begin_update_run(conn, runtime_id="one", expected_generation=generation)
        records = [
            _record(1, 99_999, "old"),
            _record(2, 1_000_000, "aaa"),
            _record(3, 1_000_002, "bbb"),
            _record(4, 1_000_002, "ccc"),
        ]
        assert store.write_update_batch(
            conn, records, now_ms=1_000_002, counters={}, runtime_id="one", expected_generation=generation
        )
        assert conn.execute("SELECT sequence FROM raw_telegram_updates ORDER BY sequence").fetchall() == [(3,), (4,)]
        assert conn.execute(
            "SELECT value FROM daemon_state WHERE key='raw_telegram_updates.db_evicted_capacity'"
        ).fetchone() == ("2",)
        monkeypatch.setattr(store, "MAX_PAYLOAD_BYTES", 5)
        assert store.write_update_batch(
            conn,
            [_record(5, 1_000_002, "ddd")],
            now_ms=1_000_002,
            counters={},
            runtime_id="one",
            expected_generation=generation,
        )
        assert conn.execute("SELECT sequence FROM raw_telegram_updates").fetchall() == [(5,)]
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        assert store.write_update_batch(
            conn, [], now_ms=1_900_002, counters={}, runtime_id="one", expected_generation=generation
        )
        conn.set_trace_callback(None)
        assert conn.execute("SELECT sequence FROM raw_telegram_updates").fetchall() == [(5,)]
        assert not any("SELECT count(*),COALESCE(sum(payload_bytes)" in sql for sql in statements)


def test_old_rows_survive_idle_flush_new_write_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "retained.db"
    sync_db.ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        generation, _ = store.bind_verified_account(conn, 42)
        assert store.begin_update_run(conn, runtime_id="one", expected_generation=generation)
        assert store.write_update_batch(
            conn,
            [_record(1, 1_000, "old")],
            now_ms=1_000,
            counters={},
            runtime_id="one",
            expected_generation=generation,
        )
        assert store.write_update_batch(
            conn, [], now_ms=1_801_000, counters={}, runtime_id="one", expected_generation=generation
        )
        assert store.write_update_batch(
            conn,
            [_record(2, 1_801_000, "new")],
            now_ms=1_801_000,
            counters={},
            runtime_id="one",
            expected_generation=generation,
        )
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT sequence FROM raw_telegram_updates ORDER BY sequence").fetchall() == [(1,), (2,)]


def test_failed_batch_rolls_back_rows_and_metadata_then_retries(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    sync_db.ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        generation, _ = store.bind_verified_account(conn, 42)
        assert store.begin_update_run(conn, runtime_id="one", expected_generation=generation)
        with write_transaction(conn):
            conn.execute(
                "CREATE TRIGGER reject_second BEFORE INSERT ON raw_telegram_updates WHEN NEW.sequence=2 BEGIN SELECT RAISE(ABORT,'failure'); END"
            )
        batch = [_record(1, 100, "one"), _record(2, 100, "two")]
        with pytest.raises(sqlite3.IntegrityError):
            store.write_update_batch(
                conn,
                batch,
                now_ms=100,
                counters={"serialization_failures": 1},
                runtime_id="one",
                expected_generation=generation,
            )
        assert conn.execute("SELECT count(*) FROM raw_telegram_updates").fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM daemon_state WHERE key='raw_telegram_updates.last_flush_ms'"
        ).fetchone() == (0,)
        with write_transaction(conn):
            conn.execute("DROP TRIGGER reject_second")
        assert store.write_update_batch(
            conn, batch, now_ms=100, counters={}, runtime_id="one", expected_generation=generation
        )
        assert conn.execute("SELECT count(*) FROM raw_telegram_updates").fetchone() == (2,)


def test_storage_rejects_invalid_json_and_oversized_payload(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    sync_db.ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        generation, _ = store.bind_verified_account(conn, 42)
        assert store.begin_update_run(conn, runtime_id="one", expected_generation=generation)
        invalid = store.UpdateRecord("one", 1, 100, "push", "UpdateUser", "{", 1, "[]")
        oversized = _record(2, 100, "x" * store.MAX_ENTRY_BYTES)
        for item in (invalid, oversized):
            with pytest.raises(sqlite3.IntegrityError):
                store.write_update_batch(
                    conn, [item], now_ms=100, counters={}, runtime_id="one", expected_generation=generation
                )
        assert conn.execute("SELECT count(*) FROM raw_telegram_updates").fetchone() == (0,)


def test_restart_preserves_loss_totals_and_records_unknown_tail(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    sync_db.ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        generation, _ = store.bind_verified_account(conn, 42)
        assert store.begin_update_run(conn, runtime_id="one", expected_generation=generation)
        counters = {"serialization_failures": 2, "pending_records": 3, "pending_bytes": 40}
        for _ in range(2):
            assert store.write_update_batch(
                conn, [], now_ms=100, counters=counters, expected_generation=generation, runtime_id="one"
            )
        assert store.begin_update_run(conn, runtime_id="two", expected_generation=generation)
        assert store.begin_update_run(conn, runtime_id="two", expected_generation=generation)
        assert not store.end_update_run(conn, runtime_id="one", expected_generation=generation)
        assert store.write_update_batch(
            conn,
            [],
            now_ms=101,
            counters={"serialization_failures": 1, "pending_records": 0, "pending_bytes": 0},
            expected_generation=generation,
            runtime_id="two",
        )
        values = dict(conn.execute("SELECT key,value FROM daemon_state WHERE key LIKE 'raw_telegram_updates.%'"))
        assert values["raw_telegram_updates.total.serialization_failures"] == "3"
        assert values["raw_telegram_updates.unknown_tail_runs"] == "1"
        assert values["raw_telegram_updates.pending_records"] == "0"
        assert values["raw_telegram_updates.pending_bytes"] == "0"
        assert store.end_update_run(conn, runtime_id="two", expected_generation=generation)
        assert store.begin_update_run(conn, runtime_id="three", expected_generation=generation)
        assert conn.execute(
            "SELECT value FROM daemon_state WHERE key='raw_telegram_updates.unknown_tail_runs'"
        ).fetchone() == ("1",)
