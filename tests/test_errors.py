"""Tests for error text generation functions in errors.py."""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

import pytest
from telethon.errors import RPCError

from mcp_telegram.errors import (
    action_text,
    ambiguous_deleted_topic_text,
    ambiguous_dialog_text,
    ambiguous_entity_text,
    ambiguous_sender_text,
    ambiguous_topic_text,
    deleted_topic_text,
    dialog_not_found_text,
    dialog_topics_unavailable_text,
    entity_not_found_text,
    fetch_entity_info_error_text,
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
)


def _exc(message: str) -> MagicMock:
    exc = MagicMock()
    exc.message = message
    return exc


# Every error helper must surface its key context — the offending name/id, the
# retry tool, and the distinguishing keyword — in the rendered text. One row per
# helper: a dropped substring is a real UX regression (an error with no context).
# Matching is case-insensitive (names appear verbatim; keywords vary in case).
_ERROR_TEXT_CASES: list[tuple[str, str, list[str]]] = [
    ("action", action_text("Something failed.", "Fix it."), ["Something failed.", "Action: Fix it."]),
    (
        "dialog_not_found",
        dialog_not_found_text("MyChat", retry_tool="ListMessages"),
        ["MyChat", "ListMessages", "ListDialogs"],
    ),
    (
        "ambiguous_dialog",
        ambiguous_dialog_text("Test", ["id=1 name=A", "id=2 name=B"], retry_tool="ListMessages"),
        ["id=1 name=A", "id=2 name=B", "multiple"],
    ),
    ("deleted_topic", deleted_topic_text("OldTopic", retry_tool="ListMessages"), ["OldTopic", "deleted"]),
    ("topic_not_found", topic_not_found_text("Missing", retry_tool="ListMessages"), ["Missing", "not found"]),
    ("ambiguous_topic", ambiguous_topic_text("Dev", ["A", "B"], retry_tool="ListMessages"), ["multiple"]),
    (
        "ambiguous_deleted_topic",
        ambiguous_deleted_topic_text("Old", ["X", "Y"], retry_tool="ListMessages"),
        ["deleted"],
    ),
    (
        "dialog_topics_unavailable",
        dialog_topics_unavailable_text("Forum", _exc("CHANNEL_PRIVATE")),
        ["Forum", "CHANNEL_PRIVATE"],
    ),
    ("no_active_topics", no_active_topics_text("EmptyForum"), ["EmptyForum"]),
    (
        "invalid_navigation",
        invalid_navigation_text("bad token", retry_tool="SearchMessages"),
        ["bad token", "SearchMessages"],
    ),
    ("sender_not_found", sender_not_found_text("Ghost", retry_tool="ListMessages"), ["Ghost"]),
    (
        "ambiguous_sender",
        ambiguous_sender_text("Ivan", ["id=1 Ivan P", "id=2 Ivan S"], retry_tool="ListMessages"),
        ["Ivan", "id=1"],
    ),
    ("entity_not_found", entity_not_found_text("Nobody", retry_tool="GetEntityInfo"), ["Nobody"]),
    ("ambiguous_entity", ambiguous_entity_text("Ivan", ["match1", "match2"], retry_tool="GetEntityInfo"), ["multiple"]),
    ("fetch_entity_info_error", fetch_entity_info_error_text("Bob", "timeout"), ["Bob", "timeout"]),
    ("not_authenticated", not_authenticated_text("GetMyAccount"), ["GetMyAccount", "authenticated"]),
    ("no_usage_data", no_usage_data_text(), ["30 days"]),
    ("usage_stats_db_missing", usage_stats_db_missing_text(), ["database"]),
    ("usage_stats_query_error", usage_stats_query_error_text("OperationalError"), ["OperationalError"]),
    ("no_dialogs", no_dialogs_text(), ["no dialogs"]),
    ("no_unread_personal", no_unread_personal_text(), ["personal"]),
    ("no_unread_all", no_unread_all_text(), ["no unread"]),
    ("search_no_hits", search_no_hits_text("ChatName", "hello"), ["hello", "ChatName"]),
]


@pytest.mark.parametrize(
    "text, expected",
    [(case[1], case[2]) for case in _ERROR_TEXT_CASES],
    ids=[case[0] for case in _ERROR_TEXT_CASES],
)
def test_error_text_surfaces_key_context(text: str, expected: list[str]) -> None:
    lowered = text.lower()
    for substring in expected:
        assert substring.lower() in lowered, substring


# --- Behavioral branches (real logic, not pass-through) ---


def test_rpc_error_detail_uses_message_attr() -> None:
    assert rpc_error_detail(_exc("FLOOD_WAIT_42")) == "FLOOD_WAIT_42"


def test_rpc_error_detail_falls_back_to_str() -> None:
    class FakeExc:
        def __str__(self) -> str:
            return "some error"

    assert rpc_error_detail(cast(RPCError, FakeExc())) == "some error"


def test_inaccessible_topic_text_branches_on_resolved() -> None:
    exc = _exc("TOPIC_PRIVATE")
    resolved = inaccessible_topic_text("Secret", exc, resolved=True, retry_tool="ListMessages")
    not_resolved = inaccessible_topic_text("Secret", exc, resolved=False, retry_tool="ListMessages")
    assert "resolved" in resolved.lower()
    assert "TOPIC_PRIVATE" in resolved
    assert "could not be loaded" in not_resolved.lower()
