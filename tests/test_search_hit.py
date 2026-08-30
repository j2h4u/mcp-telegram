"""Contract tests for the canonical compact SearchHit projection."""

from __future__ import annotations

from typing import cast

from jsonschema import validate  # type: ignore[import-untyped]

from mcp_telegram.message_content import MessageSnapshot, project_message_content
from mcp_telegram.models import ReadReactionEvent
from mcp_telegram.temporal import normalize_temporal_output_schema, project_temporal_response
from mcp_telegram.tools._base import TOOL_REGISTRY
from mcp_telegram.tools.message_view import MESSAGE_VIEW_SCHEMA
from mcp_telegram.tools.search_hit import SEARCH_HIT_SCHEMA, extract_search_snippet, project_search_hit


def _lifecycle(
    *,
    message_state: str = "sent",
    scheduled_at: int | None = None,
    published_at: int | None = 1_700_000_000,
) -> dict[str, object]:
    scheduled = message_state == "scheduled"
    return {
        "message_state": message_state,
        "visibility": "author_only" if scheduled else "chat_visible",
        "unpublished": scheduled,
        "published": not scheduled,
        "unseen": scheduled,
        "scheduled_at": scheduled_at,
        "published_at": published_at,
        "inclusion_basis": ["direct_message"],
    }


def _maximal_row() -> dict[str, object]:
    return {
        "dialog_id": -100,
        "dialog_name": "Search Chat",
        "message_id": 42,
        "sent_at": 1_700_000_000,
        "text": ("prefix " * 20) + "needle" + (" tail" * 25),
        "sender_id": 7,
        "sender_first_name": "Ada",
        "forum_topic_id": 9,
        "topic_title": "  Reports  ",
        "media_description": "photo attachment",
        "media_kind": "photo",
        "reaction_events": (
            {"reactor_id": 77, "emoji": "like", "reacted_at": 1_700_000_100},
            ReadReactionEvent(reactor_id=None, emoji="fire", reacted_at=None),
        ),
        "reaction_events_status": "partial",
        "read_at": 1_700_000_200,
    }


def test_search_hit_schema_and_maximal_projector_have_exact_field_parity() -> None:
    hit = project_search_hit(_maximal_row(), "needle", lifecycle=_lifecycle())
    properties = cast(dict[str, object], SEARCH_HIT_SCHEMA["properties"])

    assert set(hit) == set(properties)
    assert len(properties) == 20
    assert cast(list[str], SEARCH_HIT_SCHEMA["required"]) == [
        "dialog_id",
        "dialog_name",
        "msg_id",
        "anchor_call",
        "message_state",
        "visibility",
        "unpublished",
        "published",
        "unseen",
        "scheduled_at",
        "published_at",
        "inclusion_basis",
        "reaction_events",
        "reaction_events_status",
        "read_at",
    ]
    validate(instance=hit, schema=SEARCH_HIT_SCHEMA)


def test_registered_search_hit_schema_preserves_normalized_20_field_11_required_contract() -> None:
    registered = cast(dict[str, object], TOOL_REGISTRY["search_messages"].output_schema)
    properties = cast(dict[str, object], registered["properties"])
    results = cast(dict[str, object], properties["results"])
    item = cast(dict[str, object], results["items"])

    assert len(cast(dict[str, object], item["properties"])) == 20
    assert cast(list[str], item["required"]) == [
        "dialog_id",
        "msg_id",
        "anchor_call",
        "message_state",
        "visibility",
        "unpublished",
        "published",
        "unseen",
        "inclusion_basis",
        "reaction_events",
        "reaction_events_status",
    ]


def test_sent_search_hit_is_a_bounded_query_centered_discovery_result() -> None:
    hit = project_search_hit(_maximal_row(), "needle", lifecycle=_lifecycle())
    content = cast(dict[str, object], hit["content"])

    assert content["content_kind"] == "snippet"
    assert "needle" in cast(str, content["text"])
    assert len(cast(str, content["text"])) <= 156
    assert hit["topic"] == {"title": "Reports"}
    assert hit["media"] == {"type": "photo", "description": "photo attachment"}
    assert hit["sender"] == "Ada"
    assert hit["anchor_call"] == {
        "tool": "list_messages",
        "arguments": {"exact_dialog_id": -100, "anchor_message_id": 42},
    }


def test_search_snippet_preserves_exact_50_lead_150_body_and_no_match_fallback() -> None:
    text = ("a" * 80) + "needle" + ("b" * 200)
    expected_body = text[30:180]

    assert extract_search_snippet(text, "needle") == f"...{expected_body}..."
    assert extract_search_snippet(text, "absent") == (text[:150] + "...")


def test_search_hit_plain_snippet_does_not_expose_hidden_link_destination_or_markdown() -> None:
    target = "https://example.test/hidden"
    raw_text = ("prefix " * 18) + "needle" + (" tail" * 24)
    offset = raw_text.index("needle")
    rendered = project_message_content(
        MessageSnapshot(text=raw_text, text_links=((offset, len("needle"), target),))
    ).text
    assert rendered is not None and target in rendered

    row = {**_maximal_row(), "text": raw_text}
    hit = project_search_hit(row, "needle", lifecycle=_lifecycle())
    snippet = cast(str, cast(dict[str, object], hit["content"])["text"])

    assert "needle" in snippet
    assert target not in snippet
    assert "](" not in snippet


def test_scheduled_search_hit_keeps_lifecycle_without_inventing_history_anchor() -> None:
    lifecycle = _lifecycle(
        message_state="scheduled",
        scheduled_at=1_900_000_000,
        published_at=None,
    )
    hit = project_search_hit(_maximal_row(), "needle", lifecycle=lifecycle)
    anchor = cast(dict[str, object], hit["anchor_call"])
    arguments = cast(dict[str, object], anchor["arguments"])

    assert {field: hit[field] for field in lifecycle} == lifecycle
    assert arguments == {"exact_dialog_id": -100, "message_state": "scheduled"}
    assert "anchor_message_id" not in arguments


def test_sparse_search_hit_preserves_nullable_keys_and_dialog_zero_fallback() -> None:
    lifecycle = _lifecycle(published_at=None)
    hit = project_search_hit(
        {"message_id": 3, "sent_at": None, "text": None},
        "missing",
        lifecycle=lifecycle,
    )

    assert hit["dialog_id"] == 0
    assert hit["dialog_name"] is None
    assert hit["date"] is None
    assert hit["read_at"] is None
    assert hit["reaction_events"] == []
    assert hit["reaction_events_status"] == "unavailable"
    assert hit["sender"] == "(unknown user)"
    assert cast(dict[str, object], hit["content"])["text"] == "(no text)"
    assert "topic" not in hit
    assert "media" not in hit
    validate(instance=hit, schema=SEARCH_HIT_SCHEMA)


def test_search_hit_keeps_compact_sender_id_fallback() -> None:
    row = {"message_id": 3, "sent_at": 1_700_000_000, "dialog_id": -100, "sender_id": 88, "text": "needle"}

    hit = project_search_hit(row, "needle", lifecycle=_lifecycle())

    assert hit["sender"] == "(unknown user 88)"


def test_search_hit_timezone_schema_matches_wire_projection() -> None:
    envelope_schema: dict[str, object] = {
        "type": "object",
        "properties": {"hit": SEARCH_HIT_SCHEMA},
        "required": ["hit"],
        "additionalProperties": False,
    }
    normalized = cast(dict[str, object], normalize_temporal_output_schema(envelope_schema))
    hit = project_search_hit(
        _maximal_row(),
        "needle",
        lifecycle=_lifecycle(
            message_state="scheduled",
            scheduled_at=1_700_000_300,
            published_at=1_700_000_400,
        ),
    )
    wire = project_temporal_response({"hit": hit}, "Asia/Almaty")
    wire_hit = cast(dict[str, object], wire["hit"])

    assert wire_hit["date"] == "2023-11-15T04:13:20+06:00"
    assert wire_hit["read_at"] == "2023-11-15T04:16:40+06:00"
    assert wire_hit["scheduled_at"] == "2023-11-15T04:18:20+06:00"
    assert wire_hit["published_at"] == "2023-11-15T04:20:00+06:00"
    reaction_events = cast(list[dict[str, object]], wire_hit["reaction_events"])
    assert reaction_events[0]["reacted_at"] == "2023-11-15T04:15:00+06:00"
    assert reaction_events[1]["reacted_at"] is None
    assert cast(dict[str, object], wire["time_context"])["timezone"] == "Asia/Almaty"
    validate(instance=wire, schema=normalized)


def test_search_hit_reaction_events_keep_search_nullable_key_representation() -> None:
    hit = project_search_hit(_maximal_row(), "needle", lifecycle=_lifecycle())

    assert hit["reaction_events"] == [
        {"reactor_id": 77, "emoji": "like", "reacted_at": 1_700_000_100},
        {"reactor_id": None, "emoji": "fire", "reacted_at": None},
    ]
    assert hit["reaction_events_status"] == "partial"


def test_search_hit_has_no_message_view_only_fields() -> None:
    hit = project_search_hit(_maximal_row(), "needle", lifecycle=_lifecycle())
    message_view_fields = set(cast(dict[str, object], MESSAGE_VIEW_SCHEMA["properties"]))
    shared_fields = {
        "dialog_id",
        "msg_id",
        "sender",
        "topic",
        "content",
        "media",
        "reaction_events",
        "reaction_events_status",
        "read_at",
    }

    assert not ((message_view_fields - shared_fields) & set(hit))
    assert "sent_at" not in hit
    assert "out" not in hit
    assert "reply_context_ref" not in hit


def test_sent_search_hit_anchor_coordinates_open_the_exact_result_message() -> None:
    hit = project_search_hit(_maximal_row(), "needle", lifecycle=_lifecycle())
    anchor = cast(dict[str, object], hit["anchor_call"])

    assert hit["dialog_id"] == -100
    assert hit["msg_id"] == 42
    assert anchor["tool"] == "list_messages"
    assert anchor["arguments"] == {
        "exact_dialog_id": hit["dialog_id"],
        "anchor_message_id": hit["msg_id"],
    }
