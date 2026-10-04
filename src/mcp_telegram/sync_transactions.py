"""Synchronous, explicitly owned SQLite write units for the sync database."""

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import count
from typing import cast


class _WriteOwners(threading.local):
    def __init__(self) -> None:
        self.connections: set[sqlite3.Connection] = set()


_owners = _WriteOwners()
_savepoint_ids = count()


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
def write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Reserve the writer before reading; never await inside this write unit.

    Existing transactions belong to their caller and are rejected untouched.
    Cleanup restores the original query_only state, including raw test fixtures.
    """
    if conn.in_transaction or conn in _owners.connections:
        raise RuntimeError("write_transaction requires an idle connection")
    query_only = bool(cast(tuple[int], conn.execute("PRAGMA query_only").fetchone())[0])
    primary: BaseException | None = None
    retired = False
    try:
        conn.execute("PRAGMA query_only = OFF")
        conn.execute("BEGIN IMMEDIATE")
        _owners.connections.add(conn)
        yield
        require_write_transaction(conn)
        conn.commit()
    except BaseException as error:
        primary = error
        try:
            conn.rollback()
        except BaseException as rollback_error:
            error.add_note(f"SQLite rollback failed: {rollback_error!r}")
            retired = True
            _retire_connection(conn, error)
            raise error from rollback_error
        raise
    finally:
        _owners.connections.discard(conn)
        if not retired:
            try:
                conn.execute(f"PRAGMA query_only = {int(query_only)}")
            except BaseException as restore_error:
                if primary is not None:
                    primary.add_note(f"Restoring SQLite query_only failed: {restore_error!r}")
                _retire_connection(conn, primary or restore_error)
                if primary is None:
                    raise


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
