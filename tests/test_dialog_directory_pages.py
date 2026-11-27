"""Bounded local dialog directory traversal and signed cursors."""

import sqlite3
from collections.abc import Callable
from typing import cast

import pytest

from mcp_telegram.dialog_identity_contracts import DialogIdentity
from mcp_telegram.pagination import (
    DialogDirectoryNavigationToken,
    _encode_payload,
    decode_dialog_directory_navigation,
    encode_dialog_directory_navigation,
)
from mcp_telegram.reading import service as reading_service
from mcp_telegram.reading.sqlite_projection import _DIALOG_DIRECTORY_PAGE_SQL
from mcp_telegram.sync_db import _apply_migrations
from tests.test_daemon_api import make_server


def _directory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _apply_migrations(conn)
    conn.row_factory = sqlite3.Row
    return conn


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
    payload = {
        "kind": "dialog_directory",
        "version": 2,
        "order": "last_message_at_desc",
        "account_id": 7,
        "fingerprint": "a" * 64,
        "last_dialog_id": 1,
        "upper_dialog_id": 1,
        "last_message_at": 2**63,
    }
    with pytest.raises(ValueError):
        decode_dialog_directory_navigation(_encode_payload(payload))
    for timestamp in (True, 2**63, -(2**63) - 1):
        with pytest.raises(ValueError):
            encode_dialog_directory_navigation(
                DialogDirectoryNavigationToken(7, "a" * 64, 1, 1, "last_message_at_desc", timestamp)
            )


async def test_sparse_directory_pages_bound_examined_candidates_and_preserve_upper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _directory_db()
    try:
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.executemany(
            "INSERT INTO dialogs(dialog_id,name,type,last_message_at) VALUES (?,?,?,?)",
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
    conn = _directory_db()
    try:
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.executemany(
            "INSERT INTO dialogs(dialog_id,name,type,last_message_at) VALUES(?,?,?,?)",
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
        conn.execute("INSERT INTO dialogs(dialog_id,name,type,last_message_at) VALUES(60,'new','user',150)")
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
    conn = _directory_db()
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
    conn.execute("UPDATE telegram_folder_projection_state SET account_id=42 WHERE singleton=1")
    conn.executemany(
        "INSERT INTO dialogs(dialog_id,name,type) VALUES(?,?,'user')",
        [(10, "not folder"), (20, "selected"), (30, "unknown membership")],
    )
    conn.execute(
        "INSERT INTO telegram_folder_rules(account_id,namespace,folder_id,title,rule_kind,source_position,rule_json,catalog_member) VALUES(42,'filter',2,'Work','filter',0,'{}',1)"
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
    assert result["data"]["navigation"]["has_more"] is False


async def test_page_uses_caller_transaction_and_equal_effective_bounds_are_empty() -> None:
    conn = _directory_db()
    try:
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.commit()
        server = make_server(conn)
        server.self_id = 42
        reader = server._get_reading_service()
        conn.execute("INSERT INTO dialogs(dialog_id,name,type,last_message_at) VALUES(8,'pending','user',100)")
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
    conn = _directory_db()
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
    conn = _directory_db()
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


async def test_folder_paging_includes_members_before_dialog_metadata(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    from mcp_telegram.folders.read_repository import folder_summaries

    conn = make_synced_db()
    conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
    conn.execute("UPDATE telegram_folder_projection_state SET account_id=42 WHERE singleton=1")
    conn.execute(
        "INSERT INTO telegram_folder_rules(account_id,namespace,folder_id,title,rule_kind,source_position,rule_json,catalog_member) VALUES(42,'filter',2,'Work','filter',0,'{}',1)"
    )
    conn.executemany(
        "INSERT INTO telegram_folder_local_members(namespace,folder_id,dialog_id,state) VALUES('filter',2,?,'present')",
        [(10,), (20,)],
    )
    conn.commit()
    server = make_server(conn)
    server.self_id = 42
    reader = server._get_reading_service()
    legacy = await reader.list_dialogs({"verified_account_id": 42, "folder_id": 2})
    assert [row["id"] for row in legacy["data"]["dialogs"]] == [10, 20]
    first = await reader.list_dialogs({"navigation": "start", "verified_account_id": 42, "folder_id": 2, "limit": 1})
    assert [row["id"] for row in first["data"]["dialogs"]] == [10]
    assert first["data"]["dialogs"][0]["archived"] is None
    assert first["data"]["dialogs"][0]["unread_count"] is None
    token = first["data"]["navigation"]["next_navigation"]
    second = await reader.list_dialogs({"navigation": token, "verified_account_id": 42, "folder_id": 2, "limit": 1})
    assert [row["id"] for row in second["data"]["dialogs"]] == [20]
    assert folder_summaries(conn, account_id=42)[0]["dialog_count"] == 2


async def test_missing_dialog_access_facts_stay_unknown_and_filters_require_evidence() -> None:
    conn = _directory_db()
    conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
    conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (10,'access_lost')")
    conn.commit()
    server = make_server(conn)
    server.self_id = 42
    reader = server._get_reading_service()
    for navigation in (None, "start"):
        req = {"verified_account_id": 42, "navigation": navigation}
        result = await reader.list_dialogs(req)
        row = result["data"]["dialogs"][0]
        assert row["id"] == 10 and row["archived"] is None
        assert row["unread_mentions_count"] is None and row["unread_reactions_count"] is None
        for selector in ("exclude_archived", "ignore_pinned"):
            filtered = await reader.list_dialogs({**req, selector: True})
            assert filtered["data"]["dialogs"] == []
    conn.close()


@pytest.mark.parametrize(
    ("order", "expected"),
    [("last_message_at_desc", [30, 10, 20, 40]), ("last_message_at_asc", [10, 20, 30, 40])],
)
async def test_folder_time_order_across_pages(order: str, expected: list[int]) -> None:
    conn = _directory_db()
    conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
    conn.execute("UPDATE telegram_folder_projection_state SET account_id=42 WHERE singleton=1")
    conn.execute(
        "INSERT INTO telegram_folder_rules(account_id,namespace,folder_id,title,rule_kind,source_position,rule_json,catalog_member) VALUES(42,'filter',2,'Work','filter',0,'{}',1)"
    )
    conn.executemany(
        "INSERT INTO dialogs(dialog_id,name,type,last_message_at,pinned) VALUES(?,?,'user',?,0)",
        [(10, "a", 100), (20, "b", 100), (30, "c", 200), (40, "d", None)],
    )
    conn.executemany(
        "INSERT INTO telegram_folder_local_members(namespace,folder_id,dialog_id,state) VALUES('filter',2,?,'present')",
        [(10,), (20,), (30,), (40,)],
    )
    conn.commit()
    server = make_server(conn)
    server.self_id = 42
    reader = server._get_reading_service()
    req = {
        "verified_account_id": 42,
        "folder_id": 2,
        "message_state": "sent",
        "ignore_pinned": True,
        "dialog_type": "user",
        "limit": 1,
        "order": order,
    }
    ids = []
    while True:
        page = await reader.list_dialogs(req)
        assert page["ok"]
        ids.extend(row["id"] for row in page["data"]["dialogs"])
        nav = page["data"]["navigation"]
        assert nav["order"] == order
        if not nav["has_more"]:
            break
        if len(ids) == 1:
            assert (await reader.list_dialogs({**req, "navigation": nav["next_navigation"], "dialog_type": "bot"}))[
                "error"
            ] == "invalid_navigation"
        req["navigation"] = nav["next_navigation"]
    assert ids == expected
    conn.close()


async def test_folder_default_time_order_filtered_sparse_pages() -> None:
    conn = _directory_db()
    conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
    conn.execute("UPDATE telegram_folder_projection_state SET account_id=42 WHERE singleton=1")
    conn.execute(
        "INSERT INTO telegram_folder_rules(account_id,namespace,folder_id,title,rule_kind,source_position,rule_json,catalog_member) VALUES(42,'filter',2,'Work','filter',0,'{}',1)"
    )
    conn.executemany(
        "INSERT INTO dialogs(dialog_id,name,type,last_message_at,pinned) VALUES(?,?,?, ?,0)",
        [(i, f"peer {i}", "user" if i == 1 else "bot", i) for i in range(1, 503)],
    )
    conn.executemany(
        "INSERT INTO telegram_folder_local_members(namespace,folder_id,dialog_id,state) VALUES('filter',2,?,'present')",
        [(i,) for i in range(1, 503)],
    )
    conn.commit()
    server = make_server(conn)
    server.self_id = 42
    reader = server._get_reading_service()
    req = {
        "verified_account_id": 42,
        "folder_id": 2,
        "message_state": "sent",
        "ignore_pinned": True,
        "dialog_type": "user",
        "last_message_since_utc": 1,
        "last_message_until_utc": 600,
        "limit": 30,
    }
    first = await reader.list_dialogs(req)
    assert first["data"]["dialogs"] == []
    nav = first["data"]["navigation"]
    assert nav["order"] == "last_message_at_desc" and nav["has_more"]
    second = await reader.list_dialogs({**req, "order": "last_message_at_desc", "navigation": nav["next_navigation"]})
    assert [row["id"] for row in second["data"]["dialogs"]] == [1]
    assert not second["data"]["navigation"]["has_more"]
    conn.close()


async def test_archived_folder_orders_by_time() -> None:
    conn = _directory_db()
    conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
    conn.executemany(
        "INSERT INTO dialogs(dialog_id,name,type,last_message_at,archived) VALUES(?,?,'user',?,?)",
        [(1, "old", 100, 1), (2, "new", 200, 1), (3, "outside", 300, 0)],
    )
    conn.commit()
    server = make_server(conn)
    server.self_id = 42
    reader = server._get_reading_service()
    req = {"verified_account_id": 42, "folder_id": 1, "navigation": "start", "limit": 1}
    first = await reader.list_dialogs(req)
    assert [row["id"] for row in first["data"]["dialogs"]] == [2]
    second = await reader.list_dialogs({**req, "navigation": first["data"]["navigation"]["next_navigation"]})
    assert [row["id"] for row in second["data"]["dialogs"]] == [1]
    conn.close()
