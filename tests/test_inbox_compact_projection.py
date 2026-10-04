import json
from typing import cast

import pytest
from mcp.types import TextContent
from pydantic import ValidationError

from mcp_telegram.temporal import response_timezone
from mcp_telegram.tools._base import ToolResult
from mcp_telegram.tools.unread import (
    INBOX_MESSAGE_FIELDS,
    GetInbox,
    _drop_largest_inbox_preview,
    _project_inbox_response,
    _project_read_position_pending_entities,
    _structured_messages,
)


def _dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _list(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def _text(value: object) -> str:
    assert isinstance(value, str)
    return value


def _message(**values: object) -> dict[str, object]:
    return {
        "message_id": 1,
        "dialog_id": 10,
        "sent_at": 1700000000,
        "text": "hello",
        "content_kind": "message_text",
        **values,
    }


def _response(groups: list[dict[str, object]], **values: object) -> dict[str, object]:
    return {
        "ok": True,
        "data": {"groups": groups, "read_position_pending_count": 0, "read_position_pending_entities": [], **values},
    }


def _group(dialog_id: int = 10, **values: object) -> dict[str, object]:
    return {
        "dialog_id": dialog_id,
        "display_name": "Alice",
        "category": "user",
        "dialog_type": "User",
        "unread_count": 1,
        "messages": [_message(dialog_id=dialog_id)],
        **values,
    }


def _project(response: dict[str, object], **values: object) -> ToolResult:
    args = GetInbox.model_validate(values)
    token = response_timezone.set(args.timezone)
    try:
        return _project_inbox_response(args, response, applied_since_utc=None, has_inbox_filter=False)
    finally:
        response_timezone.reset(token)


def test_compact_message_clips_final_body_and_media_without_hidden_originals() -> None:
    body, media = "Ж" * 1000, "Фото" * 1000
    item = _structured_messages(
        [
            _message(
                text=body,
                media_description=media,
                media_kind="photo",
                formatting_text=body,
                formatting_entities=[{"offset": 0, "length": 1000, "type": "bold"}] * 1000,
                reactions_display=body,
                reaction_events=[{"emoji": "X"}] * 1000,
                fwd_from_name=body,
                post_author=body,
                service_action={"text": body},
            )
        ],
        read_state=None,
        dialog_type="User",
        preview_chars=32,
    )[0]
    assert set(item) <= INBOX_MESSAGE_FIELDS | {"content_truncated", "content_source_length"}
    assert _dict(item["content"])["text"] == body[:29] + "..."
    assert _dict(item["media"])["description"] == media[:29] + "..."
    assert item["content_source_length"] == 5000
    assert item["content_truncated"] is True
    assert body not in json.dumps(item, ensure_ascii=False)


def test_media_dedup_and_short_text_are_truthful() -> None:
    item = _structured_messages(
        [_message(text="photo", media_description="photo", media_kind="photo")],
        read_state=None,
        dialog_type=None,
        preview_chars=32,
    )[0]
    assert "content" not in item
    assert item["content_source_length"] == 5
    assert item["content_truncated"] is False
    assert _dict(item["media"])["description"] == "photo"


def test_bounded_identities_preserve_exact_selectors() -> None:
    item = _structured_messages(
        [
            _message(
                sender_id=55,
                sender_first_name="A" * 1000,
                sender_username="u" * 1000,
                forum_topic_id=77,
                topic_title="T" * 1000,
            )
        ],
        read_state=None,
        dialog_type=None,
    )[0]
    assert item["sender"] == {"display_name": "A" * 128, "telegram_id": 55}
    assert item["topic"] == {"topic_id": 77}
    pending = _project_read_position_pending_entities(
        [{"dialog_id": i, "display_name": "A" * 1000, "username": "u" * 1000} for i in range(40)]
    )
    assert len(pending) == 20
    assert all(_dict(entry["entity"])["telegram_id"] == i for i, entry in enumerate(pending))
    assert len(_text(_dict(pending[0]["entity"])["display_name"])) == 128


def test_pending_is_bounded_before_dedup() -> None:
    assert len(_project_read_position_pending_entities([{"dialog_id": 1}] * 20 + [{"dialog_id": 2}])) == 1


def test_lower_tier_previews_are_removed_before_human_messages() -> None:
    human = {"category": "user", "unread_mentions_count": 0, "messages": [1, 2, 3]}
    channel = {"category": "channel", "unread_mentions_count": 0, "messages": [1]}
    dialogs: list[object] = [human, channel]
    assert _drop_largest_inbox_preview(dialogs)
    assert channel["messages"] == []
    assert human["messages"] == [1, 2, 3]
    assert _drop_largest_inbox_preview(dialogs)
    assert human["messages"] == [2, 3]


def test_final_ascii_size_and_all_receipts_include_warnings_and_timezone() -> None:
    groups = [
        _group(i, unread_count=5, messages=[_message(dialog_id=i, message_id=j, text="Ж" * 1000) for j in range(1, 6)])
        for i in range(20)
    ]
    result = _project(_response(groups), response_chars=24000, timezone="Asia/Almaty")
    payload = _dict(result.structured_content)
    assert not result.is_error
    assert len(json.dumps(payload, ensure_ascii=True, separators=(",", ":"))) <= 24000
    dialogs = [_dict(value) for value in _list(payload["dialogs"])]
    budget = _dict(payload["budget"])
    warnings = [_dict(value) for value in _list(payload["warnings"])]
    shown = sum(len(_list(dialog["messages"])) for dialog in dialogs)
    assert result.result_count == shown == payload["shown_message_count"] == budget["result_message_count"]
    assert shown + cast(int, budget["hidden_count"]) == 100
    assert sum(cast(int, _dict(row)["hidden_count"]) for row in _list(budget["hidden_count_by_dialog"])) == 100 - shown
    assert payload["content_truncated_count"] == shown
    assert len(warnings) == 1
    assert "list_messages" in _text(warnings[0]["action"])
    assert "list_conversation_changes" in _text(warnings[0]["action"])
    for dialog in dialogs:
        for value in _list(dialog["messages"]):
            assert _text(_dict(value)["sent_at"]).endswith("+06:00")


def test_metadata_overflow_is_recoverable_without_false_receipt() -> None:
    groups = [_group(i, display_name="Ж" * 1000, messages=[]) for i in range(20)]
    result = _project(_response(groups), response_chars=4000)
    assert result.is_error
    first = result.content[0]
    assert isinstance(first, TextContent)
    assert "Action:" in first.text
    assert "dialogs_per_page" in first.text


def test_configurable_projection_and_bounded_pending_coverage() -> None:
    result = _project(
        _response(
            [_group(unread_count=3, messages=[_message(message_id=i, text="x" * 100) for i in range(3)])],
            read_position_pending_count=100,
            read_position_pending_entities=[{"dialog_id": i} for i in range(100)],
        ),
        messages_per_dialog=2,
        preview_chars=32,
        response_chars=64000,
    )
    payload = _dict(result.structured_content)
    messages = _list(_dict(_list(payload["dialogs"])[0])["messages"])
    coverage = _dict(payload["coverage"])
    assert len(messages) == 2
    assert len(_text(_dict(_dict(messages[0])["content"])["text"])) == 32
    assert coverage["read_position_pending_count"] == 100
    assert len(_list(coverage["read_position_pending_entities"])) == 20
    assert len(_list(payload["warnings"])) == 2
    assert "preview_chars=32" in _text(_dict(payload["budget"])["allocation_policy"])


@pytest.mark.parametrize(
    "values", [{"messages_per_dialog": 21}, {"dialogs_per_page": 21}, {"preview_chars": 31}, {"response_chars": 3999}]
)
def test_inbox_knob_limits(values: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        GetInbox.model_validate(values)


def test_inbox_metadata_fields_are_bounded_and_unknown_topic_is_omitted() -> None:
    read_state = {
        "inbox_cursor_state": "populated",
        "outbox_cursor_state": "populated",
        "inbox_unread_count": 0,
        "outbox_unread_count": 0,
        "unexpected": "X" * 10000,
    }
    result = _project(
        _response(
            [
                _group(
                    display_name="Ж" * 1000,
                    username="u" * 1000,
                    display_name_source="X" * 10000,
                    category="X" * 10000,
                    dialog_type="X" * 10000,
                    read_state=read_state,
                    messages=[_message(topic_title="T" * 1000)],
                )
            ]
        )
    )
    dialog = _dict(_list(_dict(result.structured_content)["dialogs"])[0])
    read_state_payload = _dict(dialog["read_state"])
    assert dialog["entity"] == {"display_name": "Ж" * 128, "telegram_id": 10}
    assert dialog["display_name_source"] == "numeric"
    assert dialog["category"] == dialog["dialog_type"] == "unknown"
    assert "unexpected" not in _dict(read_state_payload["state"])
    assert len(_dict(read_state_payload["state"])) <= 8
    assert len(_list(read_state_payload["header_lines"])) <= 2
    assert "topic" not in _dict(_list(dialog["messages"])[0])


@pytest.mark.parametrize("preview_chars", [400, 4000])
def test_response_budget_shortens_human_preview_before_discarding_it(preview_chars: int) -> None:
    result = _project(
        _response(
            [
                _group(10, messages=[_message(text="Ж" * 400)]),
                _group(11, category="bot", dialog_type="bot", messages=[_message(dialog_id=11, text="small")]),
            ]
        ),
        preview_chars=preview_chars,
        response_chars=4000,
    )
    payload = _dict(result.structured_content)
    assert not result.is_error
    assert len(json.dumps(payload, ensure_ascii=True, separators=(",", ":"))) <= 4000
    human = [_dict(value) for value in _list(_dict(_list(payload["dialogs"])[0])["messages"])]
    assert len(human) == 1
    assert _dict(human[0]["content"])["text"]
    assert human[0]["content_source_length"] == 400
    assert human[0]["content_truncated"] is True
