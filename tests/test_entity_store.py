"""Contract tests for the canonical entity persistence owner."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import FrozenInstanceError, fields, is_dataclass
from typing import cast

import pytest

from mcp_telegram.entity_store import EntitySnapshot, ensure_entity_stub, upsert_entity_snapshots

EntityRow = tuple[int, str, str | None, str | None, str | None, int]
DetailRow = tuple[int, str, int]


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY,
            type TEXT NOT NULL,
            name TEXT,
            username TEXT,
            name_normalized TEXT,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE entity_details (
            entity_id INTEGER PRIMARY KEY,
            detail_json TEXT NOT NULL,
            fetched_at INTEGER NOT NULL,
            FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
        ) WITHOUT ROWID;
        """
    )
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


def _entity_row(connection: sqlite3.Connection, entity_id: int) -> EntityRow | None:
    return cast(
        EntityRow | None,
        connection.execute(
            "SELECT id, type, name, username, name_normalized, updated_at FROM entities WHERE id = ?",
            (entity_id,),
        ).fetchone(),
    )


def _detail_row(connection: sqlite3.Connection, entity_id: int) -> DetailRow | None:
    return cast(
        DetailRow | None,
        connection.execute(
            "SELECT entity_id, detail_json, fetched_at FROM entity_details WHERE entity_id = ?",
            (entity_id,),
        ).fetchone(),
    )


def _insert_entity(connection: sqlite3.Connection, snapshot: EntitySnapshot) -> None:
    connection.execute(
        """
        INSERT INTO entities (id, type, name, username, name_normalized, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot.entity_id,
            snapshot.entity_type,
            snapshot.name,
            snapshot.username,
            snapshot.name_normalized,
            snapshot.updated_at,
        ),
    )


def test_entity_snapshot_is_frozen_slots_value() -> None:
    snapshot = EntitySnapshot(
        entity_id=1,
        entity_type="User",
        name="Alice",
        username="alice",
        name_normalized="alice",
        updated_at=100,
    )

    assert is_dataclass(EntitySnapshot)
    assert [field.name for field in fields(EntitySnapshot)] == [
        "entity_id",
        "entity_type",
        "name",
        "username",
        "name_normalized",
        "updated_at",
    ]
    assert not hasattr(snapshot, "__dict__")
    with pytest.raises(FrozenInstanceError):
        snapshot.name = "Changed"  # type: ignore[misc]


def test_upsert_entity_snapshots_inserts_complete_snapshot(conn: sqlite3.Connection) -> None:
    snapshot = EntitySnapshot(
        entity_id=10,
        entity_type="User",
        name="Alice",
        username="alice",
        name_normalized="alisa",
        updated_at=101,
    )

    upsert_entity_snapshots(conn, [snapshot])

    assert _entity_row(conn, 10) == (10, "User", "Alice", "alice", "alisa", 101)


def test_upsert_conflict_updates_every_mutable_field_including_none(conn: sqlite3.Connection) -> None:
    _insert_entity(
        conn,
        EntitySnapshot(
            entity_id=10,
            entity_type="User",
            name="Old Name",
            username="old_username",
            name_normalized="old name",
            updated_at=100,
        ),
    )
    conn.commit()

    upsert_entity_snapshots(
        conn,
        [
            EntitySnapshot(
                entity_id=10,
                entity_type="Bot",
                name=None,
                username=None,
                name_normalized=None,
                updated_at=200,
            )
        ],
    )

    assert _entity_row(conn, 10) == (10, "Bot", None, None, None, 200)


def test_upsert_conflict_preserves_entity_details_with_foreign_keys_enabled(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
    _insert_entity(
        conn,
        EntitySnapshot(10, "User", "Alice", "alice", "alice", 100),
    )
    conn.execute(
        "INSERT INTO entity_details (entity_id, detail_json, fetched_at) VALUES (?, ?, ?)",
        (10, '{"about":"preserve me"}', 101),
    )
    conn.commit()

    upsert_entity_snapshots(
        conn,
        [EntitySnapshot(10, "User", "Alice Updated", "alice", "alice updated", 200)],
    )

    assert _entity_row(conn, 10) == (10, "User", "Alice Updated", "alice", "alice updated", 200)
    assert _detail_row(conn, 10) == (10, '{"about":"preserve me"}', 101)


def test_upsert_entity_snapshots_handles_insert_and_update_batch(conn: sqlite3.Connection) -> None:
    _insert_entity(conn, EntitySnapshot(10, "User", "Old", "old", "old", 100))
    conn.commit()

    upsert_entity_snapshots(
        conn,
        [
            EntitySnapshot(10, "User", "Updated", "updated", "updated", 200),
            EntitySnapshot(20, "Channel", "New", None, "new", 201),
        ],
    )

    rows = cast(
        list[EntityRow],
        conn.execute(
            "SELECT id, type, name, username, name_normalized, updated_at FROM entities ORDER BY id"
        ).fetchall(),
    )
    assert rows == [
        (10, "User", "Updated", "updated", "updated", 200),
        (20, "Channel", "New", None, "new", 201),
    ]


def test_upsert_entity_snapshots_accepts_empty_batch(conn: sqlite3.Connection) -> None:
    changes_before = conn.total_changes

    upsert_entity_snapshots(conn, ())

    assert conn.total_changes == changes_before
    assert conn.in_transaction is False


def test_ensure_entity_stub_inserts_missing_row(conn: sqlite3.Connection) -> None:
    ensure_entity_stub(
        conn,
        EntitySnapshot(10, "Channel", "Stub", None, "stub", 100),
    )

    assert _entity_row(conn, 10) == (10, "Channel", "Stub", None, "stub", 100)


def test_ensure_entity_stub_collision_changes_neither_snapshot_nor_details(conn: sqlite3.Connection) -> None:
    _insert_entity(conn, EntitySnapshot(10, "User", "Rich Name", "rich", "rich name", 100))
    conn.execute(
        "INSERT INTO entity_details (entity_id, detail_json, fetched_at) VALUES (?, ?, ?)",
        (10, '{"about":"rich details"}', 101),
    )
    conn.commit()

    ensure_entity_stub(
        conn,
        EntitySnapshot(10, "Channel", None, None, None, 200),
    )

    assert _entity_row(conn, 10) == (10, "User", "Rich Name", "rich", "rich name", 100)
    assert _detail_row(conn, 10) == (10, '{"about":"rich details"}', 101)


def test_upsert_entity_snapshots_leaves_transaction_control_to_caller(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN")
    upsert_entity_snapshots(
        conn,
        [EntitySnapshot(10, "User", "Rollback", None, "rollback", 100)],
    )
    assert conn.in_transaction is True

    conn.rollback()

    assert _entity_row(conn, 10) is None


def test_ensure_entity_stub_leaves_transaction_control_to_caller(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN")
    ensure_entity_stub(
        conn,
        EntitySnapshot(10, "User", "Rollback", None, "rollback", 100),
    )
    assert conn.in_transaction is True

    conn.rollback()

    assert _entity_row(conn, 10) is None
