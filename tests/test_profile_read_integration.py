"""Local profile selections preserve canonical facts at the registered MCP boundary."""

import json
import sqlite3
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from jsonschema import Draft202012Validator
from telethon.tl import types

from mcp_telegram import server
from mcp_telegram.entity_profile.collection_repository import store_semantic_snapshot
from mcp_telegram.entity_profile.contracts import ObservationBoundary, ProfileFieldObservation
from mcp_telegram.entity_profile.reader import read_profile
from mcp_telegram.entity_profile.refresh import RefreshLimits
from mcp_telegram.entity_profile.repository import EntityProfileRepository
from mcp_telegram.entity_profile.semantic_normalization import normalize_collection_item, normalize_semantic_profile
from mcp_telegram.sync_transactions import write_savepoint
from mcp_telegram.tools import entity_info
from mcp_telegram.tools._base import TOOL_REGISTRY, ToolResult, structured_result
from tests.test_entity_profile_progressive import _test_service


@pytest.fixture(scope="module")
def registered_validator() -> Draft202012Validator:
    schema = TOOL_REGISTRY["get_entity_info"].output_schema
    assert schema is not None
    Draft202012Validator.check_schema(schema)
    assert len(json.dumps(schema, separators=(",", ":"))) < 400_000
    return Draft202012Validator(schema)


@pytest.fixture
def conn(make_synced_db: Callable[[], sqlite3.Connection]) -> sqlite3.Connection:
    conn = make_synced_db()
    with write_savepoint(conn):
        conn.execute("INSERT INTO entities(id,type,name,updated_at) VALUES (42,'user','Retained',1)")
        conn.execute("INSERT INTO dialogs(dialog_id,type,name) VALUES (42,'user','Retained')")
        conn.execute(
            "INSERT INTO entity_details(entity_id,detail_json,fetched_at,profile_owner_account_id) VALUES(42,?,100,77)",
            (
                json.dumps(
                    {
                        "schema": 1,
                        "id": 42,
                        "type": "user",
                        "name": "Retained",
                        "about": "legacy useful",
                        "avatar_count": 0,
                    }
                ),
            ),
        )
        full = types.UserFull(
            id=42,
            settings=types.PeerSettings(),
            notify_settings=types.PeerNotifySettings(),
            common_chats_count=0,
            note=types.TextWithEntities(
                " 💬 note ", [types.MessageEntityFormattedDate(1, 2, datetime(2025, 1, 1, tzinfo=UTC), relative=False)]
            ),
            business_work_hours=types.BusinessWorkHours("UTC", [types.BusinessWeeklyOpen(60, 120)], open_now=False),
        )
        assert full.note is not None
        full.note.entities = [object(), *full.note.entities]
        snapshot = normalize_semantic_profile(
            types.User(id=42, phone="123", first_name="", last_name="Юникод💬"),
            full,
            target_id=42,
            owner_account_id=77,
            observation=ObservationBoundary(100, 101),
        )
        store_semantic_snapshot(conn, snapshot, 1)
        EntityProfileRepository(conn, section_ttl_seconds=300).apply_profile_update(
            42, {"phone": None}, observation=ProfileFieldObservation(77, 3, 150, "update")
        )
        store_semantic_snapshot(conn, snapshot, 2)
    return conn


def _publish(  # noqa: PLR0913
    conn: sqlite3.Connection,
    section: str,
    values: list[object],
    *,
    generation: int = 1,
    complete: bool = True,
    owner: int = 77,
    entity: int = 42,
) -> None:
    with write_savepoint(conn):
        for index, value in enumerate(values):
            fields, coverage, _ = normalize_collection_item(value)
            conn.execute(
                "INSERT OR REPLACE INTO entity_profile_collection_facts VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    owner,
                    entity,
                    section,
                    str(index),
                    json.dumps(fields),
                    json.dumps({key: {"state": fact.state.value} for key, fact in coverage.items()}),
                    "{}",
                    1,
                    100,
                ),
            )
            conn.execute(
                "INSERT INTO entity_profile_collection_membership VALUES (?,?,?,?,?,?,?,?,?,?)",
                (owner, entity, section, generation, 0, str(index), "remote", "page", index, 1),
            )
        payload = {
            "generation": generation,
            "scan": 0,
            "complete": complete,
            "reported_count": len(values),
            "retained_count": len(values),
        }
        if complete:
            payload["published"] = {"owner_account_id": owner, "generation": generation, "scan": 0, "complete": True}
        conn.execute(
            "INSERT INTO entity_detail_sections(entity_id,section,status,observed_at,payload_json) VALUES (?,?,'fresh',100,?) "
            "ON CONFLICT(entity_id,section) DO UPDATE SET payload_json=excluded.payload_json",
            (entity, section, json.dumps(payload)),
        )


@pytest.mark.parametrize(
    "section", ["common_chats", "avatar_history", "saved_music", "saved_gifts", "current_stories", "pinned_stories"]
)
async def test_registered_default_and_collection_outputs_are_closed_and_lossless(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    registered_validator: Draft202012Validator,
    section: Literal[
        "common_chats", "avatar_history", "saved_music", "saved_gifts", "current_stories", "pinned_stories"
    ],
) -> None:
    date = datetime(2025, 1, 1, tzinfo=UTC)
    music = types.Document(
        8,
        987654321,
        b"private-ref",
        date,
        "audio/ogg",
        99,
        4,
        [types.DocumentAttributeAudio(12, title=" song ", waveform=b"\x00\xff")],
    )
    photo = types.Photo(
        10,
        987654321,
        b"private-ref",
        date,
        [types.PhotoSize("m", 100, 200, 12)],
        4,
        video_sizes=[types.VideoSize("v", 300, 400, 99, 1.25)],
    )
    gift = types.StarGift(9, music, 5, 3, title=" gift ")
    story = types.StoryItem(
        10,
        date,
        date,
        types.MessageMediaPhoto(photo=photo),
        caption=" 💬 story ",
        entities=[types.MessageEntityBold(1, 2)],
        pinned=False,
    )
    values = {
        "common_chats": types.Chat(10, " Chat💬 ", types.ChatPhotoEmpty(), 0, date, 1),
        "avatar_history": photo,
        "saved_music": music,
        "saved_gifts": types.SavedStarGift(date=date, gift=gift),
        "current_stories": types.StoryItemDeleted(10),
        "pinned_stories": story,
    }
    _publish(conn, section, [values[section]])
    service = _test_service(conn, limits=RefreshLimits(), account_id=77)
    monkeypatch.setattr(service, "_write_profiles", AsyncMock(side_effect=AssertionError("read queued writer")))
    monkeypatch.setattr(service.refresh_coordinator, "enqueue", MagicMock(side_effect=AssertionError("read enrolled")))

    @asynccontextmanager
    async def connection():
        async def get_entity_info(**kwargs: object):
            return await service.get_entity_info(kwargs)

        yield SimpleNamespace(get_entity_info=get_entity_info)

    monkeypatch.setattr(entity_info, "_bounded_daemon_connection", connection)
    before = conn.total_changes
    for selected in (None, section):
        tool = await entity_info.get_entity_info(entity_info.GetEntityInfo(exact_entity_id=42, section=selected))
        assert not tool.is_error
        result = cast(
            dict, server._project_tool_result("get_entity_info", tool, server._CallTelemetry()).structured_content
        )
        assert result["semantic"]["fields"]["user.phone"] is None
        phone_value = next(row["value"] for row in result["semantic"]["field_evidence"] if row["path"] == "user.phone")
        assert phone_value["availability"] == "retained"
        assert phone_value["state"] == "unknown" and phone_value["authoritative"] is None
        assert result["semantic"]["fields"]["full_user.about"] is None
        assert result["semantic"]["fields"]["full_user.note"]["entities"][0] is None
        assert result["semantic"]["fields"]["full_user.note"]["entities"][1]["offset"] == 1
        assert result["semantic"]["fields"]["full_user.note"]["entities"][1]["length"] == 2
        assert result["display_name"] == "Retained"
        assert result["semantic"]["fields"]["user.first_name"]["text"] == ""
        assert result["semantic"]["fields"]["full_user.common_chats_count"] == 0
        assert result["semantic"]["fields"]["full_user.business_work_hours"]["open_now"] is False
        assert "private-ref" not in json.dumps(result) and "987654321" not in json.dumps(result)
        assert result["semantic"]["fields"]["full_user.note"]["text"]["text"] == " 💬 note "
        registered_validator.validate(result)
        if selected:
            item = result["page"]["items"][0]
            assert item["state"] == ("deleted" if section == "current_stories" else "retained")
            _assert_rich_item(section, item, date)
            if section == "current_stories":
                assert result["page"]["returned_active_count"] == 0
    assert conn.total_changes == before
    await service.shutdown()


def _assert_rich_item(section: str, item: dict, date: datetime) -> None:
    if section == "avatar_history":
        assert item["value"]["sizes"][0]["w"] == 100
        assert item["value"]["video_sizes"][0]["video_start_ts"] == 1.25
        assert item["value"]["date"] == date.isoformat()
    elif section == "pinned_stories":
        assert item["value"]["media"]["photo"]["id"] == 10
        assert item["value"]["caption"]["text"] == " 💬 story "
    elif section == "saved_music":
        assert item["value"]["attributes"][0]["waveform"] == {"encoding": "base64", "data": "AP8="}
    elif section == "common_chats":
        assert item["value"]["title"]["text"] == " Chat💬 "
    elif section == "saved_gifts":
        assert item["value"]["gift"]["title"]["text"] == " gift "


def test_published_membership_stable_latest_values_and_active_cursor_restarts(conn: sqlite3.Connection) -> None:
    _publish(conn, "avatar_history", [types.PhotoEmpty(1), types.PhotoEmpty(2)])
    first = cast(dict, read_profile(conn, 77, 42, 200, {"section": "avatar_history", "limit": 1})["page"])
    token = first["next_cursor"]
    with write_savepoint(conn):
        conn.execute(
            "UPDATE entity_profile_collection_facts SET fields_json=?,observed_at=180 WHERE item_key='1'",
            (json.dumps({"constructor": "PhotoEmpty", "id": 99}),),
        )
    second = cast(
        dict, read_profile(conn, 77, 42, 200, {"section": "avatar_history", "limit": 1, "cursor": token})["page"]
    )
    assert second["items"][0]["value"]["id"] == 99
    assert second["items"][0]["age_seconds"] == 20
    for args in ({"cursor": token + "x"}, {"cursor": token, "section": "saved_music"}):
        with pytest.raises(ValueError):
            read_profile(conn, 77, 42, 200, {"section": "avatar_history", **args})
    with pytest.raises(ValueError):
        read_profile(conn, 88, 42, 200, {"section": "avatar_history", "cursor": token})
    _publish(conn, "saved_music", [types.DocumentEmpty(1), types.DocumentEmpty(2)], complete=False)
    active = cast(dict, read_profile(conn, 77, 42, 200, {"section": "saved_music", "limit": 1})["page"])["next_cursor"]
    with write_savepoint(conn):
        conn.execute(
            "UPDATE entity_detail_sections SET payload_json=json_set(payload_json,'$.retained_count',3) WHERE section='saved_music'"
        )
    with pytest.raises(ValueError, match="selection changed"):
        read_profile(conn, 77, 42, 200, {"section": "saved_music", "cursor": active})


def test_actual_wire_preserves_only_new_typed_subtree_nulls() -> None:
    payload = {
        "legacy": None,
        "semantic": {
            "fields": {"user.phone": None, "full_user.note": {"constructor": "TextWithEntities", "entities": [None]}}
        },
        "page": {"items": [{"before": None, "after": None}]},
    }
    profile = cast(
        dict,
        server._project_tool_result(
            "get_entity_info", ToolResult(structured_content=payload), server._CallTelemetry()
        ).structured_content,
    )
    other = cast(
        dict,
        server._project_tool_result(
            "get_sync_status", ToolResult(structured_content=payload), server._CallTelemetry()
        ).structured_content,
    )
    assert "legacy" not in profile
    assert profile["semantic"]["fields"]["user.phone"] is None
    assert profile["page"]["items"][0]["before"] is None
    assert "user.phone" not in other["semantic"]["fields"]


@pytest.mark.parametrize("section", ["avatar_history"])
def test_real_legacy_receipt_survives_partial_page_failure_and_retry(conn: sqlite3.Connection, section: str) -> None:
    from mcp_telegram.entity_profile.contracts import ProfileCollectionItem, ProfileCollectionPage
    from mcp_telegram.entity_profile.repository import EntitySectionCommit

    values = (
        [{"photo_id": n, "date": None} for n in (1, 2, 3)]
        if section == "avatar_history"
        else [{"id": n, "name": f"Chat {n}", "type": "chat", "username": None} for n in (1, 2, 3)]
    )
    repo = EntityProfileRepository(conn, section_ttl_seconds=300)
    with write_savepoint(conn):
        detail = cast(
            dict,
            json.loads(
                cast(tuple[str], conn.execute("SELECT detail_json FROM entity_details WHERE entity_id=42").fetchone())[
                    0
                ]
            ),
        )
        detail[section] = values
        conn.execute("UPDATE entity_details SET detail_json=?,fetched_at=999 WHERE entity_id=42", (json.dumps(detail),))
        conn.execute(
            "INSERT INTO entity_detail_sections(entity_id,section,status,observed_at,payload_json) VALUES(42,?,'fresh',100,?)",
            (section, json.dumps(values)),
        )
        assert repo.request_section_refresh(42, (section,), now=105, pair_mode="enabled")
        cursor = repo.next_due_refresh(now=105)
        assert cursor is not None and cursor.next_section == section
        fields, coverage, _ = normalize_collection_item(
            types.PhotoEmpty(4) if section == "avatar_history" else types.ChatEmpty(4)
        )
        page = ProfileCollectionPage(
            42,
            section,
            (ProfileCollectionItem("4", fields, coverage),),
            ObservationBoundary(105, 106),
            4,
            {"offset": 1},
        )
        assert repo.commit_section(
            cursor,
            EntitySectionCommit({}, collection_page=page, field_observation=ProfileFieldObservation(77, 4, 106, "rpc")),
            now=106,
        )
        cursor = repo.next_due_refresh(now=106)
        assert cursor is not None
        assert repo.mark_section_failure(cursor, now=110, reason="acquisition_failed", retry_at=170)
    result = cast(dict, read_profile(conn, 77, 42, 200, {"section": section})["page"])
    assert result["source"] == "legacy"
    assert result["coverage"] == "legacy_unknown"
    legacy_source = next(source for source in result["available_sources"] if source["source"] == "legacy")
    assert "generation" not in legacy_source and "scan" not in legacy_source
    assert len(result["items"]) == 3
    assert all(item["observed_at"] is None and item["age_seconds"] is None for item in result["items"])
    assert result["legacy_evidence"]["observed_at"] == 100
    assert result["legacy_evidence"]["age_seconds"] == 100
    assert result["legacy_evidence"]["observation_scope"] == "legacy_section"
    assert result["legacy_evidence"]["stored_status"] == "fresh"
    assert result["legacy_evidence"]["traversal_coverage"] == "unknown"
    _assert_legacy_wire(result)
    active = next(source["cursor"] for source in result["available_sources"] if source["source"] == "active")
    current = cast(dict, read_profile(conn, 77, 42, 200, {"section": section, "cursor": active})["page"])
    assert current["source"] == "active" and current["returned_count"] == 1
    assert current["items"][0]["observed_at"] == 106
    reopened = EntityProfileRepository(conn, section_ttl_seconds=300)
    with write_savepoint(conn):
        assert reopened.request_section_refresh(42, (section,), now=201, pair_mode="enabled")
    retained = cast(dict, read_profile(conn, 77, 42, 210, {"section": section})["page"])
    assert retained["legacy_evidence"] == {**result["legacy_evidence"], "age_seconds": 110}


def _assert_legacy_wire(result: dict) -> None:
    # Validate the real projection/omission boundary against the registered page schema.
    schema = TOOL_REGISTRY["get_entity_info"].output_schema
    assert schema is not None
    page_schema = {**cast(dict, schema["properties"])["page"], "$defs": schema["$defs"]}
    wire = cast(
        dict,
        server._project_tool_result(
            "get_entity_info", structured_result({"page": result}), server._CallTelemetry()
        ).structured_content,
    )
    Draft202012Validator(page_schema).validate(wire["page"])
    assert all(item["observed_at"] is None for item in wire["page"]["items"])


async def test_history_uses_insertion_watermark_and_real_registered_wire(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, registered_validator: Draft202012Validator
) -> None:
    from mcp_telegram.entity_profile.profile_history import (
        HistoryObservation,
        ProfileHistoryMerge,
        append_profile_history,
    )

    def append(order: int, before: object, after: object) -> None:
        path = "user.first_name"
        observation = HistoryObservation(
            42, ProfileFieldObservation(77, order, 180, "update"), ObservationBoundary(170, 180)
        )
        with write_savepoint(conn):
            assert (
                append_profile_history(
                    conn,
                    ProfileHistoryMerge(
                        observation, {path: before}, {path: after}, frozenset({path}), frozenset({path}), {}
                    ),
                )
                == 1
            )

    append(100, "A", " B😀 ")
    append(99, " B😀 ", None)
    first = cast(dict, read_profile(conn, 77, 42, 200, {"section": "history", "limit": 1})["page"])
    assert first["items"][0]["after"] is None
    append(50, "late lower order", "later insertion")
    second = cast(
        dict,
        read_profile(conn, 77, 42, 200, {"section": "history", "limit": 1, "cursor": first["next_cursor"]})["page"],
    )
    assert second["items"][0]["after"]["text"] == " B😀 "
    assert second["watermark"] == first["watermark"] and second["has_more"] is False
    service = _test_service(conn, limits=RefreshLimits(), account_id=77)
    monkeypatch.setattr(service, "_write_profiles", AsyncMock(side_effect=AssertionError("read writer")))

    @asynccontextmanager
    async def connection():
        async def get_entity_info(**kwargs: object):
            return await service.get_entity_info(kwargs)

        yield SimpleNamespace(get_entity_info=get_entity_info)

    monkeypatch.setattr(entity_info, "_bounded_daemon_connection", connection)
    tool = await entity_info.get_entity_info(entity_info.GetEntityInfo(exact_entity_id=42, section="history"))
    wire = cast(dict, server._project_tool_result("get_entity_info", tool, server._CallTelemetry()).structured_content)
    registered_validator.validate(wire)
    assert wire["history_coverage"]["complete_past_history"] is False
    assert wire["page"]["items"][1]["after"] is None
    assert wire["page"]["items"][2]["after"]["is_telegram_content"] is True
    await service.shutdown()


def test_profile_cursor_process_restart_and_account_fencing(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_telegram import pagination

    _publish(conn, "saved_music", [types.DocumentEmpty(1), types.DocumentEmpty(2)])
    page = cast(dict, read_profile(conn, 77, 42, 200, {"section": "saved_music", "limit": 1})["page"])
    wrong = cast(dict, read_profile(conn, 88, 42, 200, {}))
    assert wrong["semantic"]["fields"] == {}
    assert all(summary["status"] == "not_observed" for summary in wrong["collections"].values())
    monkeypatch.setattr(pagination, "_TOKEN_SECRET", b"replacement-process-key")
    with pytest.raises(ValueError):
        read_profile(conn, 77, 42, 200, {"section": "saved_music", "cursor": page["next_cursor"]})


@pytest.mark.parametrize("phone", ["123", None])
def test_retained_value_and_latest_full_user_nonreturn_have_separate_evidence(
    make_synced_db: Callable[[], sqlite3.Connection], phone: str | None
) -> None:
    conn = make_synced_db()
    repo = EntityProfileRepository(conn, section_ttl_seconds=300)
    full = types.UserFull(42, types.PeerSettings(), types.PeerNotifySettings(), 0)
    from mcp_telegram.entity_profile.contracts import SuppliedProfileUser
    from mcp_telegram.entity_profile.updates import project_profile_update, project_user_profile

    push = project_profile_update(types.UpdateUserPhone(42, phone or ""))
    assert push is not None
    with write_savepoint(conn):
        conn.execute("INSERT INTO entities(id,type,updated_at) VALUES(42,'user',1)")
        repo.apply_supplied_user(
            SuppliedProfileUser(42, push.semantic_fields, push.semantic_coverage, push.fields, False, False),
            observation=ProfileFieldObservation(77, 1, 10, "update"),
        )
    patch = cast(dict, read_profile(conn, 77, 42, 200, {})["semantic"])
    assert patch["acquisition"] == {"availability": "not_observed"}
    snapshot = normalize_semantic_profile(
        types.User(42), full, target_id=42, owner_account_id=77, observation=ObservationBoundary(100, 101)
    )
    with write_savepoint(conn):
        assert store_semantic_snapshot(conn, snapshot, 2) is not None
    semantic = cast(dict, read_profile(conn, 77, 42, 200, {})["semantic"])
    value = next(row["value"] for row in semantic["field_evidence"] if row["path"] == "user.phone")
    assert semantic["fields"]["user.phone"] == (
        None if phone is None else {"text": "123", "is_telegram_content": True, "content_kind": "snippet"}
    )
    assert value["availability"] == "retained"
    assert value["state"] == ("cleared" if phone is None else "present")
    assert (value["observed_at"], value["source"], value["age_seconds"], value["authoritative"]) == (
        10,
        "update",
        190,
        True,
    )
    acquisition = semantic["acquisition"]
    assert (
        acquisition["observation_started_at"],
        acquisition["observation_completed_at"],
        acquisition["age_seconds"],
        acquisition["source"],
    ) == (100, 101, 99, "rpc")
    fact = next(row for row in acquisition["field_coverage"] if row["path"] == "user.phone")
    assert fact["state"] == "api_not_returned"
    # A later minimal supplied observation retains accepted evidence and the actual RPC clock.
    minimal = project_user_profile(types.User(42, min=True))
    assert minimal is not None
    with write_savepoint(conn):
        repo.apply_supplied_user(
            SuppliedProfileUser(
                42,
                minimal.semantic_fields,
                minimal.semantic_coverage,
                minimal.fields,
                True,
                minimal.authoritative_photo,
            ),
            observation=ProfileFieldObservation(77, 3, 120, "update"),
        )
    later = cast(dict, read_profile(conn, 77, 42, 200, {})["semantic"])
    assert next(row["value"] for row in later["field_evidence"] if row["path"] == "user.phone") == value
    assert later["acquisition"] == acquisition


def test_real_collection_page_exposes_flat_provenance_and_retains_unknown_coverage(conn: sqlite3.Connection) -> None:
    from mcp_telegram.entity_profile.collection_repository import store_collection_page
    from mcp_telegram.entity_profile.contracts import (
        ProfileCollectionItem,
        ProfileCollectionPage,
    )
    from mcp_telegram.pagination import ProfileNavigationToken, encode_profile_navigation

    date = datetime(2025, 1, 1, tzinfo=UTC)
    first, coverage, _ = normalize_collection_item(types.Photo(9, 123, b"private-ref", date, [], 4))
    page = ProfileCollectionPage(
        42,
        "avatar_history",
        (ProfileCollectionItem("9", first, coverage),),
        ObservationBoundary(10, 11),
        1,
        terminal=True,
    )
    with write_savepoint(conn):
        store_collection_page(conn, page, owner=77, order=1, generation=1, cursor={"scan": 0})
        partial_fields, partial_coverage, _ = normalize_collection_item(
            types.Photo(9, 123, b"private-ref", None, [], 4)
        )
        partial = ProfileCollectionPage(
            42,
            "avatar_history",
            (
                ProfileCollectionItem(
                    "9",
                    partial_fields,
                    partial_coverage,
                ),
            ),
            ObservationBoundary(100, 101),
            1,
            terminal=True,
        )
        store_collection_page(conn, partial, owner=77, order=2, generation=1, cursor={"scan": 0})
    token = encode_profile_navigation(ProfileNavigationToken(77, 42, "avatar_history", "published", 1, 0))
    page_result = cast(dict, read_profile(conn, 77, 42, 200, {"section": "avatar_history", "cursor": token})["page"])
    value = next(row["value"] for row in page_result["items"][0]["field_evidence"] if row["path"] == "item.date")
    assert page_result["items"][0]["value"]["date"] == date.isoformat()
    assert (
        value["observed_at"],
        value["observation_started_at"],
        value["observation_completed_at"],
        value["source"],
        value["age_seconds"],
    ) == (10, 10, 11, "rpc", 190)
    assert value["state"] == "present" and value["authoritative"] is True
    assert page_result["items"][0]["acquisition"] == {"availability": "unknown"}


async def test_common_chats_overview_pages_and_trace_share_published_baseline(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, registered_validator: Draft202012Validator
) -> None:
    from mcp_telegram.account_trace_sqlite import common_chat_ids
    from mcp_telegram.daemon_account_trace import _known_common_chats

    _publish(conn, "common_chats", [types.ChatEmpty(7), types.ChannelForbidden(7, 1, "Channel")])
    conn.execute(
        "UPDATE entity_details SET detail_json=json_set(detail_json, '$.common_chats', json('[{\"id\":999}]'))"
    )
    section = cast(
        dict,
        json.loads(
            cast(
                str,
                conn.execute(
                    "SELECT payload_json FROM entity_detail_sections WHERE entity_id=42 AND section='common_chats'"
                ).fetchone()[0],
            )
        ),
    )
    section["published"].update(
        observation_started_at=90, observation_completed_at=100, reported_count=2, retained_unique_count=2
    )
    section.update(generation=2, complete=False, observation_started_at=190, observation_completed_at=195)
    conn.execute(
        "UPDATE entity_detail_sections SET payload_json=? WHERE entity_id=42 AND section='common_chats'",
        (json.dumps(section),),
    )
    first = cast(dict, read_profile(conn, 77, 42, 200, {"section": "common_chats", "limit": 1})["page"])
    second = cast(
        dict,
        read_profile(conn, 77, 42, 200, {"section": "common_chats", "limit": 1, "cursor": first["next_cursor"]})[
            "page"
        ],
    )
    assert first["source"] == "published" and first["age_seconds"] == 100
    assert first["reported_count"] == 2 and first["retained_unique_count"] == 2
    assert first["has_more"] and not second["has_more"]
    assert common_chat_ids(conn, 77, 42, 200) == [-7, -1000000000007]
    assert common_chat_ids(conn, 78, 42, 200) == []
    diagnostic = _known_common_chats(conn, 77, 42, 200)
    assert [item["dialog_id"] for item in cast(list[dict], diagnostic["items"])] == [-7, -1000000000007]
    assert all(item["local_state"]["status"] == "not_synced" for item in cast(list[dict], diagnostic["items"]))
    conn.commit()
    service = _test_service(conn, limits=RefreshLimits(), account_id=77)
    monkeypatch.setattr(service.refresh_coordinator, "enqueue", MagicMock(side_effect=AssertionError("read enrolled")))

    @asynccontextmanager
    async def connection():
        async def get_entity_info(**kwargs: object):
            return await service.get_entity_info(kwargs)

        yield SimpleNamespace(get_entity_info=get_entity_info)

    monkeypatch.setattr(entity_info, "_bounded_daemon_connection", connection)
    tool = await entity_info.get_entity_info(entity_info.GetEntityInfo(exact_entity_id=42))
    result = cast(
        dict, server._project_tool_result("get_entity_info", tool, server._CallTelemetry()).structured_content
    )
    registered_validator.validate(result)
    assert len(result["relationships"]["common_chats"]) == 2
    assert result["relationships"]["common_chats"] == result["type_specific"]["common_chats"]
    assert result["common_chats_page"]["returned_count"] == 2
    await service.shutdown()


def test_migration_normalizes_owner_bound_common_chats_without_inventing_coverage(conn: sqlite3.Connection) -> None:
    from mcp_telegram.sync_db import _apply_migration_96

    detail = {
        "common_chats": [
            {"id": -7, "type": "group", "name": "Group"},
            {"id": -1000000000007, "type": "channel", "name": "Channel"},
        ]
    }
    conn.execute("UPDATE entity_details SET detail_json=? WHERE entity_id=42", (json.dumps(detail),))
    conn.execute(
        "INSERT INTO entity_detail_sections(entity_id,section,status,observed_at,payload_json) VALUES(42,'common_chats','fresh',100,?)",
        (json.dumps(detail["common_chats"]),),
    )
    conn.execute("DELETE FROM schema_version WHERE version=96")
    conn.commit()
    assert _apply_migration_96(conn, 95) == 96
    page = cast(dict, read_profile(conn, 77, 42, 200, {"section": "common_chats"})["page"])
    assert page["coverage"] == "retained_partial" and page["age_seconds"] is None
    assert page["reported_count"] is None and page["retained_unique_count"] == 2
    assert [source["source"] for source in page["available_sources"]] == ["published"]
    assert [item["item_key"] for item in page["items"]] == ["channel:7", "chat:7"]
    assert all(item["observed_at"] is None and item["age_seconds"] is None for item in page["items"])
    assert all("constructor" not in item["value"] and not item["detail_complete"] for item in page["items"])
    assert [item["value"]["peer_kind"] for item in page["items"]] == ["channel", "chat"]
    schema = TOOL_REGISTRY["get_entity_info"].output_schema
    assert schema is not None
    wire = server._project_tool_result("get_entity_info", structured_result({"page": page}), server._CallTelemetry())
    page_schema = {**cast(dict, schema["properties"])["page"], "$defs": schema["$defs"]}
    Draft202012Validator(page_schema).validate(cast(dict, wire.structured_content)["page"])
    assert (
        conn.execute(
            "SELECT observed_at FROM entity_detail_sections WHERE entity_id=42 AND section='common_chats'"
        ).fetchone()[0]
        == 100
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert _apply_migration_96(conn, 96) == 96


def test_migration_keeps_unknown_owner_and_retains_valid_siblings(conn: sqlite3.Connection) -> None:
    from mcp_telegram.sync_db import _apply_migration_96

    mixed = {"common_chats": [{"id": -7, "type": "group", "name": "Group"}, {"id": 8, "type": ["unknown"]}]}
    conn.execute("UPDATE entity_details SET detail_json=? WHERE entity_id=42", (json.dumps(mixed),))
    conn.execute("INSERT INTO entities(id,type,name,updated_at) VALUES(43,'user','Unknown owner',1)")
    encoded = json.dumps({"common_chats": [{"id": -9, "type": "group", "name": "Private"}]})
    conn.execute("INSERT INTO entity_details(entity_id,detail_json,fetched_at) VALUES(43,?,1)", (encoded,))
    conn.execute("DELETE FROM schema_version WHERE version=96")
    conn.commit()
    _apply_migration_96(conn, 95)
    page = cast(dict, read_profile(conn, 77, 42, 200, {"section": "common_chats"})["page"])
    assert page["returned_count"] == 1 and page["coverage"] == "retained_partial"
    assert (
        cast(
            dict,
            json.loads(
                cast(str, conn.execute("SELECT detail_json FROM entity_details WHERE entity_id=42").fetchone()[0])
            ),
        )
        == mixed
    )
    assert conn.execute("SELECT detail_json FROM entity_details WHERE entity_id=43").fetchone()[0] == encoded
    assert (
        conn.execute("SELECT count(*) FROM entity_profile_collection_membership WHERE entity_id=43").fetchone()[0] == 0
    )


def test_migration_preserves_existing_publication_and_rolls_back_atomically(conn: sqlite3.Connection) -> None:
    from mcp_telegram.sync_db import _apply_migration_96

    _publish(conn, "common_chats", [types.ChatEmpty(7)])
    detail = {"common_chats": [{"id": -9, "type": "group", "name": "Old"}]}
    conn.execute("UPDATE entity_details SET detail_json=? WHERE entity_id=42", (json.dumps(detail),))
    conn.execute("DELETE FROM schema_version WHERE version=96")
    conn.execute("DROP INDEX idx_profile_membership_lookup")
    conn.execute("CREATE INDEX idx_profile_membership_lookup ON entities(id)")
    conn.commit()
    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        _apply_migration_96(conn, 95)
    assert conn.execute("SELECT count(*) FROM entity_profile_collection_facts").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM schema_version WHERE version=96").fetchone()[0] == 0
    conn.execute("DROP INDEX idx_profile_membership_lookup")
    conn.commit()
    _apply_migration_96(conn, 95)
    page = cast(dict, read_profile(conn, 77, 42, 200, {"section": "common_chats"})["page"])
    assert page["coverage"] == "published_traversal"
    assert [item["value"]["id"] for item in page["items"]] == [7]
    assert (
        cast(
            dict,
            json.loads(
                cast(str, conn.execute("SELECT detail_json FROM entity_details WHERE entity_id=42").fetchone()[0])
            ),
        )
        == detail
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migrated_partial_baseline_survives_refresh_and_real_acquisition(conn: sqlite3.Connection) -> None:
    from mcp_telegram.entity_profile.contracts import ProfileCollectionItem, ProfileCollectionPage
    from mcp_telegram.entity_profile.repository import EntitySectionCommit
    from mcp_telegram.sync_db import _apply_migration_96

    detail = {"common_chats": [{"id": -7, "type": "group", "name": "Old"}]}
    conn.execute("UPDATE entity_details SET detail_json=? WHERE entity_id=42", (json.dumps(detail),))
    conn.execute("DELETE FROM schema_version WHERE version=96")
    conn.commit()
    _apply_migration_96(conn, 95)
    repo = EntityProfileRepository(conn, section_ttl_seconds=300)
    with write_savepoint(conn):
        assert repo.request_section_refresh(42, ("common_chats",), now=105, pair_mode="enabled")
        cursor = repo.next_due_refresh(now=105)
        assert cursor is not None
        fields, coverage, _ = normalize_collection_item(types.ChatEmpty(7))
        page = ProfileCollectionPage(
            42,
            "common_chats",
            (ProfileCollectionItem("chat:7", fields, coverage),),
            ObservationBoundary(105, 106),
            2,
            {"max_id": 7},
        )
        assert repo.commit_section(
            cursor,
            EntitySectionCommit({}, collection_page=page, field_observation=ProfileFieldObservation(77, 4, 106, "rpc")),
            now=106,
        )
        cursor = repo.next_due_refresh(now=106)
        assert cursor is not None
        assert repo.mark_section_failure(cursor, now=110, reason="acquisition_failed", retry_at=170)
    retained = cast(dict, read_profile(conn, 77, 42, 120, {"section": "common_chats"})["page"])
    assert retained["coverage"] == "retained_partial" and retained["source"] == "published"
    assert retained["items"][0]["value"]["id"] == 7
    assert retained["items"][0]["observed_at"] == 106
    persisted = cast(
        dict,
        json.loads(
            cast(
                str,
                conn.execute(
                    "SELECT fields_json FROM entity_profile_collection_facts WHERE entity_id=42 AND section='common_chats' AND item_key='chat:7'"
                ).fetchone()[0],
            )
        ),
    )
    assert persisted["title"] == "Old" and "peer_kind" not in persisted
    with write_savepoint(conn):
        assert repo.request_section_refresh(42, ("common_chats",), now=201, pair_mode="enabled")
        cursor = repo.next_due_refresh(now=201)
        assert cursor is not None
        fields, coverage, _ = normalize_collection_item(types.ChatEmpty(7))
        page = ProfileCollectionPage(
            42,
            "common_chats",
            (ProfileCollectionItem("chat:7", fields, coverage),),
            ObservationBoundary(201, 202),
            1,
            terminal=True,
        )
        assert repo.commit_section(
            cursor,
            EntitySectionCommit({}, collection_page=page, field_observation=ProfileFieldObservation(77, 5, 202, "rpc")),
            now=202,
        )
    published = cast(dict, read_profile(conn, 77, 42, 210, {"section": "common_chats"})["page"])
    assert published["coverage"] == "published_traversal" and published["age_seconds"] == 8
    assert published["reported_count"] == 1
    assert published["items"][0]["observed_at"] == 202
    summary = cast(dict, read_profile(conn, 77, 42, 210, {})["collections"])["common_chats"]
    assert not summary["comparable_membership"]


def test_migration_preserves_baseline_alongside_active_scan(conn: sqlite3.Connection) -> None:
    from mcp_telegram.sync_db import _apply_migration_96

    _publish(conn, "common_chats", [types.ChatEmpty(7)], generation=2, complete=False)
    conn.execute("DELETE FROM entity_profile_collection_membership WHERE entity_id=42 AND section='common_chats'")
    conn.execute(
        "UPDATE entity_profile_collection_facts SET item_key='chat:7' WHERE entity_id=42 AND section='common_chats'"
    )
    conn.execute(
        "INSERT INTO entity_profile_collection_membership VALUES(77,42,'common_chats',2,0,'chat:7','remote','page',0,1)"
    )
    detail = {"common_chats": [{"id": -7, "type": "group", "name": "A"}, {"id": -8, "type": "group", "name": "B"}]}
    conn.execute(
        "INSERT INTO entity_profile_refresh_state(entity_id,status,updated_at,next_section,page_cursor_json) VALUES(42,'pending',1,'common_chats',?)",
        (json.dumps({"baseline": None, "scan": 0}),),
    )
    conn.execute("UPDATE entity_details SET detail_json=? WHERE entity_id=42", (json.dumps(detail),))
    conn.execute("DELETE FROM schema_version WHERE version=96")
    conn.commit()
    _apply_migration_96(conn, 95)
    page = cast(dict, read_profile(conn, 77, 42, 200, {"section": "common_chats"})["page"])
    assert page["source"] == "published" and page["coverage"] == "retained_partial"
    state = cast(
        dict,
        json.loads(
            cast(
                str,
                conn.execute("SELECT page_cursor_json FROM entity_profile_refresh_state WHERE entity_id=42").fetchone()[
                    0
                ],
            )
        ),
    )
    assert state["baseline"]["complete"] is False and state["baseline"]["retained_unique_count"] == 2
    assert [item["value"]["id"] for item in page["items"]] == [7, 8]
    assert page["items"][0]["value"]["constructor"] == "ChatEmpty"
    assert page["items"][1]["value"]["peer_kind"] == "chat"
    active = next(source["cursor"] for source in page["available_sources"] if source["source"] == "active")
    active_page = cast(dict, read_profile(conn, 77, 42, 200, {"section": "common_chats", "cursor": active})["page"])
    assert active_page["returned_count"] == 1 and active_page["items"][0]["value"]["id"] == 7


async def test_service_common_chats_uses_canonical_collection_page(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from mcp_telegram.entity_profile.contracts import ProfileCollectionPage
    from mcp_telegram.entity_profile.repository import EntityRefreshCursor
    from mcp_telegram.models import DialogType

    service = _test_service(conn, limits=RefreshLimits(), account_id=77)
    page = ProfileCollectionPage(42, "common_chats", (), ObservationBoundary(100, 101), 0, terminal=True)
    fetch_page = AsyncMock(return_value=page)
    gateway = SimpleNamespace(fetch_profile_collection_page=fetch_page)
    service._deps = replace(service._deps, user_profile_port=gateway)
    monkeypatch.setattr(service, "_collection_user_reference", lambda *_args: object())
    cursor = EntityRefreshCursor(42, "common_chats", 0, None)
    commit = await service._acquire_profile_section(cursor, DialogType.SERVICE)
    assert commit.collection_page is page and commit.detail_patch == {}
    fetch_page.assert_awaited_once()
    assert service._not_applicable_section_patch("common_chats") == {}
    await service.shutdown()


@pytest.mark.parametrize("section", ["common_chats", "avatar_history"])
def test_old_cursor_never_borrows_new_collection_receipt(conn: sqlite3.Connection, section: str) -> None:
    values: list[object] = (
        [types.ChatEmpty(1), types.ChatEmpty(2)]
        if section == "common_chats"
        else [types.PhotoEmpty(1), types.PhotoEmpty(2)]
    )
    _publish(conn, section, values)
    payload = cast(
        dict,
        json.loads(
            cast(
                str,
                conn.execute(
                    "SELECT payload_json FROM entity_detail_sections WHERE entity_id=42 AND section=?", (section,)
                ).fetchone()[0],
            )
        ),
    )
    payload.update(observation_started_at=90, observation_completed_at=100, retained_unique_count=2)
    payload["published"].update(
        observation_started_at=90, observation_completed_at=100, retained_unique_count=2, reported_count=2
    )
    if section == "common_chats":
        payload["complete"] = False
        payload["published"]["complete"] = False
    conn.execute(
        "UPDATE entity_detail_sections SET payload_json=? WHERE entity_id=42 AND section=?",
        (json.dumps(payload), section),
    )
    first = cast(dict, read_profile(conn, 77, 42, 150, {"section": section, "limit": 1})["page"])
    old = payload["published"]
    payload.update(
        generation=2,
        scan=0,
        complete=True,
        observation_started_at=190,
        observation_completed_at=200,
        reported_count=9,
        retained_unique_count=9,
        published={
            "owner_account_id": 77,
            "generation": 2,
            "scan": 0,
            "complete": True,
            "observation_started_at": 190,
            "observation_completed_at": 200,
            "reported_count": 9,
            "retained_unique_count": 9,
        },
        previous_published=old,
    )
    conn.execute(
        "UPDATE entity_detail_sections SET payload_json=? WHERE entity_id=42 AND section=?",
        (json.dumps(payload), section),
    )
    second = cast(
        dict, read_profile(conn, 77, 42, 210, {"section": section, "limit": 1, "cursor": first["next_cursor"]})["page"]
    )
    assert second["coverage"] == ("retained_partial" if section == "common_chats" else "published_traversal")
    assert second["age_seconds"] == 110 and second["reported_count"] == 2 and second["retained_unique_count"] == 2
    payload.pop("previous_published")
    conn.execute(
        "UPDATE entity_detail_sections SET payload_json=? WHERE entity_id=42 AND section=?",
        (json.dumps(payload), section),
    )
    unknown = cast(
        dict, read_profile(conn, 77, 42, 210, {"section": section, "limit": 1, "cursor": first["next_cursor"]})["page"]
    )
    assert unknown["coverage"] == "observation_unknown"
    assert (
        unknown["age_seconds"] is None
        and unknown["reported_count"] is None
        and unknown["retained_unique_count"] is None
    )


def test_partial_peer_identity_merges_only_same_family() -> None:
    from mcp_telegram.entity_profile.collection_repository import merge_semantic_fields

    partial = {"peer_kind": "chat", "id": 7, "title": "Known"}
    same = cast(dict, merge_semantic_fields(partial, {"constructor": "ChatEmpty", "id": 7}, {}))
    different = cast(dict, merge_semantic_fields(partial, {"constructor": "ChannelForbidden", "id": 7}, {}))
    assert same["title"] == "Known"
    assert "title" not in different


def test_migration_normalizes_inflight_published_receipts_for_other_collections(conn: sqlite3.Connection) -> None:
    from mcp_telegram.entity_profile.contracts import ProfileCollectionItem, ProfileCollectionPage
    from mcp_telegram.entity_profile.repository import EntitySectionCommit
    from mcp_telegram.sync_db import _apply_migration_96

    _publish(conn, "avatar_history", [types.PhotoEmpty(7), types.PhotoEmpty(8)], generation=100)
    payload = cast(
        dict,
        json.loads(
            cast(
                str,
                conn.execute(
                    "SELECT payload_json FROM entity_detail_sections WHERE entity_id=42 AND section='avatar_history'"
                ).fetchone()[0],
            )
        ),
    )
    payload["published"]["identifiable"] = True
    conn.execute(
        "UPDATE entity_detail_sections SET payload_json=? WHERE entity_id=42 AND section='avatar_history'",
        (json.dumps(payload),),
    )
    conn.commit()
    repo = EntityProfileRepository(conn, section_ttl_seconds=300)
    with write_savepoint(conn):
        assert repo.request_section_refresh(42, ("avatar_history",), now=105, pair_mode="enabled")
        cursor = repo.next_due_refresh(now=105)
        assert cursor is not None
        fields, coverage, _ = normalize_collection_item(types.PhotoEmpty(7))
        page = ProfileCollectionPage(
            42,
            "avatar_history",
            (ProfileCollectionItem("7", fields, coverage),),
            ObservationBoundary(105, 106),
            2,
            {"offset": 1},
        )
        assert repo.commit_section(
            cursor,
            EntitySectionCommit({}, collection_page=page, field_observation=ProfileFieldObservation(77, 4, 106, "rpc")),
            now=106,
        )
    state = cast(
        dict,
        json.loads(
            cast(
                str,
                conn.execute("SELECT page_cursor_json FROM entity_profile_refresh_state WHERE entity_id=42").fetchone()[
                    0
                ],
            )
        ),
    )
    state["baseline"].pop("complete")
    conn.execute("UPDATE entity_profile_refresh_state SET page_cursor_json=? WHERE entity_id=42", (json.dumps(state),))
    payload = cast(
        dict,
        json.loads(
            cast(
                str,
                conn.execute(
                    "SELECT payload_json FROM entity_detail_sections WHERE entity_id=42 AND section='avatar_history'"
                ).fetchone()[0],
            )
        ),
    )
    payload["published"].pop("complete")
    payload["previous_published"].pop("complete")
    conn.execute(
        "UPDATE entity_detail_sections SET payload_json=? WHERE entity_id=42 AND section='avatar_history'",
        (json.dumps(payload),),
    )
    conn.execute("DELETE FROM schema_version WHERE version=96")
    conn.commit()
    _apply_migration_96(conn, 95)
    state = cast(
        dict,
        json.loads(
            cast(
                str,
                conn.execute("SELECT page_cursor_json FROM entity_profile_refresh_state WHERE entity_id=42").fetchone()[
                    0
                ],
            )
        ),
    )
    payload = cast(
        dict,
        json.loads(
            cast(
                str,
                conn.execute(
                    "SELECT payload_json FROM entity_detail_sections WHERE entity_id=42 AND section='avatar_history'"
                ).fetchone()[0],
            )
        ),
    )
    assert state["baseline"]["complete"] is True
    assert payload["published"]["complete"] is True and payload["previous_published"]["complete"] is True
    assert "observation_completed_at" not in state["baseline"]
    with write_savepoint(conn):
        cursor = repo.next_due_refresh(now=107)
        assert cursor is not None
        fields, coverage, _ = normalize_collection_item(types.PhotoEmpty(8))
        page = ProfileCollectionPage(
            42,
            "avatar_history",
            (ProfileCollectionItem("8", fields, coverage),),
            ObservationBoundary(107, 108),
            2,
            terminal=True,
        )
        assert repo.commit_section(
            cursor,
            EntitySectionCommit({}, collection_page=page, field_observation=ProfileFieldObservation(77, 5, 108, "rpc")),
            now=108,
        )
    summary = cast(dict, read_profile(conn, 77, 42, 200, {})["collections"])["avatar_history"]
    assert summary["comparable_membership"] is True, json.dumps(summary)


def test_migration_preserves_matching_published_bounds_across_refresh(conn: sqlite3.Connection) -> None:
    from mcp_telegram.entity_profile.contracts import ProfileCollectionItem, ProfileCollectionPage
    from mcp_telegram.entity_profile.repository import EntitySectionCommit
    from mcp_telegram.sync_db import _apply_migration_96

    _publish(conn, "avatar_history", [types.PhotoEmpty(7), types.PhotoEmpty(8)], generation=100)
    payload = cast(
        dict,
        json.loads(
            cast(
                str,
                conn.execute(
                    "SELECT payload_json FROM entity_detail_sections WHERE entity_id=42 AND section='avatar_history'"
                ).fetchone()[0],
            )
        ),
    )
    payload.update(
        owner_account_id=77, observation_started_at=90, observation_completed_at=100, retained_unique_count=2
    )
    payload["published"].pop("complete")
    conn.execute(
        "UPDATE entity_detail_sections SET payload_json=? WHERE entity_id=42 AND section='avatar_history'",
        (json.dumps(payload),),
    )
    conn.execute("DELETE FROM schema_version WHERE version=96")
    conn.commit()
    _apply_migration_96(conn, 95)
    repo = EntityProfileRepository(conn, section_ttl_seconds=300)
    with write_savepoint(conn):
        assert repo.request_section_refresh(42, ("avatar_history",), now=105, pair_mode="enabled")
        cursor = repo.next_due_refresh(now=105)
        assert cursor is not None
        fields, coverage, _ = normalize_collection_item(types.PhotoEmpty(7))
        page = ProfileCollectionPage(
            42,
            "avatar_history",
            (ProfileCollectionItem("7", fields, coverage),),
            ObservationBoundary(105, 106),
            2,
            {"offset": 1},
        )
        assert repo.commit_section(
            cursor,
            EntitySectionCommit({}, collection_page=page, field_observation=ProfileFieldObservation(77, 4, 106, "rpc")),
            now=106,
        )
    retained = cast(dict, read_profile(conn, 77, 42, 200, {"section": "avatar_history"})["page"])
    assert retained["source"] == "published" and retained["age_seconds"] == 100
    assert retained["observation_started_at"] == 90 and retained["observation_completed_at"] == 100
    assert retained["reported_count"] == 2 and retained["retained_unique_count"] == 2


async def test_common_chat_observer_reports_only_accepted_publications_and_failures(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from mcp_telegram.daemon_entity_info import _ProfileSectionContext, _ProfileSectionFailure
    from mcp_telegram.entity_profile.contracts import ProfileCollectionItem, ProfileCollectionPage
    from mcp_telegram.entity_profile.repository import EntitySectionCommit
    from mcp_telegram.models import DialogType

    service = _test_service(conn, limits=RefreshLimits(), account_id=77)
    monkeypatch.setattr(service, "_rpc_attempt_count", lambda: 0)
    seen: list[dict[str, int]] = []

    class Observer:
        broken = False

        def observe_membership(self, counts: dict[str, int], *, source: str) -> None:
            assert source == "common_chats_refresh" and not conn.in_transaction
            seen.append(counts)
            if self.broken:
                raise RuntimeError("observer failed")

    observer = Observer()
    service._deps = replace(service._deps, profile_observer=observer)
    repo = service._profiles
    context = _ProfileSectionContext(None, 0, False, "disabled", False)
    with write_savepoint(conn):
        assert repo.request_section_refresh(42, ("common_chats",), now=105, pair_mode="enabled")
    cursor = repo.next_due_refresh(now=105)
    assert cursor is not None
    fields, coverage, _ = normalize_collection_item(types.ChatEmpty(7))
    page = ProfileCollectionPage(
        42,
        "common_chats",
        (ProfileCollectionItem("chat:7", fields, coverage),),
        ObservationBoundary(105, 106),
        2,
        {"max_id": 7},
    )
    assert await service._commit_profile_section_result(
        cursor,
        DialogType.USER,
        EntitySectionCommit({}, collection_page=page, field_observation=ProfileFieldObservation(77, 4, 106, "rpc")),
        context,
        now=106,
    )
    assert seen == []
    cursor = repo.next_due_refresh(now=106)
    assert cursor is not None
    fields, coverage, _ = normalize_collection_item(types.ChatEmpty(8))
    page = ProfileCollectionPage(
        42,
        "common_chats",
        (ProfileCollectionItem("chat:8", fields, coverage),),
        ObservationBoundary(107, 108),
        2,
        terminal=True,
    )
    commit = EntitySectionCommit({}, collection_page=page, field_observation=ProfileFieldObservation(77, 5, 108, "rpc"))
    assert await service._commit_profile_section_result(cursor, DialogType.USER, commit, context, now=108)
    assert seen == [{"published": 1}]
    assert not await service._commit_profile_section_result(cursor, DialogType.USER, commit, context, now=108)
    assert seen == [{"published": 1}]
    with write_savepoint(conn):
        assert repo.request_section_refresh(42, ("common_chats",), now=200, pair_mode="enabled")
    failure_cursor = repo.next_due_refresh(now=200)
    assert failure_cursor is not None
    failure = _ProfileSectionFailure(failure_cursor, DialogType.USER, context, 200, "acquisition_failed", 250)
    observer.broken = True
    assert await service._handle_profile_section_failure(failure) is not None
    assert seen == [{"published": 1}, {"failed": 1}]
    stale = _ProfileSectionFailure(cursor, DialogType.USER, context, 201, "acquisition_failed", 250)
    assert await service._handle_profile_section_failure(stale) is None
    assert seen == [{"published": 1}, {"failed": 1}]
    assert conn.execute(
        "SELECT status FROM entity_detail_sections WHERE entity_id=42 AND section='common_chats'"
    ).fetchone()[0] in {"stale", "unavailable"}
    await service.shutdown()
