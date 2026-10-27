"""Tests for behaviors introduced by Phase 47 code-review fixes.

Covers:
- WR-01: _format_relative_ymd future-date and today branches (fix(47-08))
- CR-01: a channel profile failure does not suppress the independent bounded
         contact-overlap operation
- WR-05: failed background acquisitions do not publish fresh profile facts
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_telegram.daemon_api import DaemonAPIServer, DaemonClientLike
from mcp_telegram.entity_profile.contracts import (
    ChannelContactOverlapObservation,
    ChannelProfileObservation,
    ChannelReference,
    ProjectionStatus,
)
from mcp_telegram.entity_profile.ports import ChannelProfilePort, UserProfilePort
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
from mcp_telegram.tools.entity_info import _entity_input_label, _format_relative_ymd
from tests.daemon_api_policy import make_daemon_api_policy
from tests.demand_adapter_helpers import run_ready_slice
from tests.helpers import (
    ClientChatAvatarHistoryPort,
    ClientCommonChatsPort,
    ClientUserAvatarHistoryPort,
    FakeUserProfilePort,
    LoudChannelProfilePort,
    LoudGroupProfilePort,
    LoudUserProfilePort,
    bind_profile_test_account,
    in_memory_sync_db,
)
from tests.sqlite_writer_helpers import inline_writer

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
    conn = in_memory_sync_db()
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
        writer=inline_writer(conn),
        channel_profile_port=channel_profile_port,
        group_profile_port=LoudGroupProfilePort(),
        user_profile_port=user_profile_port if user_profile_port is not None else LoudUserProfilePort(),
        common_chats_port=ClientCommonChatsPort(client),
        user_avatar_history_port=ClientUserAvatarHistoryPort(client),
        chat_avatar_history_port=ClientChatAvatarHistoryPort(client),
        policy=make_daemon_api_policy(),
    )
    server._ready = True
    return server


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


async def _acquire_overlap_then_fail_profile(server: DaemonAPIServer, entity_id: int, kind: str) -> None:
    """Exercise independent durable sections, retaining overlap on profile failure."""
    service = server._get_entity_info_service()
    bind_profile_test_account(service)
    service._deps = replace(service._deps, now_provider=lambda: 100.0, enable_full_user_pair=False)
    service.bind_demand_sink(MagicMock())
    await service._write_profiles(
        lambda profiles: profiles.save_core({"id": entity_id, "type": kind, "name": "Known channel"}, now=100)
    )
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    adapter = EntityProfileDemandAdapter(coordinator)
    for section in ("contact_overlap", "full_profile"):
        await service._write_profiles(
            lambda profiles, section=section: profiles.request_section_refresh(
                entity_id, (section,), now=100, pair_mode="disabled"
            )
        )
        await run_ready_slice(adapter)


def _assert_failed_profile_is_retryable(conn: sqlite3.Connection, entity_id: int) -> None:
    section = cast(
        tuple[str, int | None, int] | None,
        conn.execute(
            "SELECT status, observed_at, retry_at FROM entity_detail_sections WHERE entity_id=? AND section='full_profile'",
            (entity_id,),
        ).fetchone(),
    )
    assert section is not None
    assert section[0] == "unavailable"
    assert section[1] is None
    # Telegram retry timing belongs to the classified RPC gate. Persist the
    # failed section at the observation boundary and let that gate admit the
    # next network attempt.
    assert section[2] == 100


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


@pytest.mark.parametrize(
    ("iso_date", "expected"),
    [
        ("not-a-date", "not-a-date"),
        ("2026-04-24", "1d ago"),
        ("2026-03-26", "1mo ago"),
        ("2025-04-25", "1y ago"),
        ("2025-03-25", "1y 1mo ago"),
        ("2025-04-24", "1y ago"),
    ],
)
def test_format_relative_ymd_date_boundaries(iso_date: str, expected: str) -> None:
    now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
    assert _format_relative_ymd(iso_date, now=now) == expected


def test_format_relative_ymd_uses_calendar_date_for_timezone_offsets() -> None:
    now = datetime(2026, 4, 25, 0, 30, tzinfo=UTC)
    assert _format_relative_ymd("2026-04-25T23:30:00-05:00", now=now) == "today"


def test_format_relative_ymd_naive_now_and_date_are_compared_as_dates() -> None:
    now = datetime(2026, 4, 25, tzinfo=UTC)
    assert _format_relative_ymd("2026-04-24T23:59:59", now=now) == "1d ago"


def test_entity_input_label_prefers_entity_string() -> None:
    from mcp_telegram.tools.entity_info import GetEntityInfo

    assert _entity_input_label(GetEntityInfo(entity="Alice")) == "Alice"
    assert _entity_input_label(GetEntityInfo(exact_entity_id=42)) == "42"


# ---------------------------------------------------------------------------
# CR-01: profile and overlap are independent bounded operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_profile_failure_preserves_acquired_bounded_overlap() -> None:
    """A profile failure leaves the successful bounded overlap available."""
    client = AsyncMock()
    port = _FailingChannelProfilePort(-1001, RuntimeError("simulated profile failure"))
    server = _make_server(
        client=client,
        channel_profile_port=port,
    )
    await _acquire_overlap_then_fail_profile(server, -1001, "channel")
    _assert_failed_profile_is_retryable(server._conn, -1001)
    changes = server._conn.total_changes

    r = await server._dispatch({"method": "get_entity_info", "entity_id": -1001})

    assert r["ok"] is True, f"expected ok=True, got {r}"
    d = cast(dict[str, object], r["data"])
    assert d["contacts_subscribed"] == []
    assert d["contacts_subscribed_partial"] is True
    assert d["contacts_reason"] == "bounded_contacts_page"
    assert port.profile_calls == port.overlap_calls == 1
    assert server._conn.total_changes == changes
    cast(AsyncMock, client.get_entity).assert_not_awaited()


@pytest.mark.asyncio
async def test_supergroup_profile_failure_preserves_overlap_adapter_reason() -> None:
    """A profile failure leaves an independent adapter-unavailable reason intact."""
    client = AsyncMock()
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
    await _acquire_overlap_then_fail_profile(server, -2001, "supergroup")
    _assert_failed_profile_is_retryable(server._conn, -2001)
    changes = server._conn.total_changes

    r = await server._dispatch({"method": "get_entity_info", "entity_id": -2001})

    assert r["ok"] is True, f"expected ok=True, got {r}"
    d = cast(dict[str, object], r["data"])
    assert d["contacts_subscribed"] is None
    assert d["contacts_reason"] == "access_lost"
    assert d.get("members_count") is None
    assert port.profile_calls == port.overlap_calls == 1
    assert server._conn.total_changes == changes
    cast(AsyncMock, client.get_entity).assert_not_awaited()


# ---------------------------------------------------------------------------
# WR-05: failed durable acquisitions do not publish fresh profile facts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_failed_acquisition_keeps_identity_without_fresh_detail() -> None:
    """A failed background request retains identity and retry, not fabricated detail."""
    conn = _make_db()
    client = AsyncMock()
    port = FakeUserProfilePort(error=RuntimeError("simulated profile failure"))

    server = _make_server(
        conn=conn,
        client=client,
        user_profile_port=port,
        channel_profile_port=LoudChannelProfilePort(),
    )
    service = server._get_entity_info_service()
    bind_profile_test_account(service)
    service._deps = replace(service._deps, now_provider=lambda: 100.0, enable_full_user_pair=False)
    service.bind_demand_sink(MagicMock())
    await service._write_profiles(
        lambda profiles: profiles.save_core({"id": 77, "type": "user", "name": "Known user"}, now=100)
    )
    await service._write_profiles(lambda profiles: profiles.mark_pending(77, now=100))
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    await run_ready_slice(EntityProfileDemandAdapter(coordinator), now=100.0)
    _assert_failed_profile_is_retryable(conn, 77)
    changes = conn.total_changes
    calls = list(port.calls)

    r = await server._dispatch({"method": "get_entity_info", "entity_id": 77})

    assert r["ok"] is True, f"expected ok=True despite degraded fetch, got {r}"

    # Identity existed before acquisition and remains locally readable.
    ent = cast(tuple[int] | None, conn.execute("SELECT id FROM entities WHERE id = 77").fetchone())
    assert ent is not None

    # entity_details row NOT written (degraded response must not be cached)
    detail = cast(
        tuple[int] | None, conn.execute("SELECT entity_id FROM entity_details WHERE entity_id = 77").fetchone()
    )
    assert detail is None, "entity_details must NOT be written when the user profile request fails"
    assert conn.total_changes == changes
    assert len(calls) == 1
    assert port.calls == calls
    cast(AsyncMock, client.get_entity).assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_failed_acquisition_keeps_identity_without_fresh_detail() -> None:
    """Failed channel acquisition does not cache invented full-profile fields."""
    conn = _make_db()
    client = AsyncMock()
    port = _FailingChannelProfilePort(-3001, RuntimeError("simulated profile failure"))

    server = _make_server(
        conn=conn,
        client=client,
        channel_profile_port=port,
    )
    service = server._get_entity_info_service()
    bind_profile_test_account(service)
    service._deps = replace(service._deps, now_provider=lambda: 100.0, enable_full_user_pair=False)
    service.bind_demand_sink(MagicMock())
    await service._write_profiles(
        lambda profiles: profiles.save_core({"id": -3001, "type": "channel", "name": "Known channel"}, now=100)
    )
    await service._write_profiles(
        lambda profiles: profiles.request_section_refresh(-3001, ("full_profile",), now=100, pair_mode="disabled")
    )
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    await run_ready_slice(EntityProfileDemandAdapter(coordinator), now=100.0)
    _assert_failed_profile_is_retryable(conn, -3001)
    changes = conn.total_changes

    r = await server._dispatch({"method": "get_entity_info", "entity_id": -3001})

    assert r["ok"] is True, f"expected ok=True despite degraded fetch, got {r}"

    detail = cast(
        tuple[int] | None, conn.execute("SELECT entity_id FROM entity_details WHERE entity_id = -3001").fetchone()
    )
    assert detail is None, "entity_details must NOT be written when the channel profile observation fails"
    assert conn.total_changes == changes
    assert port.profile_calls == 1
    assert port.overlap_calls == 0
    cast(AsyncMock, client.get_entity).assert_not_awaited()
