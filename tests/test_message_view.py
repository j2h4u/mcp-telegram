from __future__ import annotations

from typing import cast

from mcp_telegram.models import ReadMessage
from mcp_telegram.temporal import format_timestamp, response_timezone
from mcp_telegram.tools._base import structured_result
from mcp_telegram.tools.message_view import MESSAGE_VIEW_SCHEMA, project_message_view, project_read_markers
from mcp_telegram.tools.reading import LIST_MESSAGES_OUTPUT_SCHEMA, _list_messages_structured_messages
from mcp_telegram.tools.unread import GET_INBOX_OUTPUT_SCHEMA, _structured_messages


def _shared_message() -> dict[str, object]:
    return {
        "message_id": 17,
        "sent_at": 1_700_000_000,
        "dialog_id": -42,
        "text": "untrusted body",
        "sender_id": 99,
        "effective_sender_id": 99,
        "sender_first_name": "Sender",
        "sender_username": "sender",
        "out": 0,
        "media_description": "attachment description",
        "media_kind": "document",
        "content_kind": "message_text",
        "forum_topic_id": 5,
        "topic_title": "Topic",
        "reply_to_msg_id": 12,
        "fwd_from_name": "Forward source",
        "post_author": "Post author",
        "edit_date": 1_700_000_100,
        "reactions_display": "[like]",
        "reaction_events": ({"reactor_id": 77, "emoji": "like", "reacted_at": 1_700_000_200},),
        "reaction_events_status": "complete",
        "read_at": 1_700_000_300,
    }


def test_list_and_inbox_share_one_message_view_contract() -> None:
    row = _shared_message()
    read_state = {
        "inbox_unread_count": 0,
        "inbox_cursor_state": "populated",
        "inbox_max_id_anchor": 17,
        "outbox_unread_count": 0,
        "outbox_cursor_state": "populated",
        "outbox_max_id_anchor": 17,
    }
    listed = _list_messages_structured_messages([row], read_state=read_state, dialog_type="user")[0]
    inbox = _structured_messages([row], read_state=read_state, dialog_type="user")[0]

    shared_fields = {
        "dialog_id",
        "msg_id",
        "sent_at",
        "sender",
        "out",
        "content",
        "media",
        "topic",
        "forward",
        "post_author",
        "edit_date",
        "reactions",
        "reaction_events",
        "reaction_events_status",
        "read_at",
        "read_markers",
    }
    assert {key: listed[key] for key in shared_fields} == {key: inbox[key] for key in shared_fields}
    assert "date" not in listed
    assert "date" not in inbox
    assert "reply_to_msg_id" not in listed
    assert "reply_to_msg_id" not in inbox
    assert "inline_markers" not in inbox
    assert listed["reply_context_ref"] == {"msg_id": 12, "in_page": False, "context_included": False}
    assert inbox["reply_context_ref"] == listed["reply_context_ref"]


def test_message_view_omits_optional_nulls_and_uses_identity_fallback() -> None:
    message = ReadMessage(
        message_id=17,
        sent_at=1_700_000_000,
        dialog_id=-42,
        effective_sender_id=99,
        sender_first_name="Sender",
        reaction_events=(),
    )
    view = project_message_view(message)
    assert view["sender"] == {"display_name": "Sender", "telegram_id": 99}
    assert view["sent_at"] == 1_700_000_000
    assert "content" not in view
    assert "media" not in view
    assert "topic" not in view
    assert "read_at" not in view
    assert "forward" not in view


def test_read_markers_are_projected_once_for_both_surfaces() -> None:
    rows = [
        {**_shared_message(), "message_id": 16, "sent_at": 1_699_999_900},
        {**_shared_message(), "message_id": 17},
    ]
    messages = [ReadMessage(**row) for row in rows]
    read_state = {
        "inbox_unread_count": 1,
        "inbox_cursor_state": "populated",
        "inbox_max_id_anchor": 16,
    }
    assert project_read_markers(messages, read_state=read_state, dialog_type="group") == {}
    assert project_read_markers(messages, read_state=read_state, dialog_type="user") == {
        16: {
            "kind": "i_read_up_to_here",
            "label": "[I read up to here]",
            "side": "inbox",
            "anchor_message_id": 16,
        },
        17: {
            "kind": "unread_by_me",
            "label": "[unread by me]",
            "side": "inbox",
            "anchor_message_id": 17,
        },
    }

    listed = _list_messages_structured_messages(rows, read_state=read_state, dialog_type="user")
    inbox = _structured_messages(rows, read_state=read_state, dialog_type="user")
    assert [item["read_markers"] for item in listed] == [item["read_markers"] for item in inbox]
    assert all("inline_markers" not in item for item in (*listed, *inbox))


def test_list_and_inbox_schemas_embed_the_same_canonical_message_contract() -> None:
    canonical_properties = cast(dict[str, object], MESSAGE_VIEW_SCHEMA["properties"])
    canonical_required = set(cast(list[str], MESSAGE_VIEW_SCHEMA["required"]))

    list_properties = cast(dict[str, object], LIST_MESSAGES_OUTPUT_SCHEMA["properties"])
    list_messages = cast(dict[str, object], list_properties["messages"])
    list_item = cast(dict[str, object], list_messages["items"])

    inbox_properties = cast(dict[str, object], GET_INBOX_OUTPUT_SCHEMA["properties"])
    dialogs = cast(dict[str, object], inbox_properties["dialogs"])
    dialog_item = cast(dict[str, object], dialogs["items"])
    dialog_properties = cast(dict[str, object], dialog_item["properties"])
    inbox_messages = cast(dict[str, object], dialog_properties["messages"])
    inbox_item = cast(dict[str, object], inbox_messages["items"])

    for item_schema in (list_item, inbox_item):
        item_properties = cast(dict[str, object], item_schema["properties"])
        assert {key: item_properties[key] for key in canonical_properties} == canonical_properties
        assert canonical_required <= set(cast(list[str], item_schema["required"]))


def test_structured_result_renders_canonical_event_times_once_in_requested_timezone() -> None:
    row = _shared_message()
    listed = _list_messages_structured_messages([row], dialog_type="group")[0]
    inbox = _structured_messages([row], read_state=None, dialog_type="group")[0]

    utc_result = structured_result({"listed": [listed], "inbox": [inbox]})
    utc_payload = cast(dict[str, object], utc_result.structured_content)
    utc_item = cast(list[dict[str, object]], utc_payload["listed"])[0]
    assert utc_item["sent_at"] == format_timestamp(1_700_000_000, "UTC")
    assert str(utc_item["sent_at"]).endswith("+00:00")

    timezone = "Asia/Almaty"
    token = response_timezone.set(timezone)
    try:
        result = structured_result({"listed": [listed], "inbox": [inbox]})
    finally:
        response_timezone.reset(token)

    payload = cast(dict[str, object], result.structured_content)
    for surface in ("listed", "inbox"):
        item = cast(list[dict[str, object]], payload[surface])[0]
        assert item["sent_at"] == format_timestamp(1_700_000_000, timezone)
        assert item["edit_date"] == format_timestamp(1_700_000_100, timezone)
        assert item["read_at"] == format_timestamp(1_700_000_300, timezone)
        events = cast(list[dict[str, object]], item["reaction_events"])
        assert events[0]["reacted_at"] == format_timestamp(1_700_000_200, timezone)
        assert "date" not in item
    assert cast(dict[str, object], payload["time_context"])["timezone"] == timezone
