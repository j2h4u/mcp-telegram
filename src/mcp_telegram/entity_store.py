"""Canonical SQLite persistence boundary for entity snapshots."""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EntitySnapshot:
    """Complete entity identity values supplied by a runtime writer."""

    entity_id: int
    entity_type: str
    name: str | None
    username: str | None
    name_normalized: str | None
    updated_at: int


_UPSERT_ENTITY_SQL = (
    "INSERT INTO entities (id, type, name, username, name_normalized, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(id) DO UPDATE SET "
    "type = excluded.type, "
    "name = excluded.name, "
    "username = excluded.username, "
    "name_normalized = excluded.name_normalized, "
    "updated_at = excluded.updated_at"
)
_INSERT_ENTITY_STUB_SQL = (
    "INSERT OR IGNORE INTO entities (id, type, name, username, name_normalized, updated_at) VALUES (?, ?, ?, ?, ?, ?)"
)


def _snapshot_values(snapshot: EntitySnapshot) -> tuple[int, str, str | None, str | None, str | None, int]:
    return (
        snapshot.entity_id,
        snapshot.entity_type,
        snapshot.name,
        snapshot.username,
        snapshot.name_normalized,
        snapshot.updated_at,
    )


def upsert_entity_snapshots(conn: sqlite3.Connection, snapshots: Sequence[EntitySnapshot]) -> None:
    """Insert or update complete snapshots without owning the transaction."""
    if not snapshots:
        return
    conn.executemany(_UPSERT_ENTITY_SQL, (_snapshot_values(snapshot) for snapshot in snapshots))


def ensure_entity_stub(conn: sqlite3.Connection, snapshot: EntitySnapshot) -> None:
    """Insert a missing parent entity without changing an existing row."""
    conn.execute(_INSERT_ENTITY_STUB_SQL, _snapshot_values(snapshot))
