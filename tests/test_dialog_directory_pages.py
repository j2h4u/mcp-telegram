"""Bounded local dialog directory traversal and signed cursors."""

import sqlite3
from collections.abc import Callable
from typing import cast

import pytest

from mcp_telegram.dialog_identity_contracts import DialogIdentity
from mcp_telegram.pagination import (
    DialogDirectoryNavigationToken,
    decode_dialog_directory_navigation,
    encode_dialog_directory_navigation,
)
from mcp_telegram.reading import service as reading_service
from mcp_telegram.reading.sqlite_projection import _DIALOG_DIRECTORY_PAGE_SQL
from tests.test_daemon_api import _make_db_with_dialogs, make_server


def test_directory_navigation_signed_and_negative_anchor() -> None:
    token = DialogDirectoryNavigationToken(7, "a" * 64, -20, 50)
    encoded = encode_dialog_directory_navigation(token)
    assert decode_dialog_directory_navigation(encoded) == token
    zero = DialogDirectoryNavigationToken(7, "a" * 64, 0, 0)
    assert decode_dialog_directory_navigation(encode_dialog_directory_navigation(zero)) == zero
    with pytest.raises(ValueError):
        decode_dialog_directory_navigation(encoded + "x")
    with pytest.raises(ValueError):
        encode_dialog_directory_navigation(DialogDirectoryNavigationToken(7, "a" * 64, True, 1))


async def test_sparse_directory_pages_bound_examined_candidates_and_preserve_upper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _make_db_with_dialogs()
    try:
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.executemany(
            "INSERT INTO dialogs(dialog_id,name,type,hidden,last_message_at) VALUES (?,?,?,0,?)",
            [(i, f"peer {i}", "bot" if i < 1000 else "user", 100 if i == 1000 else None) for i in range(1, 1001)],
        )
        conn.commit()
        server = make_server(conn)
        server.self_id = 42
        reader = server._get_reading_service()
        request = {"verified_account_id": 42, "navigation": "start", "dialog_type": "user", "limit": 1}
        before = conn.total_changes
        sizes: list[int] = []
        original = reading_service.read_dialog_identities

        def bounded_identities(connection: sqlite3.Connection, ids: list[int]) -> dict[int, DialogIdentity]:
            sizes.append(len(ids))
            return original(connection, ids)

        monkeypatch.setattr(reading_service, "read_dialog_identities", bounded_identities)
        page1 = await reader.list_dialogs(request)
        assert page1["ok"] and page1["data"]["dialogs"] == []
        next1 = page1["data"]["navigation"]["next_navigation"]
        assert decode_dialog_directory_navigation(next1).last_dialog_id == 500
        page2 = await reader.list_dialogs({**request, "navigation": next1})
        assert [row["id"] for row in page2["data"]["dialogs"]] == [1000]
        assert not page2["data"]["navigation"]["has_more"]
        assert conn.total_changes == before
        assert sizes == [500, 500]
        changed = await reader.list_dialogs({**request, "navigation": next1, "dialog_type": "bot"})
        assert changed["error"] == "invalid_navigation"
    finally:
        conn.close()


async def test_page_membership_time_scope_and_live_upper_bound() -> None:
    conn = _make_db_with_dialogs()
    try:
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.executemany(
            "INSERT INTO dialogs(dialog_id,name,type,hidden,last_message_at) VALUES(?,?,?,0,?)",
            [
                (-9, "negative", "user", 100),
                (4, "too old", "user", 99),
                (7, "at end", "user", 200),
                (8, "missing", "user", None),
                (9, "robot", "bot", 120),
                (42, "self", "user", 130),
                (50, "tail", "user", 150),
            ],
        )
        conn.commit()
        server = make_server(conn)
        server.self_id = 42
        reader = server._get_reading_service()
        req = {
            "verified_account_id": 42,
            "dialog_type": "user",
            "exclude_self": True,
            "last_message_since_utc": 100,
            "last_message_until_utc": 200,
            "limit": 1,
        }
        first = await reader.list_dialogs(req)
        assert [row["id"] for row in first["data"]["dialogs"]] == [-9]
        nav = first["data"]["navigation"]["next_navigation"]
        assert decode_dialog_directory_navigation(nav).upper_dialog_id == 50
        conn.execute("INSERT INTO dialogs(dialog_id,name,type,hidden,last_message_at) VALUES(60,'new','user',0,150)")
        conn.commit()
        second = await reader.list_dialogs({**req, "navigation": nav, "limit": 10})
        assert [row["id"] for row in second["data"]["dialogs"]] == [50]
        assert second["data"]["navigation"]["has_more"] is False
        other = await reader.list_dialogs({**req, "verified_account_id": 43, "navigation": nav})
        assert other["error"] == "account_unverified"
        invalid = await reader.list_dialogs({**req, "navigation": nav + "x"})
        assert invalid["error"] == "invalid_navigation"
    finally:
        conn.close()


async def test_raw_reader_rejects_invalid_audit_selectors() -> None:
    conn = _make_db_with_dialogs()
    try:
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.commit()
        server = make_server(conn)
        server.self_id = 42
        reader = server._get_reading_service()
        for invalid in (
            {"dialog_type": []},
            {"dialog_type": "human"},
            {"exclude_self": "true"},
            {"last_message_since_utc": True},
            {"last_message_since_utc": "100"},
            {"last_message_since_utc": 101, "last_message_until_utc": 100},
            {"folder_id": -1},
            {"limit": 0},
            {"navigation": ""},
        ):
            result = await reader.list_dialogs({"navigation": "start", "verified_account_id": 42, **invalid})
            assert result["ok"] is False, invalid
    finally:
        conn.close()


async def test_page_composes_folder_own_and_scheduled_filters(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    conn = make_synced_db()
    conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
    conn.execute("UPDATE scheduled_sync_state SET account_id=42 WHERE key='account'")
    conn.executemany(
        "INSERT INTO dialogs(dialog_id,name,type,hidden) VALUES(?,?,'user',0)",
        [(10, "not folder"), (20, "selected"), (30, "unknown membership")],
    )
    conn.execute(
        "INSERT INTO telegram_folder_rules(namespace,folder_id,title,rule_kind,source_position,rule_json) VALUES('filter',2,'Work','filter',0,'{}')"
    )
    conn.executemany(
        "INSERT INTO telegram_folder_local_members(namespace,folder_id,dialog_id,state) VALUES('filter',2,?,?)",
        [(20, "present"), (30, "unknown")],
    )
    conn.executemany(
        "INSERT INTO own_only_dialogs(dialog_id,inclusion_basis,updated_at) VALUES(?, '[\"direct_message\"]',1)",
        [(10,), (20,), (30,)],
    )
    conn.executemany(
        "INSERT INTO scheduled_messages(dialog_id,message_id,scheduled_at,first_seen_at,updated_at) VALUES(?,1,9999999999,1,1)",
        [(10,), (20,), (30,)],
    )
    conn.commit()
    server = make_server(conn)
    server.self_id = 42
    reader = server._get_reading_service()
    result = await reader.list_dialogs(
        {
            "navigation": "start",
            "verified_account_id": 42,
            "folder_id": 2,
            "scope": "own_only",
            "message_state": "scheduled",
            "limit": 1,
        }
    )
    assert result["ok"] is True
    rows = result["data"]["dialogs"]
    assert [row["id"] for row in rows] == [20]
    assert rows[0]["folder_ids"] == [2] and rows[0]["scheduled_count"] == 1
    assert result["data"]["folder_membership_unknown_count"] == 1
    assert result["data"]["navigation"]["has_more"] is True


async def test_page_uses_caller_transaction_and_equal_effective_bounds_are_empty() -> None:
    conn = _make_db_with_dialogs()
    try:
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.commit()
        server = make_server(conn)
        server.self_id = 42
        reader = server._get_reading_service()
        conn.execute("INSERT INTO dialogs(dialog_id,name,type,hidden,last_message_at) VALUES(8,'pending','user',0,100)")
        result = await reader.list_dialogs(
            {
                "navigation": "start",
                "verified_account_id": 42,
                "last_message_since_utc": 100,
                "last_message_until_utc": 100,
            }
        )
        assert result["ok"] and result["data"]["dialogs"] == []
        assert conn.in_transaction
        conn.rollback()
        assert conn.execute("SELECT 1 FROM dialogs WHERE dialog_id=8").fetchone() is None
    finally:
        conn.close()


def test_continuation_sql_seeks_both_primary_keys() -> None:
    conn = _make_db_with_dialogs()
    try:
        rows = cast(
            list[tuple[object, object, object, object]],
            conn.execute(
                "EXPLAIN QUERY PLAN " + _DIALOG_DIRECTORY_PAGE_SQL,
                {"after_id": 5, "upper_id": 100, "archived_filter": None, "pinned_filter": None},
            ).fetchall(),
        )
        details = [str(row[3]) for row in rows]
        assert sum("rowid>? AND rowid<?" in detail for detail in details) >= 2
        assert not any("TEMP B-TREE" in detail for detail in details)
    finally:
        conn.close()


async def test_empty_directory_finishes_without_cursor() -> None:
    conn = _make_db_with_dialogs()
    try:
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.commit()
        server = make_server(conn)
        server.self_id = 42
        result = await server._get_reading_service().list_dialogs({"navigation": "start", "verified_account_id": 42})
        assert result["data"]["dialogs"] == []
        assert result["data"]["navigation"]["has_more"] is False
        assert result["data"]["navigation"]["next_navigation"] is None
    finally:
        conn.close()
