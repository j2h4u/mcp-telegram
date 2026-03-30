"""Tests for error text generation functions in errors.py."""
from __future__ import annotations

from unittest.mock import MagicMock

from mcp_telegram.errors import (
    action_text,
    ambiguous_deleted_topic_text,
    ambiguous_dialog_text,
    ambiguous_sender_text,
    ambiguous_topic_text,
    ambiguous_user_text,
    deleted_topic_text,
    dialog_not_found_text,
    dialog_topics_unavailable_text,
    fetch_user_info_error_text,
    inaccessible_topic_text,
    invalid_navigation_text,
    no_active_topics_text,
    no_dialogs_text,
    no_unread_all_text,
    no_unread_personal_text,
    no_usage_data_text,
    not_authenticated_text,
    rpc_error_detail,
    search_no_hits_text,
    sender_not_found_text,
    topic_not_found_text,
    usage_stats_db_missing_text,
    usage_stats_query_error_text,
    user_not_found_text,
)


def test_action_text_format():
    result = action_text("Something failed.", "Fix it.")
    assert result == "Something failed.\nAction: Fix it."


def test_dialog_not_found_includes_name_and_retry_tool():
    result = dialog_not_found_text("MyChat", retry_tool="ListMessages")
    assert "MyChat" in result
    assert "ListMessages" in result
    assert "ListDialogs" in result


def test_ambiguous_dialog_includes_matches():
    result = ambiguous_dialog_text("Test", ["id=1 name=A", "id=2 name=B"], retry_tool="ListMessages")
    assert "id=1 name=A" in result
    assert "id=2 name=B" in result
    assert "multiple" in result.lower()


def test_deleted_topic_text():
    result = deleted_topic_text("OldTopic", retry_tool="ListMessages")
    assert "OldTopic" in result
    assert "deleted" in result.lower()


def test_rpc_error_detail_uses_message_attr():
    exc = MagicMock()
    exc.message = "FLOOD_WAIT_42"
    assert rpc_error_detail(exc) == "FLOOD_WAIT_42"


def test_rpc_error_detail_falls_back_to_str():
    class FakeExc:
        def __str__(self):
            return "some error"
    exc = FakeExc()
    assert rpc_error_detail(exc) == "some error"


def test_inaccessible_topic_resolved_true():
    exc = MagicMock()
    exc.message = "TOPIC_PRIVATE"
    result = inaccessible_topic_text("Secret", exc, resolved=True, retry_tool="ListMessages")
    assert "resolved" in result.lower()
    assert "TOPIC_PRIVATE" in result


def test_inaccessible_topic_resolved_false():
    exc = MagicMock()
    exc.message = "TOPIC_PRIVATE"
    result = inaccessible_topic_text("Secret", exc, resolved=False, retry_tool="ListMessages")
    assert "could not be loaded" in result.lower()


def test_topic_not_found_text():
    result = topic_not_found_text("Missing", retry_tool="ListMessages")
    assert "Missing" in result
    assert "not found" in result.lower()


def test_ambiguous_topic_text():
    result = ambiguous_topic_text("Dev", ["A", "B"], retry_tool="ListMessages")
    assert "multiple" in result.lower()


def test_ambiguous_deleted_topic_text():
    result = ambiguous_deleted_topic_text("Old", ["X", "Y"], retry_tool="ListMessages")
    assert "deleted" in result.lower()


def test_dialog_topics_unavailable_text():
    exc = MagicMock()
    exc.message = "CHANNEL_PRIVATE"
    result = dialog_topics_unavailable_text("Forum", exc)
    assert "Forum" in result
    assert "CHANNEL_PRIVATE" in result


def test_no_active_topics_text():
    result = no_active_topics_text("EmptyForum")
    assert "EmptyForum" in result


def test_invalid_navigation_text():
    result = invalid_navigation_text("bad token", retry_tool="SearchMessages")
    assert "bad token" in result
    assert "SearchMessages" in result


def test_sender_not_found_text():
    result = sender_not_found_text("Ghost", retry_tool="ListMessages")
    assert "Ghost" in result


def test_ambiguous_sender_text():
    result = ambiguous_sender_text("Ivan", ["id=1 Ivan P", "id=2 Ivan S"], retry_tool="ListMessages")
    assert "Ivan" in result
    assert "id=1" in result


def test_user_not_found_text():
    result = user_not_found_text("Nobody", retry_tool="GetUserInfo")
    assert "Nobody" in result


def test_ambiguous_user_text():
    result = ambiguous_user_text("Ivan", ["match1", "match2"], retry_tool="GetUserInfo")
    assert "multiple" in result.lower()


def test_fetch_user_info_error_text():
    result = fetch_user_info_error_text("Bob", "timeout")
    assert "Bob" in result
    assert "timeout" in result


def test_not_authenticated_text():
    result = not_authenticated_text("GetMyAccount")
    assert "GetMyAccount" in result
    assert "authenticated" in result.lower()


def test_no_usage_data_text():
    result = no_usage_data_text()
    assert "30 days" in result


def test_usage_stats_db_missing_text():
    result = usage_stats_db_missing_text()
    assert "database" in result.lower()


def test_usage_stats_query_error_text():
    result = usage_stats_query_error_text("OperationalError")
    assert "OperationalError" in result


def test_no_dialogs_text():
    result = no_dialogs_text()
    assert "no dialogs" in result.lower()


def test_no_unread_personal_text():
    result = no_unread_personal_text()
    assert "personal" in result.lower()


def test_no_unread_all_text():
    result = no_unread_all_text()
    assert "no unread" in result.lower()


def test_search_no_hits_text():
    result = search_no_hits_text("ChatName", "hello")
    assert "hello" in result
    assert "ChatName" in result
