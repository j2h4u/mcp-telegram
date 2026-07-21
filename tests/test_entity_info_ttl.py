"""Tests for GetEntityInfo TTL gate + auto-resolve write-back.

SPEC Reqs covered: 8 (DB-first configured entity-detail TTL — second call within window
produces zero new MTProto), 11 (first call on unknown id writes entities
AND entity_details rows; subsequent in-TTL call serves from DB).
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# HIGH-1 from 47-REVIEWS.md cycle 3 (codex 2026-04-25): TTL tests dispatch
# through the User branch of `_classify_dialog_type()`, which calls
# `isinstance(entity, User)`. Plain MagicMock() bypasses that branch
# silently — use spec=User on the resolved entity mock.
from telethon.tl.types import User  # type: ignore[import-untyped]

from mcp_telegram.daemon_api import DaemonAPIServer, _DaemonClientLike
from tests.daemon_api_policy import make_daemon_api_policy
from tests.reaction_helpers import make_reaction_freshener

_TEST_DBS: list[sqlite3.Connection] = []


@pytest.fixture(autouse=True)
def _close_test_db():
    yield
    while _TEST_DBS:
        conn = _TEST_DBS.pop()
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - best-effort fixture cleanup, preserve old teardown semantics
            pass


@pytest.fixture(autouse=True)
def _patch_get_peer_id():
    with patch(
        "mcp_telegram.daemon_api.telethon_utils.get_peer_id",
        side_effect=lambda entity: int(getattr(entity, "id", 0)),
    ):
        yield


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE synced_dialogs (
            dialog_id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'not_synced',
            last_synced_at INTEGER, last_event_at INTEGER,
            sync_progress INTEGER DEFAULT 0, total_messages INTEGER,
            access_lost_at INTEGER, read_inbox_max_id INTEGER, read_outbox_max_id INTEGER
        );
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT,
            username TEXT, name_normalized TEXT, updated_at INTEGER NOT NULL
        );
        CREATE TABLE entity_details (
            entity_id INTEGER PRIMARY KEY, detail_json TEXT NOT NULL, fetched_at INTEGER NOT NULL,
            FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
        ) WITHOUT ROWID;
        CREATE INDEX idx_entity_details_fetched_at ON entity_details(fetched_at);
        """
    )
    _TEST_DBS.append(conn)
    return conn


def make_server(conn: sqlite3.Connection | None = None, client: _DaemonClientLike | None = None) -> DaemonAPIServer:
    if conn is None:
        conn = _make_db()
    if client is None:
        client = MagicMock()
    shutdown_event = asyncio.Event()
    server = DaemonAPIServer(
        conn,
        cast(_DaemonClientLike, client),
        shutdown_event,
        reaction_freshener=make_reaction_freshener(conn, client),
        policy=make_daemon_api_policy(),
    )
    server._ready = True
    return server


def _user(id_: int = 42) -> MagicMock:
    # HIGH-1 from 47-REVIEWS.md cycle 3: spec=User so the User branch in
    # `_classify_dialog_type()` actually runs. See module-level import.
    u = MagicMock(spec=User)
    u.id = id_
    u.first_name = "Cache"
    u.last_name = None
    u.username = "cache"
    u.bot = False
    u.contact = u.mutual_contact = u.close_friend = False
    u.verified = u.premium = u.scam = u.fake = u.restricted = False
    u.phone = u.lang_code = None
    u.usernames = []
    u.emoji_status = None
    u.restriction_reason = []
    u.send_paid_messages_stars = None
    u.status = None
    return u


def _trio_results() -> tuple[MagicMock, MagicMock, MagicMock]:
    common, full, photos = MagicMock(), MagicMock(), MagicMock()
    common.chats = []
    full.full_user = MagicMock(
        about=None,
        personal_channel_id=None,
        birthday=None,
        blocked=False,
        ttl_period=None,
        private_forward_name=None,
        bot_info=None,
        business_location=None,
        business_intro=None,
        business_work_hours=None,
        note=None,
        folder_id=None,
    )
    photos.count = 0
    photos.photos = []
    return common, full, photos


@pytest.mark.asyncio
async def test_get_entity_info_serves_from_db_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """SPEC Req 8: two consecutive calls within configured TTL → one MTProto round-trip."""
    client = AsyncMock()
    get_entity = AsyncMock(return_value=_user(42))
    client.get_entity = get_entity
    client.side_effect = _trio_results() + _trio_results()  # 6 items in case both calls fetch
    server = make_server(client=client)

    base = 1_000_000
    monkeypatch.setattr("mcp_telegram.daemon_api.time.time", lambda: base)
    with (
        patch("mcp_telegram.daemon_api.GetCommonChatsRequest"),
        patch("mcp_telegram.daemon_api.GetFullUserRequest"),
        patch("mcp_telegram.daemon_api.GetUserPhotosRequest"),
    ):
        r1 = await server._dispatch({"method": "get_entity_info", "entity_id": 42})
    assert r1["ok"]
    first_call_count = get_entity.call_count
    assert first_call_count == 1

    # Within TTL window
    monkeypatch.setattr("mcp_telegram.daemon_api.time.time", lambda: base + 250)
    r2 = await server._dispatch({"method": "get_entity_info", "entity_id": 42})
    assert r2["ok"]
    assert get_entity.call_count == first_call_count, "must serve from DB; no new fetch"

    # After TTL → fresh fetch
    monkeypatch.setattr("mcp_telegram.daemon_api.time.time", lambda: base + 400)
    with (
        patch("mcp_telegram.daemon_api.GetCommonChatsRequest"),
        patch("mcp_telegram.daemon_api.GetFullUserRequest"),
        patch("mcp_telegram.daemon_api.GetUserPhotosRequest"),
    ):
        r3 = await server._dispatch({"method": "get_entity_info", "entity_id": 42})
    assert r3["ok"]
    assert get_entity.call_count == first_call_count + 1


@pytest.mark.asyncio
async def test_get_entity_info_at_exact_ttl_age_refetches(monkeypatch: pytest.MonkeyPatch) -> None:
    """An entity detail exactly at the configured cutoff is stale, not fresh."""
    client = AsyncMock()
    get_entity = AsyncMock(return_value=_user(43))
    client.get_entity = get_entity
    client.side_effect = _trio_results() + _trio_results()
    server = make_server(client=client)
    ttl_seconds = server._policy.entity_detail_ttl_seconds
    base = 2_000_000
    monkeypatch.setattr("mcp_telegram.daemon_api.time.time", lambda: base)
    with (
        patch("mcp_telegram.daemon_api.GetCommonChatsRequest"),
        patch("mcp_telegram.daemon_api.GetFullUserRequest"),
        patch("mcp_telegram.daemon_api.GetUserPhotosRequest"),
    ):
        assert (await server._dispatch({"method": "get_entity_info", "entity_id": 43}))["ok"]

    monkeypatch.setattr("mcp_telegram.daemon_api.time.time", lambda: base + ttl_seconds)
    with (
        patch("mcp_telegram.daemon_api.GetCommonChatsRequest"),
        patch("mcp_telegram.daemon_api.GetFullUserRequest"),
        patch("mcp_telegram.daemon_api.GetUserPhotosRequest"),
    ):
        assert (await server._dispatch({"method": "get_entity_info", "entity_id": 43}))["ok"]

    assert get_entity.call_count == 2


@pytest.mark.asyncio
async def test_get_entity_info_auto_resolve_writes_both_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """SPEC Req 11: first call on unknown id writes entities AND entity_details rows."""
    conn = _make_db()
    # Pre-condition: no rows in either table for entity 100
    assert conn.execute("SELECT COUNT(*) FROM entities WHERE id=100").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM entity_details WHERE entity_id=100").fetchone()[0] == 0

    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=_user(100))
    client.side_effect = _trio_results()
    server = make_server(conn=conn, client=client)
    monkeypatch.setattr("mcp_telegram.daemon_api.time.time", lambda: 5_000_000)
    with (
        patch("mcp_telegram.daemon_api.GetCommonChatsRequest"),
        patch("mcp_telegram.daemon_api.GetFullUserRequest"),
        patch("mcp_telegram.daemon_api.GetUserPhotosRequest"),
    ):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": 100})

    assert r["ok"]
    # Post-condition: BOTH rows now exist
    # conn.row_factory is set to sqlite3.Row by DaemonAPIServer.__init__,
    # so compare using tuple() to avoid Row vs tuple mismatch.
    ent_row = cast(
        tuple[int, str, str] | None, conn.execute("SELECT id, type, username FROM entities WHERE id=100").fetchone()
    )
    assert ent_row is not None
    assert tuple(ent_row) == (100, "user", "cache")
    det_row = cast(
        tuple[str, int] | None,
        conn.execute("SELECT detail_json, fetched_at FROM entity_details WHERE entity_id=100").fetchone(),
    )
    assert det_row is not None
    assert det_row[1] == 5_000_000
    # detail_json carries embedded schema discriminator
    import json as _json

    payload = cast(dict[str, object], _json.loads(det_row[0]))
    assert payload["schema"] == 1
    assert payload["type"] == "user"
    assert payload["id"] == 100
