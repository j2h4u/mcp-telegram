"""Canonical SQLite persistence boundary for entity snapshots."""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from .models import DialogType
from .resolver import latinize


@dataclass(frozen=True, slots=True)
class EntitySnapshot:
    """Complete entity identity values supplied by a runtime writer."""

    entity_id: int
    entity_type: str
    name: str | None
    username: str | None
    name_normalized: str | None
    updated_at: int


@dataclass(frozen=True, slots=True)
class PartialEntityIdentity:
    """Identity fields observed by a partial runtime snapshot."""

    observed_at: int
    observed_type: str | None = None
    name_observed: bool = False
    name: str | None = None
    username_observed: bool = False
    username: str | None = None


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
_UPSERT_ENTITY_STUB_SQL = (
    "INSERT INTO entities (id, type, name, username, name_normalized, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(id) DO UPDATE SET "
    "type=COALESCE(entities.type, excluded.type), "
    "name=COALESCE(entities.name, excluded.name), "
    "username=entities.username, "
    "name_normalized=COALESCE(entities.name_normalized, excluded.name_normalized), "
    "updated_at=MAX(entities.updated_at, excluded.updated_at)"
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


def apply_partial_entity_identity(
    conn: sqlite3.Connection,
    entity_id: int,
    identity: PartialEntityIdentity,
) -> int | None:
    """Apply an identity observation and carry detail fencing forward.

    Callers own the surrounding transaction.  Missing fields preserve the
    canonical row; an explicitly observed ``None`` clears that field.  A
    known canonical type remains authoritative while an unknown placeholder
    may be upgraded by a classified observation.
    """
    current = cast(
        tuple[object, ...] | None,
        conn.execute("SELECT type, name, username, name_normalized FROM entities WHERE id=?", (entity_id,)).fetchone(),
    )
    current_type, current_name, current_username, current_normalized = current or (
        None,
        None,
        None,
        None,
    )
    next_type = _merge_observed_type(current_type, identity.observed_type)
    next_name = identity.name if identity.name_observed else _optional_text(current_name)
    next_username = identity.username if identity.username_observed else _optional_text(current_username)
    next_normalized = _merge_name_normalized(
        current_normalized,
        next_name,
        name_observed=identity.name_observed,
    )
    upsert_entity_snapshots(
        conn,
        (
            EntitySnapshot(
                entity_id=entity_id,
                entity_type=next_type,
                name=next_name,
                username=next_username,
                name_normalized=next_normalized,
                updated_at=identity.observed_at,
            ),
        ),
    )

    return _carry_profile_revision(conn, entity_id)


def _merge_observed_type(current_type: object, observed_type: str | None) -> str:
    current = str(current_type) if current_type is not None else "unknown"
    return observed_type if DialogType.parse(current) is DialogType.UNKNOWN and observed_type else current


def _merge_name_normalized(current_normalized: object, name: str | None, *, name_observed: bool) -> str | None:
    if name_observed:
        return latinize(name) if name else None
    if isinstance(current_normalized, str):
        return current_normalized
    return latinize(name) if name else None


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _carry_profile_revision(conn: sqlite3.Connection, entity_id: int) -> int | None:
    if not _has_column(conn, "entity_details", "profile_revision"):
        return _bump_refresh_if_detail_missing(conn, entity_id)
    return _bump_existing_detail_revision(conn, entity_id)


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = cast(list[tuple[object, ...]], conn.execute(f"PRAGMA table_info({table})").fetchall())
    return column in {str(row[1]) for row in rows}


def _bump_refresh_if_detail_missing(conn: sqlite3.Connection, entity_id: int) -> int | None:
    try:
        detail_row = cast(
            tuple[object, ...] | None,
            conn.execute("SELECT 1 FROM entity_details WHERE entity_id=?", (entity_id,)).fetchone(),
        )
    except sqlite3.OperationalError:
        return None
    return None if detail_row is not None else _bump_refresh_revision(conn, entity_id)


def _bump_existing_detail_revision(conn: sqlite3.Connection, entity_id: int) -> int | None:
    changed = conn.execute(
        "UPDATE entity_details SET profile_revision=profile_revision+1 WHERE entity_id=?", (entity_id,)
    ).rowcount
    if changed != 1:
        return _bump_refresh_revision(conn, entity_id)
    row = cast(
        tuple[object, ...] | None,
        conn.execute("SELECT profile_revision FROM entity_details WHERE entity_id=?", (entity_id,)).fetchone(),
    )
    if row is None or row[0] is None:
        return None
    revision = _optional_int(row[0])
    if revision is None:
        return None
    if _has_column(conn, "entity_profile_refresh_state", "profile_revision"):
        conn.execute(
            "UPDATE entity_profile_refresh_state SET profile_revision=? "
            "WHERE entity_id=? AND status IN ('pending', 'failed')",
            (revision, entity_id),
        )
    return revision


def _bump_refresh_revision(conn: sqlite3.Connection, entity_id: int) -> int | None:
    refresh_rows = cast(
        list[tuple[object, ...]], conn.execute("PRAGMA table_info(entity_profile_refresh_state)").fetchall()
    )
    if "profile_revision" not in {str(item[1]) for item in refresh_rows}:
        return None
    changed = conn.execute(
        "UPDATE entity_profile_refresh_state SET profile_revision=profile_revision+1 "
        "WHERE entity_id=? AND status IN ('pending', 'failed')",
        (entity_id,),
    ).rowcount
    if changed != 1:
        return None
    row = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT profile_revision FROM entity_profile_refresh_state WHERE entity_id=?", (entity_id,)
        ).fetchone(),
    )
    return None if row is None else _optional_int(row[0])


def ensure_entity_stub(conn: sqlite3.Connection, snapshot: EntitySnapshot) -> None:
    """Insert a missing parent entity without changing an existing row."""
    conn.execute(_INSERT_ENTITY_STUB_SQL, _snapshot_values(snapshot))


def upsert_entity_stub(conn: sqlite3.Connection, snapshot: EntitySnapshot) -> None:
    """Fill a local stub without replacing richer existing entity facts."""
    conn.execute(_UPSERT_ENTITY_STUB_SQL, _snapshot_values(snapshot))
