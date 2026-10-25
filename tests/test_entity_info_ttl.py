"""Tests for GetEntityInfo TTL gate + auto-resolve write-back.

SPEC Reqs covered: 8 (DB-first configured entity-detail TTL — second call within window
produces zero new MTProto), 11 (first call on unknown id writes entities
AND entity_details rows; subsequent in-TTL call serves from DB).
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# HIGH-1 from 47-REVIEWS.md cycle 3 (codex 2026-04-25): TTL tests dispatch
# through the User branch of `_classify_dialog_type()`, which calls
# `isinstance(entity, User)`. Plain MagicMock() bypasses that branch
# silently — use spec=User on the resolved entity mock.
from telethon.tl.types import User  # type: ignore[import-untyped]

from mcp_telegram.daemon_api import DaemonAPIServer, DaemonClientLike
from mcp_telegram.entity_profile.contracts import (
    ProjectionOutcome,
    ProjectionStatus,
    TargetKind,
    UserProfileObservation,
)
from tests.daemon_api_policy import make_daemon_api_policy
from tests.helpers import (
    ClientChatAvatarHistoryPort,
    ClientCommonChatsPort,
    ClientUserAvatarHistoryPort,
    LoudChannelProfilePort,
    LoudGroupProfilePort,
    UserReference,
    acquire_entity_profile,
    in_memory_sync_db,
)
from tests.sqlite_writer_helpers import inline_writer

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
    conn = in_memory_sync_db()
    _TEST_DBS.append(conn)
    return conn


def make_server(
    conn: sqlite3.Connection | None = None,
    client: DaemonClientLike | None = None,
    *,
    about: str | None = None,
) -> DaemonAPIServer:
    if conn is None:
        conn = _make_db()
    if client is None:
        client = MagicMock()
    shutdown_event = asyncio.Event()

    class _GenericUserProfilePort:
        def __init__(self) -> None:
            self.about = about

        def get_user_reference(self, user_id: int, *, is_self: bool = False) -> UserReference:
            return UserReference(user_id, 0, is_self=is_self)

        async def fetch_user_profile(
            self, user_id: int, target_kind: TargetKind, *, owner_account_id: int | None = None
        ) -> UserProfileObservation:
            usable = ProjectionOutcome(ProjectionStatus.USABLE, {"about": self.about}, None, None)
            absent = ProjectionOutcome(ProjectionStatus.ABSENT, {"personal_channel_id": None}, None, None)
            return UserProfileObservation(user_id, target_kind, usable, absent)

    server = DaemonAPIServer(
        conn,
        cast(DaemonClientLike, client),
        shutdown_event,
        writer=inline_writer(conn),
        channel_profile_port=LoudChannelProfilePort(),
        group_profile_port=LoudGroupProfilePort(),
        user_profile_port=_GenericUserProfilePort(),
        common_chats_port=ClientCommonChatsPort(client),
        user_avatar_history_port=ClientUserAvatarHistoryPort(client),
        chat_avatar_history_port=ClientChatAvatarHistoryPort(client),
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
async def test_get_entity_info_serves_old_profile_locally_without_refetch() -> None:
    conn = _make_db()
    client = AsyncMock()
    server = make_server(conn=conn, client=client, about="retained")
    service = server._get_entity_info_service()
    await acquire_entity_profile(
        service, 42, {"id": 42, "type": "user", "name": "Cache", "username": "cache", "about": "retained"}
    )
    service._deps = replace(service._deps, now_provider=lambda: 10_000_000)
    before = conn.total_changes
    calls_before_read = client.await_count

    result = await server._dispatch({"method": "get_entity_info", "entity_id": 42})

    assert result["ok"] is True
    assert cast(dict[str, object], result["data"])["about"] == "retained"
    assert conn.total_changes == before
    assert client.await_count == calls_before_read


@pytest.mark.asyncio
async def test_get_entity_info_exact_ttl_age_stays_local() -> None:
    conn = _make_db()
    client = AsyncMock()
    server = make_server(conn=conn, client=client, about="at cutoff")
    service = server._get_entity_info_service()
    await acquire_entity_profile(service, 43, {"id": 43, "type": "user", "name": "Cache", "about": "at cutoff"})
    service._deps = replace(service._deps, now_provider=lambda: 2_000_000)
    before = conn.total_changes
    calls_before_read = client.await_count

    result = await server._dispatch({"method": "get_entity_info", "entity_id": 43})

    assert result["ok"] is True
    assert cast(dict[str, object], result["data"])["about"] == "at cutoff"
    assert conn.total_changes == before
    assert client.await_count == calls_before_read


@pytest.mark.asyncio
async def test_get_entity_info_unknown_id_does_not_resolve_or_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing local identity stays missing without Telegram access or writeback."""
    conn = _make_db()
    # Pre-condition: no rows in either table for entity 100
    assert conn.execute("SELECT COUNT(*) FROM entities WHERE id=100").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM entity_details WHERE entity_id=100").fetchone()[0] == 0

    client = AsyncMock()
    server = make_server(conn=conn, client=client)
    monkeypatch.setattr("mcp_telegram.daemon_api.time.time", lambda: 5_000_000)
    before = conn.total_changes
    calls_before_read = client.await_count
    r = await server._dispatch({"method": "get_entity_info", "entity_id": 100})

    assert r["ok"] is False
    assert r["error"] == "entity_not_observed"
    assert conn.total_changes == before
    assert client.await_count == calls_before_read
