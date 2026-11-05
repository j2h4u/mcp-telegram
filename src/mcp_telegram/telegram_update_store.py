"""Bounded SQLite retention for raw Telegram update diagnostics."""

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from .sync_transactions import write_transaction

RETENTION_SECONDS = 900
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
MAX_ROWS = 65_536
MAX_ENTRY_BYTES = 4 * 1024 * 1024
_PREFIX = "raw_telegram_updates."


@dataclass(frozen=True, slots=True)
class UpdateRecord:
    runtime_id: str
    sequence: int
    observed_at_ms: int
    source: str
    constructor: str
    payload_json: str
    payload_bytes: int
    trace_ids_json: str


def _state(conn: sqlite3.Connection, name: str) -> str | None:
    row = cast(
        tuple[str] | None, conn.execute("SELECT value FROM daemon_state WHERE key=?", (_PREFIX + name,)).fetchone()
    )
    return None if row is None else row[0]


def _set_state(conn: sqlite3.Connection, name: str, value: str | int) -> None:
    conn.execute(
        "INSERT INTO daemon_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_PREFIX + name, str(value)),
    )


def _clear(conn: sqlite3.Connection, generation: int, reason: str) -> int:
    conn.execute("DELETE FROM raw_telegram_updates")
    conn.execute("DELETE FROM daemon_state WHERE substr(key,1,?)=?", (len(_PREFIX), _PREFIX))
    _set_state(conn, "generation", generation)
    _set_state(conn, "reset_reason", reason)
    return generation


def bind_verified_account(conn: sqlite3.Connection, account_id: int) -> tuple[int, bool]:
    """Bind a verified account, retaining records only across the same account."""
    if type(account_id) is not int or account_id <= 0:
        raise ValueError("invalid verified account")
    with write_transaction(conn):
        generation = int(_state(conn, "generation") or 0)
        prior = _state(conn, "account_id")
        changed = prior is not None and prior != str(account_id)
        if changed:
            generation = _clear(conn, generation + 1, "account_changed")
        elif generation == 0:
            generation = 1
            _set_state(conn, "generation", generation)
        _set_state(conn, "account_id", account_id)
        return generation, changed


def reset_updates(conn: sqlite3.Connection, reason: str = "logout") -> int:
    """Clear private diagnostics and invalidate every outstanding batch."""
    with write_transaction(conn):
        return _clear(conn, int(_state(conn, "generation") or 0) + 1, reason)


def _is_generation(conn: sqlite3.Connection, expected_generation: int) -> bool:
    return _state(conn, "account_id") is not None and _state(conn, "generation") == str(expected_generation)


def begin_update_run(conn: sqlite3.Connection, *, runtime_id: str, expected_generation: int) -> bool:
    """Start a verified run and retain uncertainty about an unclean prior tail."""
    with write_transaction(conn):
        if not _is_generation(conn, expected_generation):
            return False
        previous = _state(conn, "runtime_id")
        if previous == runtime_id:
            return True
        if previous is not None and _state(conn, "run_open") == "1":
            _add_evictions(conn, "unknown_tail_runs", 1)
        snapshot_prefix = _PREFIX + "snapshot."
        conn.execute("DELETE FROM daemon_state WHERE substr(key,1,?)=?", (len(snapshot_prefix), snapshot_prefix))
        _set_state(conn, "runtime_id", runtime_id)
        _set_state(conn, "run_open", 1)
        return True


def end_update_run(conn: sqlite3.Connection, *, runtime_id: str, expected_generation: int) -> bool:
    """Mark a successfully drained run clean without touching a newer run."""
    with write_transaction(conn):
        if not _is_generation(conn, expected_generation) or _state(conn, "runtime_id") != runtime_id:
            return False
        _set_state(conn, "run_open", 0)
        return True


def _add_evictions(conn: sqlite3.Connection, name: str, count: int) -> None:
    if count:
        _set_state(conn, name, int(_state(conn, name) or 0) + count)


def _prune(conn: sqlite3.Connection, now_ms: int, *, capacity_changed: bool) -> None:
    aged = conn.execute(
        "DELETE FROM raw_telegram_updates WHERE observed_at_ms<=?", (now_ms - RETENTION_SECONDS * 1000,)
    ).rowcount
    _add_evictions(conn, "db_evicted_age", aged)
    if not capacity_changed:
        return
    count, size = cast(
        tuple[int, int],
        conn.execute("SELECT count(*),COALESCE(sum(payload_bytes),0) FROM raw_telegram_updates").fetchone(),
    )
    if count <= MAX_ROWS and size <= MAX_PAYLOAD_BYTES:
        return
    cursor = conn.execute("SELECT id,observed_at_ms,payload_bytes FROM raw_telegram_updates ORDER BY observed_at_ms,id")
    cutoff: tuple[int, int] | None = None
    for row_id, observed_at_ms, payload_bytes in cast(Iterator[tuple[int, int, int]], cursor):
        count -= 1
        size -= payload_bytes
        if count <= MAX_ROWS and size <= MAX_PAYLOAD_BYTES:
            cutoff = observed_at_ms, row_id
            break
    cursor.close()
    assert cutoff is not None
    capacity = conn.execute("DELETE FROM raw_telegram_updates WHERE (observed_at_ms,id)<=(?,?)", cutoff).rowcount
    _add_evictions(conn, "db_evicted_capacity", capacity)


def write_update_batch(  # noqa: PLR0913 - explicit batch and lifecycle boundaries
    conn: sqlite3.Connection,
    records: Sequence[UpdateRecord],
    *,
    now_ms: int,
    counters: Mapping[str, int],
    expected_generation: int,
    runtime_id: str,
) -> bool:
    """Atomically persist a bounded batch under its verified account generation."""
    with write_transaction(conn):
        if (
            not _is_generation(conn, expected_generation)
            or _state(conn, "runtime_id") != runtime_id
            or _state(conn, "run_open") != "1"
        ):
            return False
        if any(item.runtime_id != runtime_id for item in records):
            raise ValueError("batch belongs to a different runtime")
        inserted = conn.executemany(
            "INSERT INTO raw_telegram_updates(runtime_id,sequence,observed_at_ms,source,constructor,"
            "payload_json,payload_bytes,trace_ids_json) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(runtime_id,sequence) DO NOTHING",
            (
                (
                    r.runtime_id,
                    r.sequence,
                    r.observed_at_ms,
                    r.source,
                    r.constructor,
                    r.payload_json,
                    r.payload_bytes,
                    r.trace_ids_json,
                )
                for r in records
            ),
        )
        _prune(conn, now_ms, capacity_changed=inserted.rowcount > 0)
        for key, value in counters.items():
            if key in {"pending_records", "pending_bytes"}:
                _set_state(conn, key, value)
                continue
            previous = int(_state(conn, "snapshot." + key) or 0)
            _add_evictions(conn, "total." + key, max(0, value - previous))
            _set_state(conn, "snapshot." + key, max(previous, value))
        _set_state(conn, "last_flush_ms", now_ms)
        return True
