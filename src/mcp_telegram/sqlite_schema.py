"""Admission checks for versioned SQLite databases."""

import sqlite3
from typing import cast


def _ledger_version(conn: sqlite3.Connection, current: int) -> int:
    ledger_rows = cast(list[tuple[object, ...]], conn.execute("PRAGMA table_info(schema_version)").fetchall())
    ledger_columns = {row[1] for row in ledger_rows}
    if ledger_columns != {"version", "applied_at"}:
        raise sqlite3.DatabaseError("invalid database schema ledger")
    rows = cast(
        list[tuple[object]], conn.execute("SELECT version FROM schema_version LIMIT ?", (current + 1,)).fetchall()
    )
    versions = [row[0] for row in rows]
    if not versions or any(type(version) is not int for version in versions):
        raise sqlite3.DatabaseError("invalid database schema ledger")
    valid_versions = cast(list[int], versions)
    version = max(valid_versions)
    if len(versions) > current or version > current or sorted(valid_versions) != list(range(1, version + 1)):
        raise sqlite3.DatabaseError("unsupported or incomplete database schema ledger")
    return version


def _validate_columns(conn: sqlite3.Connection, required: dict[str, frozenset[str]]) -> None:
    for table, expected in required.items():
        rows = cast(list[tuple[object, ...]], conn.execute("SELECT * FROM pragma_table_info(?)", (table,)).fetchall())
        columns = {row[1] for row in rows}
        exists = cast(
            tuple[object, ...] | None,
            conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone(),
        )
        if not exists or not expected <= columns:
            raise sqlite3.DatabaseError("unrecognized or incomplete database schema")


def schema_version(
    conn: sqlite3.Connection,
    current: int,
    required_columns: dict[str, frozenset[str]],
    *,
    versioned_columns: dict[int, dict[str, frozenset[str]]] | None = None,
) -> int:
    """Return a valid version, or reject an unknown or incomplete database."""
    if any(conn.execute(f"PRAGMA {marker}").fetchone()[0] for marker in ("application_id", "user_version")):
        raise sqlite3.DatabaseError("unrecognized database markers")
    objects = list(conn.execute("SELECT type, name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"))
    if not objects:
        return 0
    if ("table", "schema_version") not in objects:
        raise sqlite3.DatabaseError("unrecognized or incomplete database schema")
    version = _ledger_version(conn, current)
    _validate_columns(conn, required_columns)
    for minimum, columns in (versioned_columns or {}).items():
        if version >= minimum:
            _validate_columns(conn, columns)
    return version
