"""Story diagnostics distinguish existing rejection stages without accepting data."""

import asyncio
import json
import sqlite3
import sys
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

import pytest
from telethon.errors.rpcbaseerrors import BadRequestError
from telethon.tl import types

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.contracts import (
    StoryProfileValidationCategory,
    StoryProfileValidationError,
    UserReference,
)
from mcp_telegram.entity_profile.refresh import RefreshLimits, failure_retry_at, scope_changed_retry_at
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_gateway import TelethonUserProfileGateway
from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope
from tests.test_channel_contact_failure_identity import _QueuedWriter, _run
from tests.test_entity_profile_progressive import _test_service
from tests.test_profile_collection_pages import Database
from tests.test_profile_collection_pages import _Client as _DirectClient
from tests.test_profile_collection_pages import database as database

_IDS = [174837, 274838]
_DATE = datetime(2026, 1, 1, tzinfo=UTC)
_END = datetime(2026, 2, 1, tzinfo=UTC)


class _Session:
    available = True

    def get_input_entity(self, _peer: int) -> types.InputPeerUser | None:
        return types.InputPeerUser(42, 123) if self.available else None


class _Client:
    def __init__(self, result: object) -> None:
        self.result = result
        self.session = _Session()
        self.failure: BaseException | None = None
        self.requests: list[object] = []
        self.before_response = lambda: None

    async def __call__(self, request: object) -> object:
        budget = current_rpc_scope().attempt_budget
        assert budget is not None
        budget.debit()
        self.requests.append(request)
        self.before_response()
        if self.failure is not None:
            raise self.failure
        return self.result


def _stories(*identifiers: int) -> types.stories.Stories:
    return types.stories.Stories(len(identifiers), [types.StoryItemDeleted(value) for value in identifiers], [], [])


type Scenario = tuple[sqlite3.Connection, DaemonEntityInfoService, _Client]


@pytest.fixture
async def scenario(database: Database) -> AsyncIterator[Scenario]:
    _path, conn, _repo = database
    clock = [100.0]
    client = _Client(
        types.stories.PeerStories(
            types.PeerStories(types.PeerUser(42), [types.StoryItemSkipped(value, _DATE, _END) for value in _IDS]),
            [],
            [],
        )
    )
    gateway = TelethonUserProfileGateway(client, now_provider=lambda: clock[0])
    service = _test_service(conn, limits=RefreshLimits(), account_id=77)
    service._deps = replace(service._deps, user_profile_port=gateway, now_provider=lambda: clock[0])
    with write_transaction(conn):
        conn.execute("UPDATE entity_profile_refresh_state SET next_section='current_stories'")
    try:
        await _run(service)
        assert len(client.requests) == 1
        cursor = service._profiles.next_due_refresh(now=100)
        assert cursor is not None and cursor.page_cursor_json is not None
        state = cast(dict[str, object], json.loads(cursor.page_cursor_json))
        assert cast(dict[str, object], state["request"])["phase"] == "details"
        clock[0] = 200.0
        client.result = _stories(_IDS[0])
        yield conn, service, client
    finally:
        await service.shutdown()


def _retained(conn: sqlite3.Connection) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    return (
        cast(list[tuple[object, ...]], conn.execute("SELECT * FROM entity_details").fetchall()),
        cast(list[tuple[object, ...]], conn.execute("SELECT * FROM entity_profile_collection_facts").fetchall()),
    )


@pytest.mark.parametrize(
    ("kind", "suffix"),
    [
        ("missing", "ids_missing:requested=2:returned=1:missing=1:unexpected=0"),
        ("unexpected", "ids_unexpected:requested=2:returned=3:missing=0:unexpected=1"),
        ("mixed", "ids_mixed:requested=2:returned=1:missing=2:unexpected=1"),
        ("invalid", "ids_invalid:requested=2:returned=0:missing=2:unexpected=0"),
        ("response", "response_type"),
        ("vector", "vector_missing"),
        ("transport", "transport"),
        ("normalization", "normalization"),
        ("sidecar", "sidecar"),
    ],
)
async def test_scheduled_story_failures_have_distinct_safe_identity(scenario: Scenario, kind: str, suffix: str) -> None:
    conn, service, client = scenario
    if kind == "unexpected":
        client.result = _stories(*_IDS, 999999)
    elif kind == "mixed":
        client.result = _stories(999999)
    elif kind == "invalid":
        client.result = _stories(-1)
    elif kind == "response":
        client.result = types.photos.Photos([], [])
    elif kind == "vector":
        result = _stories(*_IDS)
        del result.stories
        client.result = result
    elif kind == "transport":
        client.failure = ValueError("private-caption peer=private-identity")
    elif kind == "normalization":
        client.result = _stories(*_IDS)
        cursor = service._profiles.next_due_refresh(now=200)
        assert cursor is not None and cursor.page_cursor_json is not None
        state = cast(dict[str, object], json.loads(cursor.page_cursor_json))
        cast(dict[str, object], state["request"])["reported_count"] = -1
        with write_transaction(conn):
            conn.execute("UPDATE entity_profile_refresh_state SET page_cursor_json=?", (json.dumps(state),))
    elif kind == "sidecar":
        client.result = types.stories.Stories(
            2, [types.StoryItemDeleted(value) for value in _IDS], [], [types.User(cast(int, "private-identity"))]
        )
    retained = _retained(conn)
    cursor = service._profiles.next_due_refresh(now=200)
    section = conn.execute("SELECT observed_at,payload_json FROM entity_detail_sections").fetchall()
    await _run(service)
    reason = "story_validation:" + suffix
    assert conn.execute("SELECT status,reason,retry_at FROM entity_profile_refresh_state").fetchone() == (
        "failed",
        reason,
        failure_retry_at(200),
    )
    assert conn.execute("SELECT observed_at,payload_json FROM entity_detail_sections").fetchall() == section
    assert _retained(conn) == retained
    resumed = service._profiles.next_due_refresh(now=failure_retry_at(200))
    assert cursor is not None and resumed is not None
    assert (resumed.generation, resumed.acquisition_cursor, resumed.page_cursor_json) == (
        cursor.generation,
        cursor.acquisition_cursor,
        cursor.page_cursor_json,
    )
    assert all(str(value) not in reason for value in _IDS)
    assert "private-identity" not in "\n".join(conn.iterdump())
    assert "private-caption" not in "\n".join(conn.iterdump())


@pytest.mark.parametrize("section", ["current_stories", "pinned_stories", "common_chats"])
async def test_unavailable_reference_is_identified_before_rpc(scenario: Scenario, section: str) -> None:
    conn, service, client = scenario
    client.session.available = False
    with write_transaction(conn):
        conn.execute("UPDATE entity_profile_refresh_state SET next_section=?", (section,))
    await _run(service)
    assert len(client.requests) == 1
    reason = "valueerror" if section == "common_chats" else "story_validation:reference_unavailable"
    assert conn.execute("SELECT reason FROM entity_profile_refresh_state").fetchone() == (reason,)


async def test_invalid_story_cursor_is_identified_before_rpc(scenario: Scenario) -> None:
    conn, service, client = scenario
    cursor = service._profiles.next_due_refresh(now=200)
    assert cursor is not None and cursor.page_cursor_json is not None
    state = cast(dict[str, object], json.loads(cursor.page_cursor_json))
    cast(dict[str, object], state["request"])["ids"] = []
    with write_transaction(conn):
        conn.execute("UPDATE entity_profile_refresh_state SET page_cursor_json=?", (json.dumps(state),))
    await _run(service)
    assert len(client.requests) == 1
    assert conn.execute("SELECT reason FROM entity_profile_refresh_state").fetchone() == (
        "story_validation:cursor_invalid",
    )


async def test_transport_wrapper_preserves_typed_category(scenario: Scenario) -> None:
    conn, service, client = scenario
    client.failure = StoryProfileValidationError("cursor_invalid")
    await _run(service)
    assert conn.execute("SELECT reason FROM entity_profile_refresh_state").fetchone() == (
        "story_validation:cursor_invalid",
    )


@pytest.mark.parametrize("kind", ["rpc", "timeout", "flood_wait"])
async def test_other_story_failures_keep_existing_retry_contract(scenario: Scenario, kind: str) -> None:
    conn, service, client = scenario
    if kind == "rpc":
        client.failure = BadRequestError(None, "UNKNOWN_STORY_SYMBOL")
        reason, retry_at = "badrequesterror", failure_retry_at(200)
    elif kind == "timeout":
        client.failure = TimeoutError("private-caption")
        reason, retry_at = "timeouterror", failure_retry_at(200)
    else:
        client.failure = TelegramRpcThrottled(retry_after_seconds=7)
        reason, retry_at = "flood_wait", 207
    await _run(service)
    assert conn.execute("SELECT reason,retry_at FROM entity_profile_refresh_state").fetchone() == (reason, retry_at)


@pytest.mark.parametrize("fence", ["generation", "page_cursor"])
async def test_stale_story_failure_cannot_publish_diagnostic(scenario: Scenario, fence: str) -> None:
    conn, service, client = scenario
    section = conn.execute("SELECT * FROM entity_detail_sections").fetchall()

    def invalidate() -> None:
        with write_transaction(conn):
            if fence == "generation":
                conn.execute("UPDATE entity_profile_refresh_state SET generation=generation+1,reason='new_cursor'")
            else:
                conn.execute("UPDATE entity_profile_refresh_state SET page_cursor_json='{}',reason='new_cursor'")

    client.before_response = invalidate
    await _run(service)
    assert conn.execute("SELECT reason FROM entity_profile_refresh_state").fetchone() == ("new_cursor",)
    assert conn.execute("SELECT * FROM entity_detail_sections").fetchall() == section


async def test_queued_story_failure_rechecks_account(scenario: Scenario) -> None:
    conn, service, client = scenario
    writer = _QueuedWriter(conn)
    service._deps = replace(service._deps, writer=cast(SQLiteWriter, writer))
    client.before_response = lambda: setattr(writer, "pause", True)
    retained = _retained(conn)
    task = asyncio.create_task(_run(service))
    try:
        await asyncio.wait_for(writer.waiting.wait(), timeout=2)
        service._deps = replace(
            service._deps, full_user_auth_scope=lambda: TelegramAuthScope(AUTH_SCOPE_VERSION, 78, 2, 99)
        )
        writer.release.set()
        await task
        assert _retained(conn) == retained
        assert conn.execute("SELECT reason,retry_at FROM entity_profile_refresh_state").fetchone() == (
            "auth_scope_changed",
            scope_changed_retry_at(200),
        )
    finally:
        writer.release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("count", [-1, True, sys.maxsize + 1, "private-identity"])
def test_diagnostic_rejects_unsafe_counts(count: object) -> None:
    with pytest.raises(ValueError, match="invalid story validation count"):
        StoryProfileValidationError("ids_missing", requested=cast(int, count))


def test_diagnostic_rejects_unknown_category_and_preserves_zero_counts() -> None:
    with pytest.raises(ValueError, match="invalid story validation category"):
        StoryProfileValidationError(cast(StoryProfileValidationCategory, "private-identity"))
    error = StoryProfileValidationError("ids_missing", requested=2, returned=0, missing=2, unexpected=0)
    assert str(error) == "story_validation:ids_missing:requested=2:returned=0:missing=2:unexpected=0"
    assert error.args == (error.reason,)
    assert len(StoryProfileValidationError("ids_missing", requested=sys.maxsize).reason) < 100


@pytest.mark.parametrize("section", ["current_stories", "pinned_stories"])
async def test_existing_duplicate_and_incomplete_story_acceptance_is_unchanged(section: str) -> None:
    client = _DirectClient(types.stories.Stories(2, [types.StoryItemSkipped(_IDS[0], _DATE, _END)] * 2, [], []))
    page = await TelethonUserProfileGateway(client).fetch_profile_collection_page(
        UserReference(42, 123), section, {"phase": "details", "ids": [_IDS[0]], "listing_terminal": True}
    )
    assert page.terminal and len(page.items) == 2
    assert all(not item.detail_complete for item in page.items)


async def test_unhashable_identity_keeps_original_type_error() -> None:
    result = types.StoryItemDeleted(cast(int, [_IDS[0]]))
    client = _DirectClient(types.stories.Stories(1, [result], [], []))
    with pytest.raises(TypeError):
        await TelethonUserProfileGateway(client).fetch_profile_collection_page(
            UserReference(42, 123), "current_stories", {"phase": "details", "ids": [_IDS[0]]}
        )


async def test_expired_pinned_story_is_not_filtered() -> None:
    story = types.StoryItem(_IDS[0], _DATE, _END, types.MessageMediaEmpty(), pinned=True, caption="Retained pinned")
    page = await TelethonUserProfileGateway(
        _DirectClient(types.stories.Stories(1, [story], [], []))
    ).fetch_profile_collection_page(
        UserReference(42, 123), "pinned_stories", {"phase": "details", "ids": [_IDS[0]], "listing_terminal": True}
    )
    assert len(page.items) == 1 and page.items[0].fields["expire_date"] == _END.isoformat()
