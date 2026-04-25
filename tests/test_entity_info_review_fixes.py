"""Tests for behaviors introduced by Phase 47 code-review fixes.

Covers:
- WR-01: _format_relative_ymd future-date and today branches (fix(47-08))
- CR-01: subscribers_count/members_count=None → contacts_reason="count_unavailable"
         when GetFullChannelRequest fails on an admin-caller (fix(47-06))
- WR-05: degraded full-fetch (GetFullUserRequest / GetFullChannelRequest raises)
         skips entity_details cache write (fix(47-09))
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from telethon.tl.types import Channel as TelethonChannel  # type: ignore[import-untyped]
from telethon.tl.types import User  # type: ignore[import-untyped]

from mcp_telegram.daemon_api import DaemonAPIServer
from mcp_telegram.tools.entity_info import _format_relative_ymd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    return conn


def _make_server(conn=None, client=None) -> DaemonAPIServer:
    if conn is None:
        conn = _make_db()
    if client is None:
        client = MagicMock()
    server = DaemonAPIServer(conn, client, asyncio.Event())
    server._ready = True
    return server


def _channel(id_=-1001, admin=True, **kw):
    c = MagicMock(spec=TelethonChannel)
    c.id = id_
    c.title = kw.get("title", "Chan")
    c.username = kw.get("username", "chan")
    c.megagroup = False
    c.broadcast = True
    c.forum = False
    c.creator = admin
    c.admin_rights = MagicMock() if admin else None
    c.left = False
    c.restriction_reason = []
    return c


def _supergroup(id_=-2001, admin=True, **kw):
    c = MagicMock(spec=TelethonChannel)
    c.id = id_
    c.title = kw.get("title", "SG")
    c.username = kw.get("username", "sg")
    c.megagroup = True
    c.broadcast = False
    c.forum = False
    c.creator = admin
    c.admin_rights = MagicMock() if admin else None
    c.left = False
    c.restriction_reason = []
    c.noforwards = False
    c.hidden_members = False
    return c


def _user_entity(id_=99):
    u = MagicMock(spec=User)
    u.id = id_
    u.first_name = "Test"
    u.last_name = None
    u.username = "testuser"
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


# ---------------------------------------------------------------------------
# WR-01: _format_relative_ymd future-date and today branches
# ---------------------------------------------------------------------------

def test_format_relative_ymd_future_date() -> None:
    """WR-01: negative delta_days (future date) → 'future date', not 'today'."""
    now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
    result = _format_relative_ymd("2026-04-30", now=now)
    assert result == "future date", f"expected 'future date', got {result!r}"


def test_format_relative_ymd_today() -> None:
    """WR-01: same-day date → 'today'."""
    now = datetime(2026, 4, 25, 23, 59, 0, tzinfo=UTC)
    result = _format_relative_ymd("2026-04-25", now=now)
    assert result == "today", f"expected 'today', got {result!r}"


def test_format_relative_ymd_future_does_not_return_today() -> None:
    """WR-01 regression: the old `<= 0` guard returned 'today' for future dates."""
    now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
    assert _format_relative_ymd("2030-01-01", now=now) == "future date"
    assert _format_relative_ymd("2026-04-26", now=now) == "future date"


# ---------------------------------------------------------------------------
# CR-01: count=None → contacts_reason="count_unavailable"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_channel_admin_full_request_fails_returns_count_unavailable() -> None:
    """CR-01: when GetFullChannelRequest raises, admin channel returns
    contacts_subscribed=None with reason='count_unavailable' instead of
    triggering unbounded iter_participants."""
    chan = _channel(id_=-1001, admin=True)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=chan)
    client.side_effect = [RuntimeError("flood"), MagicMock(count=0, messages=[])]
    server = _make_server(client=client)

    with patch("mcp_telegram.daemon_api.GetFullChannelRequest",
               side_effect=RuntimeError("simulated flood")), \
         patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -1001})

    assert r["ok"] is True, f"expected ok=True, got {r}"
    d = r["data"]
    assert d["contacts_subscribed"] is None
    assert d["contacts_reason"] == "count_unavailable", (
        f"expected 'count_unavailable', got {d.get('contacts_reason')!r}"
    )
    client.iter_participants.assert_not_called()


@pytest.mark.asyncio
async def test_supergroup_admin_full_request_fails_returns_count_unavailable() -> None:
    """CR-01: when GetFullChannelRequest raises on supergroup, returns
    contacts_reason='count_unavailable' (members_count stays None)."""
    sg = _supergroup(id_=-2001, admin=True)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=sg)
    client.side_effect = [MagicMock(count=0, messages=[])]
    server = _make_server(client=client)

    with patch("mcp_telegram.daemon_api.GetFullChannelRequest",
               side_effect=RuntimeError("simulated flood")), \
         patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -2001})

    assert r["ok"] is True, f"expected ok=True, got {r}"
    d = r["data"]
    assert d["contacts_subscribed"] is None
    assert d["contacts_reason"] == "count_unavailable", (
        f"expected 'count_unavailable', got {d.get('contacts_reason')!r}"
    )
    client.iter_participants.assert_not_called()


# ---------------------------------------------------------------------------
# WR-05: degraded full fetch skips entity_details cache write
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_user_degraded_full_fetch_skips_entity_details_cache() -> None:
    """WR-05: GetFullUserRequest raises → full_user_ok=False →
    entity_details row NOT written (prevents caching degraded response)."""
    conn = _make_db()
    user = _user_entity(id_=77)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=user)

    server = _make_server(conn=conn, client=client)

    with patch("mcp_telegram.daemon_api.GetFullUserRequest",
               side_effect=RuntimeError("simulated FloodWait")), \
         patch("mcp_telegram.daemon_api.GetCommonChatsRequest",
               return_value=MagicMock(chats=[])), \
         patch("mcp_telegram.daemon_api.GetUserPhotosRequest",
               return_value=MagicMock(count=0, photos=[])):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": 77})

    assert r["ok"] is True, f"expected ok=True despite degraded fetch, got {r}"

    # entities row written (auto-resolve still works)
    ent = conn.execute("SELECT id FROM entities WHERE id = 77").fetchone()
    assert ent is not None, "entities row should be written even on degraded fetch"

    # entity_details row NOT written (degraded response must not be cached)
    detail = conn.execute(
        "SELECT entity_id FROM entity_details WHERE entity_id = 77"
    ).fetchone()
    assert detail is None, (
        "entity_details must NOT be written when GetFullUserRequest fails"
    )


@pytest.mark.asyncio
async def test_channel_degraded_full_fetch_skips_entity_details_cache() -> None:
    """WR-05: GetFullChannelRequest raises → full_channel_ok=False →
    entity_details row NOT written."""
    conn = _make_db()
    chan = _channel(id_=-3001, admin=False)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=chan)
    client.side_effect = [MagicMock(count=0, messages=[])]

    server = _make_server(conn=conn, client=client)

    with patch("mcp_telegram.daemon_api.GetFullChannelRequest",
               side_effect=RuntimeError("simulated flood")), \
         patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -3001})

    assert r["ok"] is True, f"expected ok=True despite degraded fetch, got {r}"

    detail = conn.execute(
        "SELECT entity_id FROM entity_details WHERE entity_id = -3001"
    ).fetchone()
    assert detail is None, (
        "entity_details must NOT be written when GetFullChannelRequest fails"
    )
