"""Regression tests for canonical local dialog resolution."""

from __future__ import annotations

import dataclasses
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_telegram.daemon_api import ResolvedDialogId
from mcp_telegram.dialog_directory_coverage import DialogDirectoryCoverage
from mcp_telegram.dialog_selector import required_dialog_selector
from test_daemon_api import _make_db_with_dialogs, _seed_dialog_row, _TestClient, make_server


def _add_canonical_identity_columns(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE dialogs ADD COLUMN username TEXT")
    conn.execute("ALTER TABLE dialogs ADD COLUMN identity_observed_at INTEGER")
    conn.execute("ALTER TABLE dialogs ADD COLUMN identity_complete INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE dialogs ADD COLUMN identity_source TEXT")
    conn.commit()


def test_resolved_dialog_id_survives_dataclass_asdict() -> None:
    coverage = DialogDirectoryCoverage("complete", 7, 1_700_000_000, 1, "complete", True, True)
    resolved = ResolvedDialogId(9004, coverage)

    @dataclasses.dataclass
    class Fragment:
        dialog_id: int

    copied = dataclasses.asdict(Fragment(resolved))

    assert copied["dialog_id"] == 9004
    assert isinstance(copied["dialog_id"], ResolvedDialogId)
    assert copied["dialog_id"].coverage == coverage


@pytest.mark.asyncio
async def test_bare_name_miss_never_enumerates_telegram_or_entity_cache_only() -> None:
    conn = _make_db_with_dialogs()
    conn.execute(
        "INSERT INTO entities (id,type,name,name_normalized,updated_at) VALUES (?,?,?,?,?)",
        (9001, "User", "Entity Only", "entity only", 1_700_000_000),
    )
    conn.commit()
    client = _TestClient()
    client.get_entity = AsyncMock(side_effect=AssertionError("bare natural names must stay local"))
    client.iter_dialogs = MagicMock(side_effect=AssertionError("bare natural names must not enumerate dialogs"))

    result = await make_server(conn, client)._resolve_dialog_id(required_dialog_selector(dialog="Entity Only"))

    assert isinstance(result, dict)
    assert result["error"] == "dialog_directory_incomplete"
    client.get_entity.assert_not_awaited()
    client.iter_dialogs.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_canonical_identity_can_be_enriched_by_existing_entity_cache() -> None:
    conn = _make_db_with_dialogs()
    _add_canonical_identity_columns(conn)
    _seed_dialog_row(conn, 9002)
    conn.execute("UPDATE dialogs SET name=NULL WHERE dialog_id=9002")
    conn.execute("UPDATE dialogs SET identity_complete=0,identity_observed_at=NULL WHERE dialog_id=9002")
    conn.execute(
        "INSERT INTO entities (id,type,name,name_normalized,updated_at) VALUES (?,?,?,?,?)",
        (9002, "User", "Cached Name", "cached name", 1),
    )
    conn.commit()
    client = _TestClient()
    client.get_entity = AsyncMock(side_effect=AssertionError("cache enrichment must stay local"))
    result = await make_server(conn, client)._resolve_dialog_id(required_dialog_selector(dialog="Cached Name"))

    assert result == 9002
    client.get_entity.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_canonical_absence_defeats_stale_entity_name() -> None:
    conn = _make_db_with_dialogs()
    _add_canonical_identity_columns(conn)
    _seed_dialog_row(conn, 9003)
    conn.execute("UPDATE dialogs SET name=NULL WHERE dialog_id=9003")
    conn.execute("UPDATE dialogs SET identity_complete=1,identity_observed_at=1700000000 WHERE dialog_id=9003")
    conn.execute(
        "INSERT INTO entities (id,type,name,name_normalized,updated_at) VALUES (?,?,?,?,?)",
        (9003, "User", "Removed Name", "removed name", 1),
    )
    conn.commit()
    client = _TestClient()
    client.get_entity = AsyncMock(side_effect=AssertionError("bare natural names must stay local"))
    result = await make_server(conn, client)._resolve_dialog_id(required_dialog_selector(dialog="Removed Name"))

    assert isinstance(result, dict)
    assert result["error"] == "dialog_directory_incomplete"
    client.get_entity.assert_not_awaited()
