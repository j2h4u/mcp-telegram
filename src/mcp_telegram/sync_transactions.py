"""Synchronous, explicitly owned SQLite transactions for the sync database."""

import logging
import sqlite3
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import count
from types import FrameType
from typing import cast


class _WriteOwners(threading.local):
    def __init__(self) -> None:
        self.connections: set[sqlite3.Connection] = set()


_owners = _WriteOwners()
_savepoint_ids = count()
_SLOW_WRITE_SECONDS = 1.0
_INTERNAL_MODULES = frozenset({"contextlib", __name__})
logger = logging.getLogger(__name__)


def _caller_identity() -> tuple[str, str, int]:
    """Capture only the first caller outside this module and contextlib."""
    frame: FrameType | None = sys._getframe()
    while frame is not None and frame.f_globals.get("__name__") in _INTERNAL_MODULES:
        frame = frame.f_back
    if frame is None:
        return "<unknown>", "<unknown>", 0
    try:
        return frame.f_globals.get("__name__", "<unknown>"), frame.f_code.co_name, frame.f_lineno
    finally:
        del frame


def _log_transaction_event(  # noqa: PLR0913
    event: str,
    stage: str,
    wait_seconds: float,
    held_seconds: float,
    *,
    error: BaseException | None = None,
    level: int = logging.WARNING,
    body_seconds: float = 0.0,
    commit_seconds: float = 0.0,
) -> None:
    """Emit content-free transaction diagnostics without changing write semantics."""
    try:
        caller = _caller_identity()
        logger.log(
            level,
            (
                "%s stage=%s caller_module=%s caller_function=%s caller_line=%d "
                "thread_name=%s thread_native_id=%s wait_seconds=%.3f held_seconds=%.3f "
                "body_seconds=%.3f commit_seconds=%.3f "
                "error_type=%s sqlite_errorcode=%s sqlite_errorname=%s"
            ),
            event,
            stage,
            caller[0],
            caller[1],
            caller[2],
            threading.current_thread().name,
            threading.get_native_id(),
            wait_seconds,
            held_seconds,
            body_seconds,
            commit_seconds,
            None if error is None else type(error).__name__,
            getattr(error, "sqlite_errorcode", None),
            getattr(error, "sqlite_errorname", None),
        )
    except BaseException:  # noqa: BLE001 - diagnostics must not replace a transaction error
        return


def _is_fast_busy(error: BaseException, wait_seconds: float, held_seconds: float) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return code == sqlite3.SQLITE_BUSY and wait_seconds < _SLOW_WRITE_SECONDS and held_seconds < _SLOW_WRITE_SECONDS


def _elapsed(started_at: float | None) -> float:
    return 0.0 if started_at is None else max(0.0, time.monotonic() - started_at)


def _transaction_timings(wait_started: float, held_started: float | None) -> tuple[float, float]:
    wait_anchor = held_started if held_started is not None else time.monotonic()
    return max(0.0, wait_anchor - wait_started), _elapsed(held_started)


def _read_query_only(conn: sqlite3.Connection, wait_started: float) -> bool:
    try:
        return bool(cast(tuple[int], conn.execute("PRAGMA query_only").fetchone())[0])
    except BaseException as error:
        if isinstance(error, sqlite3.Error):
            _log_failure("guard_read", _elapsed(wait_started), 0.0, error)
        raise


def _log_failure(  # noqa: PLR0913, PLR0917
    stage: str,
    wait_seconds: float,
    held_seconds: float,
    error: BaseException,
    body_seconds: float = 0.0,
    commit_seconds: float = 0.0,
) -> None:
    if isinstance(error, sqlite3.Error):
        _log_transaction_event(
            "sqlite_write_failure",
            stage,
            wait_seconds,
            held_seconds,
            error=error,
            level=logging.DEBUG if _is_fast_busy(error, wait_seconds, held_seconds) else logging.WARNING,
            body_seconds=body_seconds,
            commit_seconds=commit_seconds,
        )
    elif (
        wait_seconds >= _SLOW_WRITE_SECONDS
        or held_seconds >= _SLOW_WRITE_SECONDS
        or body_seconds >= _SLOW_WRITE_SECONDS
        or commit_seconds >= _SLOW_WRITE_SECONDS
    ):
        _log_transaction_event(
            "sqlite_write_slow",
            stage,
            wait_seconds,
            held_seconds,
            error=error,
            body_seconds=body_seconds,
            commit_seconds=commit_seconds,
        )


def _log_success_timings(wait_seconds: float, held_seconds: float, body_seconds: float, commit_seconds: float) -> None:
    if wait_seconds >= _SLOW_WRITE_SECONDS:
        _log_transaction_event(
            "sqlite_write_slow",
            "begin",
            wait_seconds,
            held_seconds,
            body_seconds=body_seconds,
            commit_seconds=commit_seconds,
        )
    if body_seconds >= _SLOW_WRITE_SECONDS:
        _log_transaction_event(
            "sqlite_write_slow",
            "body",
            wait_seconds,
            held_seconds,
            body_seconds=body_seconds,
            commit_seconds=commit_seconds,
        )
    if commit_seconds >= _SLOW_WRITE_SECONDS:
        _log_transaction_event(
            "sqlite_write_slow",
            "commit",
            wait_seconds,
            held_seconds,
            body_seconds=body_seconds,
            commit_seconds=commit_seconds,
        )


def _rollback_after_failure(
    conn: sqlite3.Connection,
) -> BaseException | None:
    try:
        conn.rollback()
    except BaseException as rollback_error:  # noqa: BLE001 - preserve the primary failure below
        return rollback_error
    return None


def _restore_guard(  # noqa: PLR0913, PLR0917
    conn: sqlite3.Connection,
    query_only: bool,
    primary: BaseException | None,
    wait_seconds: float,
    held_seconds: float,
    body_seconds: float,
    commit_seconds: float,
) -> None:
    try:
        conn.execute(f"PRAGMA query_only = {int(query_only)}")
    except BaseException as restore_error:
        _log_failure("guard_restore", wait_seconds, held_seconds, restore_error, body_seconds, commit_seconds)
        if primary is not None:
            primary.add_note(f"Restoring SQLite query_only failed: {restore_error!r}")
        _retire_connection(conn, primary or restore_error)
        if primary is None:
            raise


def enable_runtime_writes(conn: sqlite3.Connection) -> None:
    """After bootstrap, forbid idle writes and use explicit transaction control."""
    if conn.in_transaction or conn in _owners.connections:
        raise RuntimeError("Cannot enable runtime writes during a transaction")
    conn.isolation_level = None
    conn.execute("PRAGMA query_only = ON")


def require_write_transaction(conn: sqlite3.Connection) -> None:
    """Require a write unit owned by this thread, including its savepoints."""
    if conn not in _owners.connections or not conn.in_transaction:
        raise RuntimeError("An owned write_transaction is required")


def _retire_connection(conn: sqlite3.Connection, primary: BaseException) -> None:
    try:
        conn.close()
    except BaseException as close_error:
        primary.add_note(f"Closing the unsafe SQLite connection failed: {close_error!r}")
        raise primary from close_error


@contextmanager
def read_snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    """Keep a local read snapshot without taking ownership of a caller transaction."""
    began = not conn.in_transaction
    if began:
        conn.execute("BEGIN")
    try:
        yield
    finally:
        if began:
            conn.rollback()


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[None]:  # noqa: PLR0915
    """Reserve the writer before reading; never await inside this write unit.

    Existing transactions belong to their caller and are rejected untouched.
    Cleanup restores the original query_only state, including raw test fixtures.
    """
    if conn.in_transaction or conn in _owners.connections:
        raise RuntimeError("write_transaction requires an idle connection")
    wait_started = time.monotonic()
    query_only = _read_query_only(conn, wait_started)
    primary: BaseException | None = None
    retired, stage, held_started = False, "guard_disable", None
    body_seconds = commit_seconds = 0.0
    try:
        conn.execute("PRAGMA query_only = OFF")
        stage = "begin"
        conn.execute("BEGIN IMMEDIATE")
        held_started = time.monotonic()
        _owners.connections.add(conn)
        wait_seconds = max(0.0, held_started - wait_started)
        stage = "body"
        yield
        body_seconds = _elapsed(held_started)
        stage = "guard_check"
        require_write_transaction(conn)
        stage = "commit"
        commit_started = time.monotonic()
        try:
            conn.commit()
        finally:
            commit_finished = time.monotonic()
            commit_seconds = max(0.0, commit_finished - commit_started)
        held_seconds = max(0.0, commit_finished - cast(float, held_started))
        _log_success_timings(wait_seconds, held_seconds, body_seconds, commit_seconds)
    except BaseException as error:
        primary = error
        if stage == "body" and held_started is not None:
            body_seconds = _elapsed(held_started)
        wait_seconds = _transaction_timings(wait_started, held_started)[0]
        rollback_error = _rollback_after_failure(conn)
        held_seconds = _elapsed(held_started)
        if rollback_error is not None:
            retired = True
            error.add_note(f"SQLite rollback failed: {rollback_error!r}")
            _retire_connection(conn, error)
            held_seconds = _elapsed(held_started)
            _log_failure(stage, wait_seconds, held_seconds, error, body_seconds, commit_seconds)
            _log_failure("rollback", wait_seconds, held_seconds, rollback_error, body_seconds, commit_seconds)
            raise error from rollback_error
        _log_failure(stage, wait_seconds, held_seconds, error, body_seconds, commit_seconds)
        raise
    finally:
        _owners.connections.discard(conn)
        if not retired:
            _restore_guard(
                conn,
                query_only,
                primary,
                *_transaction_timings(wait_started, held_started),
                body_seconds,
                commit_seconds,
            )


@contextmanager
def write_savepoint(conn: sqlite3.Connection) -> Iterator[None]:
    """Join an owned write unit with a savepoint, or start a strict write unit."""
    if conn not in _owners.connections:
        with write_transaction(conn):
            yield
        return
    require_write_transaction(conn)
    name = f"write_unit_{next(_savepoint_ids)}"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
        require_write_transaction(conn)
    except BaseException as error:
        try:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
        except BaseException as cleanup_error:
            error.add_note(f"SQLite savepoint cleanup failed: {cleanup_error!r}")
            _retire_connection(conn, error)
            raise error from cleanup_error
        raise
    else:
        try:
            conn.execute(f"RELEASE {name}")
        except BaseException as release_error:
            _retire_connection(conn, release_error)
            raise
