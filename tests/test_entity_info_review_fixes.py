"""Tests for behaviors introduced by Phase 47 code-review fixes.

Covers:
- WR-01: _format_relative_ymd future-date and today branches (fix(47-08))
- CR-01: a channel profile failure does not suppress the independent bounded
         contact-overlap operation
- WR-05: degraded full-fetch (user or channel profile request raises)
         skips entity_details cache write (fix(47-09))
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon.tl.types import Channel as TelethonChannel  # type: ignore[import-untyped]
from telethon.tl.types import User  # type: ignore[import-untyped]

from mcp_telegram.daemon_api import DaemonAPIServer, DaemonClientLike
from mcp_telegram.entity_profile.contracts import (
    ChannelContactOverlapObservation,
    ChannelProfileObservation,
    ChannelReference,
    ProjectionStatus,
)
from mcp_telegram.entity_profile.ports import ChannelProfilePort, UserProfilePort
from mcp_telegram.tools.entity_info import _entity_input_label, _format_relative_ymd
from tests.daemon_api_policy import make_daemon_api_policy
from tests.helpers import (
    FakeUserProfilePort,
    LoudChannelProfilePort,
    LoudGroupProfilePort,
    LoudUserProfilePort,
)
from tests.reaction_helpers import make_reaction_freshener

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_DBS: list[sqlite3.Connection] = []


@pytest.fixture(autouse=True)
def _close_test_db():
    yield
    while _TEST_DBS:
        conn = _TEST_DBS.pop()
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - best-effort fixture cleanup, keep teardown behavior stable
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
        CREATE TABLE full_history_enrollment (
            dialog_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
            source TEXT NOT NULL CHECK(source IN ('explicit', 'automatic', 'migration')),
            updated_at INTEGER NOT NULL
        ) WITHOUT ROWID;
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


def _make_server(
    conn: sqlite3.Connection | None = None,
    client: DaemonClientLike | None = None,
    user_profile_port: UserProfilePort | None = None,
    *,
    channel_profile_port: ChannelProfilePort,
) -> DaemonAPIServer:
    if conn is None:
        conn = _make_db()
    if client is None:
        client = MagicMock()
    server = DaemonAPIServer(
        conn,
        cast(DaemonClientLike, client),
        asyncio.Event(),
        reaction_freshener=make_reaction_freshener(conn, client),
        channel_profile_port=channel_profile_port,
        group_profile_port=LoudGroupProfilePort(),
        user_profile_port=user_profile_port if user_profile_port is not None else LoudUserProfilePort(),
        policy=make_daemon_api_policy(),
    )
    server._ready = True
    return server


def _channel(id_: int = -1001, admin: bool = True, **kw: object) -> MagicMock:
    c = MagicMock(spec=TelethonChannel)
    c.id = id_
    c.title = kw.get("title", "Chan")
    c.username = kw.get("username", "chan")
    c.access_hash = 0
    c.min = False
    c.megagroup = False
    c.broadcast = True
    c.forum = False
    c.creator = admin
    c.admin_rights = MagicMock() if admin else None
    c.left = False
    c.restriction_reason = []
    return c


def _supergroup(id_: int = -2001, admin: bool = True, **kw: object) -> MagicMock:
    c = MagicMock(spec=TelethonChannel)
    c.id = id_
    c.title = kw.get("title", "SG")
    c.username = kw.get("username", "sg")
    c.access_hash = 0
    c.min = False
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


class _FailingChannelProfilePort:
    def __init__(
        self,
        channel_id: int,
        error: BaseException,
        *,
        overlap_status: ProjectionStatus = ProjectionStatus.PARTIAL,
        overlap_reason: str = "bounded_contacts_page",
        contact_ids: tuple[int, ...] | None = (),
    ) -> None:
        self.error = error
        self.profile_calls = 0
        self.overlap_calls = 0
        self.overlap = ChannelContactOverlapObservation(
            channel_id=channel_id,
            contact_ids=contact_ids,
            status=overlap_status,
            reason=overlap_reason,
            observation_started_at=100,
            observation_completed_at=100,
        )

    def get_channel_reference(self, channel_id: int) -> ChannelReference | None:
        canonical_id = channel_id if channel_id <= -1_000_000_000_001 else -1_000_000_000_000 - abs(channel_id)
        return ChannelReference(canonical_id, 0)

    async def fetch_channel_profile(self, reference: ChannelReference) -> ChannelProfileObservation:
        self.profile_calls += 1
        raise self.error

    async def fetch_channel_contact_overlap(self, reference: ChannelReference) -> ChannelContactOverlapObservation:
        self.overlap_calls += 1
        return replace(self.overlap, channel_id=reference.channel_id)


def _user_entity(id_: int = 99) -> MagicMock:
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


def test_entity_input_label_prefers_entity_string() -> None:
    from mcp_telegram.tools.entity_info import GetEntityInfo

    assert _entity_input_label(GetEntityInfo(entity="Alice")) == "Alice"
    assert _entity_input_label(GetEntityInfo(exact_entity_id=42)) == "42"


# ---------------------------------------------------------------------------
# CR-01: profile and overlap are independent bounded operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_profile_failure_still_runs_bounded_overlap() -> None:
    """A profile failure leaves the successful bounded overlap available."""
    chan = _channel(id_=-1001, admin=True)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=chan)
    client.side_effect = [MagicMock(count=0, messages=[])]
    port = _FailingChannelProfilePort(-1001, RuntimeError("simulated profile failure"))
    server = _make_server(
        client=client,
        channel_profile_port=port,
    )

    with patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -1001})

    assert r["ok"] is True, f"expected ok=True, got {r}"
    d = cast(dict[str, object], r["data"])
    assert d["contacts_subscribed"] == []
    assert d["contacts_subscribed_partial"] is True
    assert d["contacts_reason"] == "bounded_contacts_page"
    assert port.profile_calls == port.overlap_calls == 1


@pytest.mark.asyncio
async def test_supergroup_profile_failure_preserves_overlap_adapter_reason() -> None:
    """A profile failure leaves an independent adapter-unavailable reason intact."""
    sg = _supergroup(id_=-2001, admin=True)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=sg)
    client.side_effect = [MagicMock(count=0, messages=[])]
    port = _FailingChannelProfilePort(
        -2001,
        RuntimeError("simulated profile failure"),
        overlap_status=ProjectionStatus.UNAVAILABLE,
        overlap_reason="access_lost",
        contact_ids=None,
    )
    server = _make_server(
        client=client,
        channel_profile_port=port,
    )

    with patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -2001})

    assert r["ok"] is True, f"expected ok=True, got {r}"
    d = cast(dict[str, object], r["data"])
    assert d["contacts_subscribed"] is None
    assert d["contacts_reason"] == "access_lost"
    assert d["members_count"] is None
    assert port.profile_calls == port.overlap_calls == 1


# ---------------------------------------------------------------------------
# WR-05: degraded full fetch skips entity_details cache write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_degraded_full_fetch_skips_entity_details_cache() -> None:
    """WR-05: user profile request raises → full_user_ok=False →
    entity_details row NOT written (prevents caching degraded response)."""
    conn = _make_db()
    user = _user_entity(id_=77)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=user)

    server = _make_server(
        conn=conn,
        client=client,
        user_profile_port=FakeUserProfilePort(error=RuntimeError("simulated FloodWait")),
        channel_profile_port=LoudChannelProfilePort(),
    )

    with (
        patch("mcp_telegram.daemon_api.GetCommonChatsRequest", return_value=MagicMock(chats=[])),
        patch("mcp_telegram.daemon_api.GetUserPhotosRequest", return_value=MagicMock(count=0, photos=[])),
    ):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": 77})

    assert r["ok"] is True, f"expected ok=True despite degraded fetch, got {r}"

    # entities row written (auto-resolve still works)
    ent = cast(tuple[int] | None, conn.execute("SELECT id FROM entities WHERE id = 77").fetchone())
    assert ent is not None, "entities row should be written even on degraded fetch"

    # entity_details row NOT written (degraded response must not be cached)
    detail = cast(
        tuple[int] | None, conn.execute("SELECT entity_id FROM entity_details WHERE entity_id = 77").fetchone()
    )
    assert detail is None, "entity_details must NOT be written when the user profile request fails"


@pytest.mark.asyncio
async def test_channel_degraded_full_fetch_skips_entity_details_cache() -> None:
    """WR-05: channel profile observation raises → full_channel_ok=False →
    entity_details row NOT written."""
    conn = _make_db()
    chan = _channel(id_=-3001, admin=False)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=chan)
    client.side_effect = [MagicMock(count=0, messages=[])]

    server = _make_server(
        conn=conn,
        client=client,
        channel_profile_port=_FailingChannelProfilePort(-3001, RuntimeError("simulated flood")),
    )

    with patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": -3001})

    assert r["ok"] is True, f"expected ok=True despite degraded fetch, got {r}"

    detail = cast(
        tuple[int] | None, conn.execute("SELECT entity_id FROM entity_details WHERE entity_id = -3001").fetchone()
    )
    assert detail is None, "entity_details must NOT be written when the channel profile observation fails"
