"""Missing detail IDs retain accepted knowledge and restart a bounded listing."""

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import cast

import pytest
from jsonschema import Draft202012Validator
from telethon.tl import types
from telethon.tl.functions.stories import GetPeerStoriesRequest, GetStoriesByIDRequest

from mcp_telegram import server
from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.collection_repository import merge_semantic_fields
from mcp_telegram.entity_profile.contracts import UserReference
from mcp_telegram.entity_profile.field_manifest import PROFILE_FIELD_MANIFEST
from mcp_telegram.entity_profile.refresh import RefreshLimits, scope_changed_retry_at
from mcp_telegram.entity_profile.semantic_normalization import normalize_collection_item
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_gateway import TelethonUserProfileGateway
from mcp_telegram.tools import entity_info
from tests.test_channel_contact_failure_identity import _QueuedWriter, _run
from tests.test_entity_profile_progressive import _test_service
from tests.test_profile_collection_pages import Database
from tests.test_profile_collection_pages import _Client as _DirectClient
from tests.test_profile_collection_pages import database as database
from tests.test_profile_read_integration import registered_validator as registered_validator
from tests.test_story_profile_diagnostics import _DATE, _END, _IDS, _Client


def _story(identifier: int, caption: str) -> types.StoryItem:
    area = types.MediaAreaUrl(types.MediaAreaCoordinates(1, 2, 3, 4, 5, radius=6), "https://example.invalid/story")
    return types.StoryItem(
        identifier, _DATE, _END, types.MessageMediaEmpty(), pinned=True, caption=caption, media_areas=[area]
    )


def _listing(*items: types.TypeStoryItem) -> types.stories.PeerStories:
    return types.stories.PeerStories(types.PeerStories(types.PeerUser(42), list(items)), [], [])


type Recovery = tuple[sqlite3.Connection, DaemonEntityInfoService, _Client, list[float]]


@pytest.fixture
async def recovery(database: Database) -> AsyncIterator[Recovery]:
    _path, conn, _repo = database
    clock = [100.0]
    client = _Client(
        types.users.UserFull(
            types.UserFull(42, types.PeerSettings(), types.PeerNotifySettings(), 0, about="Retained profile"),
            [],
            [types.User(42, first_name="Retained")],
        )
    )
    gateway = TelethonUserProfileGateway(client, now_provider=lambda: clock[0])
    service = _test_service(conn, limits=RefreshLimits(), account_id=77)
    service._deps = replace(service._deps, user_profile_port=gateway, now_provider=lambda: clock[0])
    with write_transaction(conn):
        conn.execute("UPDATE entity_profile_refresh_state SET next_section='full_profile'")
    try:
        await _run(service)
        assert conn.execute("SELECT profile_owner_account_id FROM entity_details").fetchone() == (77,)
        with write_transaction(conn):
            conn.execute("UPDATE entity_profile_refresh_state SET next_section='current_stories',acquisition_cursor=0")
        client.result = _listing(_story(_IDS[0], "Old A"), _story(_IDS[1], "Old B"))
        await _run(service)
        assert _payload(service)["complete"] is True
        published = _payload(service)["published"]
        assert isinstance(published, dict)
        clock[0] = 200.0
        assert service._profiles.request_section_refresh(42, ("current_stories",), now=200, pair_mode="disabled")
        client.result = _listing(*(types.StoryItemSkipped(identifier, _DATE, _END, live=True) for identifier in _IDS))
        await _run(service)
        assert _payload(service)["complete"] is False
        assert _payload(service)["published"] == published
        clock[0] = 300.0
        client.result = types.stories.Stories(1, [_story(_IDS[0], "New A")], [], [])
        yield conn, service, client, clock
    finally:
        await service.shutdown()


def _payload(service: DaemonEntityInfoService) -> dict[str, object]:
    payload = service._profiles.read_section_payload(42, "current_stories")
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def _fact(conn: sqlite3.Connection, identifier: int) -> tuple[str, str, str, int]:
    row = cast(
        tuple[str, str, str, int] | None,
        conn.execute(
            "SELECT fields_json,coverage_json,provenance_json,observed_at FROM entity_profile_collection_facts "
            "WHERE owner_account_id=77 AND entity_id=42 AND section='current_stories' AND item_key=?",
            (str(identifier),),
        ).fetchone(),
    )
    assert row is not None
    return row


async def _assert_retained_story_page(
    service: DaemonEntityInfoService, monkeypatch: pytest.MonkeyPatch, registered_validator: Draft202012Validator
) -> None:
    @asynccontextmanager
    async def connection():
        async def get_entity_info(**kwargs: object):
            return await service.get_entity_info(kwargs)

        yield SimpleNamespace(get_entity_info=get_entity_info)

    monkeypatch.setattr(entity_info, "_bounded_daemon_connection", connection)
    tool = await entity_info.get_entity_info(
        entity_info.GetEntityInfo(exact_entity_id=42, section="current_stories", limit=100)
    )
    result = server._project_tool_result("get_entity_info", tool, server._CallTelemetry())
    assert not result.is_error and result.content == []
    wire = cast(dict[str, object], result.structured_content)
    registered_validator.validate(wire)
    page = cast(dict[str, object], wire["page"])
    assert page["source"] == "published" and page["returned_count"] == 2
    captions: dict[str, object] = {}
    for item in cast(list[dict[str, object]], page["items"]):
        value = cast(dict[str, object], item["value"])
        caption = cast(dict[str, object], value["caption"])
        assert caption["is_telegram_content"] is True and value["live"] is True
        captions[str(item["item_key"])] = caption["text"]
    assert captions == {str(_IDS[0]): "New A", str(_IDS[1]): "Old B"}


async def test_partial_details_keep_facts_ages_published_baseline_then_fresh_listing_completes(
    recovery: Recovery, monkeypatch: pytest.MonkeyPatch, registered_validator: Draft202012Validator
) -> None:
    conn, service, client, clock = recovery
    omitted = _fact(conn, _IDS[1])
    assert json.loads(omitted[0])["caption"] == "Old B"
    assert json.loads(omitted[2])["item.caption"]["observed_at"] == 100
    assert json.loads(omitted[2])["item.media"]["observed_at"] == 100
    assert json.loads(omitted[0])["live"] is True
    baseline = _payload(service)["published"]
    cursor = service._profiles.next_due_refresh(now=300)
    assert cursor is not None
    await _run(service)
    payload = _payload(service)
    assert payload["complete"] is False and payload["comparable_membership"] is False
    assert payload["published"] == baseline
    assert payload["diagnostics"] == ["story_validation:ids_missing:requested=2:returned=1:missing=1:unexpected=0"]
    assert _fact(conn, _IDS[1]) == omitted
    assert json.loads(_fact(conn, _IDS[0])[0])["caption"] == "New A"
    assert json.loads(_fact(conn, _IDS[0])[0])["live"] is True
    assert json.loads(_fact(conn, _IDS[0])[2])["item.live"]["observed_at"] == 200
    assert conn.execute(
        "SELECT count(*) FROM entity_profile_change_events WHERE change_key=? AND after_json='null'",
        (f"collection.current_stories.{_IDS[1]}",),
    ).fetchone() == (0,)
    assert conn.execute(
        "SELECT status,retry_at,next_section,generation FROM entity_profile_refresh_state"
    ).fetchone() == ("failed", 360, "current_stories", cursor.generation)
    cursor = service._profiles.next_due_refresh(now=360)
    assert cursor is not None and cursor.page_cursor_json is not None
    state = cast(dict[str, object], json.loads(cursor.page_cursor_json))
    assert state.get("request", {}) == {} and state["scan"] == 1 and state["baseline"] == baseline

    await _assert_retained_story_page(service, monkeypatch, registered_validator)

    calls = len(client.requests)
    clock[0] = 359.0
    await _run(service)
    assert len(client.requests) == calls
    clock[0] = 360.0
    client.result = _listing(_story(_IDS[0], "Fresh A"))
    await _run(service)
    assert isinstance(client.requests[-1], GetPeerStoriesRequest)
    payload = _payload(service)
    assert payload["complete"] is True and payload["scan"] == 1
    assert _fact(conn, _IDS[1]) == omitted
    assert json.loads(omitted[0])["constructor"] == "StoryItemSkipped"
    assert conn.execute("SELECT next_section FROM entity_profile_refresh_state").fetchone() == ("pinned_stories",)


@pytest.mark.parametrize("deleted", [False, True])
async def test_missing_is_not_deletion_but_returned_deleted_item_is_explicit(recovery: Recovery, deleted: bool) -> None:
    conn, service, client, _clock = recovery
    omitted = _fact(conn, _IDS[1])
    client.result = types.stories.Stories(1, [types.StoryItemDeleted(_IDS[0])] if deleted else [], [], [])
    await _run(service)
    assert _payload(service)["complete"] is False
    assert _fact(conn, _IDS[1]) == omitted
    assert json.loads(_fact(conn, _IDS[1])[0])["constructor"] == "StoryItemSkipped"
    assert json.loads(_fact(conn, _IDS[1])[0])["caption"] == "Old B"
    assert json.loads(_fact(conn, _IDS[0])[0])["constructor"] == ("StoryItemDeleted" if deleted else "StoryItemSkipped")
    response = await service.get_entity_info({"entity_id": 42, "section": "current_stories"})
    page = cast(dict[str, object], cast(dict[str, object], response["data"])["page"])
    assert page["returned_active_count"] == (1 if deleted else 2)


@pytest.mark.parametrize(
    ("section", "partial"), [("current_stories", False), ("pinned_stories", False), ("pinned_stories", True)]
)
async def test_story_collection_commit_rechecks_queued_account(recovery: Recovery, section: str, partial: bool) -> None:
    conn, service, client, _clock = recovery
    returned = _IDS[:1] if partial else _IDS
    client.result = types.stories.Stories(len(returned), [_story(value, "New") for value in returned], [], [])
    with write_transaction(conn):
        conn.execute("UPDATE entity_profile_refresh_state SET next_section=?", (section,))
    facts = conn.execute("SELECT * FROM entity_profile_collection_facts").fetchall()
    writer = _QueuedWriter(conn)
    service._deps = replace(service._deps, writer=cast(SQLiteWriter, writer))
    client.before_response = lambda: setattr(writer, "pause", True)
    task = asyncio.create_task(_run(service))
    try:
        await asyncio.wait_for(writer.waiting.wait(), timeout=2)
        service._deps = replace(
            service._deps, full_user_auth_scope=lambda: TelegramAuthScope(AUTH_SCOPE_VERSION, 78, 2, 99)
        )
        writer.release.set()
        await task
        assert conn.execute("SELECT * FROM entity_profile_collection_facts").fetchall() == facts
        assert conn.execute("SELECT reason,retry_at,next_section FROM entity_profile_refresh_state").fetchone() == (
            "auth_scope_changed",
            scope_changed_retry_at(300),
            section,
        )
    finally:
        writer.release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_partial_commit_rolls_back_all_facts_when_cursor_write_fails(recovery: Recovery) -> None:
    conn, service, _client, _clock = recovery
    facts = conn.execute("SELECT * FROM entity_profile_collection_facts").fetchall()
    sections = conn.execute("SELECT * FROM entity_detail_sections").fetchall()
    with write_transaction(conn):
        conn.execute(
            "CREATE TRIGGER reject_story_advance BEFORE UPDATE OF page_cursor_json ON entity_profile_refresh_state "
            "BEGIN SELECT RAISE(ABORT,'story cursor rejected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="story cursor rejected"):
        await _run(service)
    assert conn.execute("SELECT * FROM entity_profile_collection_facts").fetchall() == facts
    assert conn.execute("SELECT * FROM entity_detail_sections").fetchall() == sections


async def test_missing_details_keep_bounded_chunk_and_pinned_continuation() -> None:
    requested = list(range(1, 102))
    client = _DirectClient(
        types.stories.Stories(99, [types.StoryItemDeleted(value) for value in requested[:99]], [], [])
    )
    page = await TelethonUserProfileGateway(client).fetch_profile_collection_page(
        UserReference(42, 123), "pinned_stories", {"phase": "details", "ids": requested, "listing_terminal": True}
    )
    request = client.requests[0]
    assert isinstance(request, GetStoriesByIDRequest) and len(request.id) == 100
    assert page.reason is None and len(page.items) == 99 and not page.terminal
    assert page.next_cursor is not None and page.next_cursor["ids"] == [101]
    assert page.diagnostics == ("story_validation:ids_missing:requested=100:returned=99:missing=1:unexpected=0",)

    after = {"offset_id": 900000}
    client = _DirectClient(types.stories.Stories(1, [_story(_IDS[0], "Pinned")], [], []))
    page = await TelethonUserProfileGateway(client).fetch_profile_collection_page(
        UserReference(42, 123),
        "pinned_stories",
        {"phase": "details", "ids": _IDS, "after": after, "listing_terminal": False},
    )
    assert page.reason is None and page.next_cursor == after and not page.terminal
    assert page.items[0].fields["expire_date"] == _END.isoformat()


@pytest.mark.parametrize(("old_id", "new_id"), [(_IDS[0], _IDS[1]), (1, True), (0, 0), (False, False), (None, None)])
def test_changed_or_invalid_story_id_does_not_inherit_other_details(old_id: object, new_id: object) -> None:
    old = {"constructor": "StoryItem", "id": old_id, "caption": "Old"}
    new = {"constructor": "StoryItemSkipped", "id": new_id, "live": True}
    merged = merge_semantic_fields(old, new, {})
    assert isinstance(merged, dict)
    assert "caption" not in merged


def test_effective_story_union_does_not_change_raw_acquisition_rules() -> None:
    raw = types.StoryItemSkipped(_IDS[0], _DATE, _END, live=True)
    vars(raw)["caption"] = "Injected"
    fields, _coverage, diagnostics = normalize_collection_item(raw)
    assert "caption" not in PROFILE_FIELD_MANIFEST["StoryItemSkipped"].fields
    assert "caption" not in fields and fields["live"] is True
    assert diagnostics
