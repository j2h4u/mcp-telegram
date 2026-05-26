"""Tests for tools/reading.py helpers."""

from __future__ import annotations

from mcp_telegram.tools.reading import (
    ListMessages,
    SearchMessages,
    _format_search_results,
    _list_messages_structured_content,
    _list_messages_structured_messages,
    _search_structured_content,
)


def _row(
    sender_id,
    sender_first_name,
    *,
    message_id: int = 1,
    sent_at: int = 1_700_000_000,
    text: str = "hello world",
    dialog_name: str | None = None,
    is_service: int = 0,
    out: int = 0,
    dialog_id: int = 0,
    effective_sender_id=None,
) -> dict:
    r: dict = {
        "message_id": message_id,
        "sent_at": sent_at,
        "text": text,
        "sender_id": sender_id,
        "sender_first_name": sender_first_name,
        "is_service": is_service,
        "out": out,
        "dialog_id": dialog_id,
        "effective_sender_id": effective_sender_id,
    }
    if dialog_name is not None:
        r["dialog_name"] = dialog_name
    return r


def test_search_snippet_uses_sender_first_name_when_present():
    out = _format_search_results([_row(sender_id=42, sender_first_name="Alice")], "hello")
    assert " Alice (msg_id:1)" in out


def test_search_snippet_renders_unknown_user_with_id_when_name_missing():
    out = _format_search_results([_row(sender_id=42, sender_first_name=None)], "hello")
    assert " (unknown user 42) (msg_id:1)" in out


def test_search_snippet_renders_system_when_is_service():
    """Phase 39.1-02: 'System' requires is_service=1 (not just sender_id=None)."""
    out = _format_search_results([_row(sender_id=None, sender_first_name=None, is_service=1)], "hello")
    assert " System (msg_id:1)" in out


def test_search_snippet_dm_outgoing_renders_self_label():
    """DM outgoing (out=1, dialog_id>0, is_service=0) renders SELF_SENDER_LABEL."""
    from mcp_telegram.formatter import SELF_SENDER_LABEL

    out = _format_search_results(
        [
            _row(
                sender_id=None,
                sender_first_name=None,
                out=1,
                dialog_id=268071163,
                is_service=0,
                effective_sender_id=99999,
            )
        ],
        "hello",
    )
    assert f" {SELF_SENDER_LABEL} (msg_id:1)" in out


def test_search_snippet_dm_incoming_uses_first_name():
    out = _format_search_results(
        [
            _row(
                sender_id=None,
                sender_first_name="Alice",
                out=0,
                dialog_id=268071163,
                is_service=0,
                effective_sender_id=268071163,
            )
        ],
        "hello",
    )
    assert " Alice (msg_id:1)" in out


def test_search_snippet_dm_incoming_unknown_uses_effective_sender_id():
    out = _format_search_results(
        [
            _row(
                sender_id=None,
                sender_first_name=None,
                out=0,
                dialog_id=268071163,
                is_service=0,
                effective_sender_id=268071163,
            )
        ],
        "hello",
    )
    assert " (unknown user 268071163) (msg_id:1)" in out


def test_search_snippet_group_unknown_renders_unknown_user():
    """Group unknown sender (no id anywhere) → '(unknown user)' no trailing id."""
    out = _format_search_results(
        [
            _row(
                sender_id=None,
                sender_first_name=None,
                out=0,
                dialog_id=-100123,
                is_service=0,
                effective_sender_id=None,
            )
        ],
        "hello",
    )
    assert " (unknown user) (msg_id:1)" in out


def test_search_snippet_no_raw_question_mark_sender_fallback_in_source():
    import pathlib

    src = pathlib.Path("src/mcp_telegram/tools/reading.py").read_text()
    assert 'row.get("sender_first_name") or "?"' not in src


def test_search_messages_structured_payload_includes_dialog_anchor_read_state_warning_and_limits():
    payload = _search_structured_content(
        args=SearchMessages(
            dialog="123",
            query="needle",
            limit=5,
            navigation="search-token",
        ),
        data={
            "messages": [
                {
                    "dialog_id": 123,
                    "dialog_name": "Alice",
                    "message_id": 9,
                    "sent_at": 1_700_000_000,
                    "text": "needle in Telegram text",
                    "sender_first_name": "Alice",
                }
            ],
            "dialog_access": "archived",
            "last_synced_at": 1_699_990_000,
            "last_event_at": 1_699_999_000,
            "sync_coverage_pct": 80,
            "read_state_per_dialog": {
                123: {
                    "inbox_unread_count": 0,
                    "inbox_cursor_state": "populated",
                    "outbox_unread_count": 0,
                    "outbox_cursor_state": "populated",
                }
            },
        },
        rows=[
            {
                "dialog_id": 123,
                "dialog_name": "Alice",
                "message_id": 9,
                "sent_at": 1_700_000_000,
                "text": "needle in Telegram text",
                "sender_first_name": "Alice",
            }
        ],
        dialog_id=123,
        dialog_label="123",
        global_mode=False,
        offset=20,
        next_navigation="next-search-token",
    )

    assert payload["dialog_name"] == "Alice"
    assert payload["source"] == "sync_db"
    assert payload["coverage"]["kind"] == "archived"
    assert payload["warnings"][0]["kind"] == "archived_dialog"
    assert payload["read_state_per_dialog"]["123"]["header_lines"] == ["[read-state: all caught up]"]
    assert payload["navigation"] == {
        "next_navigation": "next-search-token",
        "has_more": True,
        "source_cursor": "search-token",
        "offset": 20,
    }
    assert payload["limits"] == {"requested_limit": 5, "applied_limit": 1, "offset": 20}
    assert payload["anchor_call"]["arguments_template"]["anchor_message_id"] == "<result.msg_id>"
    result = payload["results"][0]
    assert result["dialog_name"] == "Alice"
    assert result["content"] == {
        "text": "needle in Telegram text",
        "is_telegram_content": True,
        "content_kind": "snippet",
    }
    assert result["anchor_call"] == {
        "tool": "list_messages",
        "arguments": {"exact_dialog_id": 123, "anchor_message_id": 9},
    }
    assert payload["result_count_semantics"] == "count is the number of search hits returned in this response page"


def test_list_messages_structured_page_metadata_preserves_navigation_warning_coverage_and_limits():
    payload = _list_messages_structured_content(
        args=ListMessages(exact_dialog_id=123, limit=10, navigation="start", anchor_message_id=50),
        data={
            "messages": [],
            "source": "sync_db",
            "next_navigation": "history-token",
            "coverage": "fragment",
            "dialog_access": "archived",
            "last_synced_at": 1_699_990_000,
            "last_event_at": 1_699_999_000,
            "sync_coverage_pct": 80,
            "dialog_type": "User",
            "read_state": {
                "inbox_unread_count": 0,
                "inbox_cursor_state": "populated",
                "outbox_unread_count": 0,
                "outbox_cursor_state": "populated",
            },
        },
        rows=[],
        dialog_id=123,
        sender_id=None,
        sender_name=None,
        topic_id=None,
        direction="oldest",
        next_navigation="history-token",
    )

    assert payload["dialog_id"] == 123
    assert payload["coverage"]["kind"] == "fragment"
    assert payload["coverage"]["fragment_coverage"] is True
    assert payload["warnings"][0]["kind"] == "archived_dialog"
    assert "No current access" in payload["warnings"][0]["message"]
    assert payload["navigation"]["next_navigation"] == "history-token"
    assert payload["navigation"]["anchor_message_id"] == 50
    assert payload["navigation"]["direction"] == "around"
    assert payload["presentation"]["messages_order"] == "chronological"
    assert payload["presentation"]["is_chronological"] is True
    assert payload["limits"] == {
        "requested_limit": 10,
        "applied_limit": 0,
        "requested_context_size": 10,
        "applied_context_size": 10,
    }
    assert payload["count"] == 0
    assert payload["result_count_semantics"] == "count is the number of message rows returned in this response page"
    assert payload["read_state"]["header_lines"] == ["[read-state: all caught up]"]


def test_list_messages_always_presents_selected_page_chronologically_with_reply_refs():
    rows = [
        {
            "message_id": 3,
            "sent_at": 1_700_000_120,
            "dialog_id": 123,
            "text": "newest reply",
            "sender_first_name": "Bob",
            "sender_id": 22,
            "reply_to_msg_id": 2,
        },
        {
            "message_id": 2,
            "sent_at": 1_700_000_060,
            "dialog_id": 123,
            "text": "middle parent",
            "sender_first_name": "Alice",
            "sender_id": 11,
        },
        {
            "message_id": 1,
            "sent_at": 1_700_000_000,
            "dialog_id": 123,
            "text": "oldest in latest page",
            "sender_first_name": "Alice",
            "sender_id": 11,
        },
    ]

    payload = _list_messages_structured_content(
        args=ListMessages(exact_dialog_id=123, navigation="latest"),
        data={"messages": rows, "source": "sync_db", "next_navigation": "history-token"},
        rows=rows,
        dialog_id=123,
        sender_id=None,
        sender_name=None,
        topic_id=None,
        direction="newest",
        next_navigation="history-token",
    )

    assert [message["msg_id"] for message in payload["messages"]] == [1, 2, 3]
    assert payload["presentation"]["messages_order"] == "chronological"
    assert payload["presentation"]["is_chronological"] is True
    reply = payload["messages"][2]
    assert reply["reply_to_msg_id"] == 2
    assert reply["reply_context_ref"] == {"msg_id": 2, "in_page": True, "context_included": False}
    assert reply["reply_context"] is None


def test_list_messages_structured_messages_include_content_metadata_and_all_read_markers():
    rows = [
        {
            "message_id": 1,
            "sent_at": 1_700_000_000,
            "dialog_id": 123,
            "text": "incoming seen",
            "sender_first_name": "Alice",
            "sender_id": 11,
            "effective_sender_id": 11,
            "out": 0,
        },
        {
            "message_id": 2,
            "sent_at": 1_700_000_060,
            "dialog_id": 123,
            "text": "incoming unread",
            "sender_first_name": "Alice",
            "sender_id": 11,
            "effective_sender_id": 11,
            "out": 0,
        },
        {
            "message_id": 10,
            "sent_at": 1_700_000_120,
            "dialog_id": 123,
            "text": "outgoing seen",
            "sender_first_name": None,
            "sender_id": None,
            "effective_sender_id": 999,
            "out": 1,
        },
        {
            "message_id": 11,
            "sent_at": 1_700_000_180,
            "dialog_id": 123,
            "text": "outgoing unread",
            "sender_first_name": None,
            "sender_id": None,
            "effective_sender_id": 999,
            "out": 1,
        },
    ]
    read_state = {
        "inbox_unread_count": 1,
        "inbox_cursor_state": "populated",
        "inbox_max_id_anchor": 1,
        "outbox_unread_count": 1,
        "outbox_cursor_state": "populated",
        "outbox_max_id_anchor": 10,
    }

    messages = _list_messages_structured_messages(rows, read_state=read_state, dialog_type="User")

    marker_by_id = {message["msg_id"]: message["read_markers"][0] for message in messages}
    assert marker_by_id[1]["kind"] == "i_read_up_to_here"
    assert marker_by_id[2]["kind"] == "unread_by_me"
    assert marker_by_id[10]["kind"] == "peer_read_up_to_here"
    assert marker_by_id[11]["kind"] == "unread_by_peer"
    assert marker_by_id[11]["label"] == "[unread by peer]"
    assert messages[0]["content"] == {
        "text": "incoming seen",
        "is_telegram_content": True,
        "content_kind": "message_text",
    }
    assert messages[2]["sender"] == "[me]"
    assert messages[2]["out"] is True


def test_list_messages_structured_messages_cover_media_reply_forward_reaction_topic_and_edit_fields():
    rows = [
        {
            "message_id": 1,
            "sent_at": 1_700_000_000,
            "dialog_id": -100,
            "text": "original",
            "sender_first_name": "Alice",
            "sender_id": 11,
        },
        {
            "message_id": 2,
            "sent_at": 1_700_000_060,
            "dialog_id": -100,
            "text": "reply with all metadata",
            "sender_first_name": "Bob",
            "sender_id": 22,
            "media_description": "[фото]",
            "reply_to_msg_id": 1,
            "forum_topic_id": 7,
            "topic_title": "General",
            "fwd_from_name": "Forward Source",
            "post_author": "Channel Author",
            "edit_date": 1_700_000_120,
            "reactions_display": "[👍×2]",
        },
    ]

    messages = _list_messages_structured_messages(rows, dialog_type="Forum")
    second = messages[1]

    assert second["topic_id"] == 7
    assert second["topic_title"] == "General"
    assert second["media"] == {
        "description": "[фото]",
        "content": {"text": "[фото]", "is_telegram_content": True, "content_kind": "media_description"},
    }
    assert second["reply_context_ref"] == {"msg_id": 1, "in_page": True, "context_included": False}
    assert second["reply_context"] is None
    assert second["forward"]["from_name"] == "Forward Source"
    assert second["forward"]["content"]["content_kind"] == "forward_snippet"
    assert second["post_author"] == "Channel Author"
    assert second["edit_date"] == 1_700_000_120
    assert second["reactions"]["display"] == "[👍×2]"
    assert second["reactions"]["content"]["content_kind"] == "reaction"
    assert second["read_markers"] == []
