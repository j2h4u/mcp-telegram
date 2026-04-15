"""Direct tests for _build_list_messages_query — the dynamic SQL builder.

Covers: direction combinations, anchor/cursor pagination, filter stacking,
unread_after_id, and parameter ordering.
"""
from __future__ import annotations

import pytest

from mcp_telegram.daemon_api import _build_list_messages_query


# ---------------------------------------------------------------------------
# Baseline — no filters
# ---------------------------------------------------------------------------


def test_baseline_newest() -> None:
    """Default direction=newest produces DESC order and dialog_id + limit params."""
    sql, params = _build_list_messages_query(dialog_id=100, limit=20)
    assert "ORDER BY m.message_id DESC" in sql
    assert "m.is_deleted = 0" in sql
    assert params == [100, 20]


def test_baseline_oldest() -> None:
    """direction=oldest produces ASC order."""
    sql, params = _build_list_messages_query(dialog_id=100, limit=20, direction="oldest")
    assert "ORDER BY m.message_id ASC" in sql
    assert params == [100, 20]


# ---------------------------------------------------------------------------
# Anchor (cursor pagination)
# ---------------------------------------------------------------------------


def test_anchor_newest_uses_less_than() -> None:
    """With direction=newest, anchor filters message_id < anchor."""
    sql, params = _build_list_messages_query(
        dialog_id=100, limit=20, direction="newest", anchor_msg_id=500
    )
    assert "m.message_id < ?" in sql
    assert params == [100, 500, 20]


def test_anchor_oldest_uses_greater_than() -> None:
    """With direction=oldest, anchor filters message_id > anchor."""
    sql, params = _build_list_messages_query(
        dialog_id=100, limit=20, direction="oldest", anchor_msg_id=500
    )
    assert "m.message_id > ?" in sql
    assert params == [100, 500, 20]


# ---------------------------------------------------------------------------
# Sender filters
# ---------------------------------------------------------------------------


def test_sender_id_filter() -> None:
    """sender_id adds an exact-match condition."""
    sql, params = _build_list_messages_query(
        dialog_id=100, limit=20, sender_id=42
    )
    assert "m.sender_id = ?" in sql
    assert params == [100, 42, 20]


def test_sender_name_filter() -> None:
    """sender_name adds a LIKE condition with wildcards."""
    sql, params = _build_list_messages_query(
        dialog_id=100, limit=20, sender_name="Alice"
    )
    assert "m.sender_first_name LIKE ? ESCAPE" in sql
    assert params == [100, "%Alice%", 20]


def test_sender_name_like_escapes_special_chars() -> None:
    """%, _, and \\ in sender_name are escaped so they match literally."""
    sql, params = _build_list_messages_query(
        dialog_id=100, limit=20, sender_name="100% real_name\\here"
    )
    # SQL literal uses single backslash as the ESCAPE char
    assert "ESCAPE '\\'" in sql
    # input: "100% real_name\here"  →  \→\\, %→\%, _→\_  →  wrapped in %
    assert params == [100, "%100\\% real\\_name\\\\here%", 20]


def test_sender_id_takes_precedence_over_name() -> None:
    """When both sender_id and sender_name are provided, sender_id wins."""
    sql, params = _build_list_messages_query(
        dialog_id=100, limit=20, sender_id=42, sender_name="Alice"
    )
    assert "m.sender_id = ?" in sql
    assert "LIKE" not in sql and "ESCAPE" not in sql
    assert params == [100, 42, 20]


# ---------------------------------------------------------------------------
# Topic filter
# ---------------------------------------------------------------------------


def test_topic_id_filter() -> None:
    """topic_id adds a forum_topic_id condition."""
    sql, params = _build_list_messages_query(
        dialog_id=100, limit=20, topic_id=7
    )
    assert "m.forum_topic_id = ?" in sql
    assert params == [100, 7, 20]


# ---------------------------------------------------------------------------
# Unread filter
# ---------------------------------------------------------------------------


def test_unread_after_id_filter() -> None:
    """unread_after_id adds a message_id > condition."""
    sql, params = _build_list_messages_query(
        dialog_id=100, limit=20, unread_after_id=300
    )
    assert "m.message_id > ?" in sql
    assert params == [100, 300, 20]


# ---------------------------------------------------------------------------
# Combined filters — parameter ordering
# ---------------------------------------------------------------------------


def test_all_filters_combined() -> None:
    """All filters together produce correct parameter order."""
    sql, params = _build_list_messages_query(
        dialog_id=100,
        limit=20,
        direction="oldest",
        anchor_msg_id=500,
        sender_id=42,
        topic_id=7,
        unread_after_id=300,
    )
    # Parameter order: dialog_id, sender_id, topic_id, unread_after_id, anchor, limit
    assert params == [100, 42, 7, 300, 500, 20]
    assert "ORDER BY m.message_id ASC" in sql


def test_topic_and_sender_name_combined() -> None:
    """topic_id + sender_name produce correct stacking."""
    sql, params = _build_list_messages_query(
        dialog_id=100, limit=10, sender_name="Bob", topic_id=3
    )
    assert "LIKE" in sql and "ESCAPE" in sql
    assert "forum_topic_id = ?" in sql
    assert params == [100, "%Bob%", 3, 10]


# ---------------------------------------------------------------------------
# Column structure
# ---------------------------------------------------------------------------


def test_select_has_expected_columns() -> None:
    """SELECT includes all 12 expected columns (0-11)."""
    sql, _ = _build_list_messages_query(dialog_id=1, limit=1)
    for col in (
        "m.message_id", "m.sent_at", "m.text", "m.sender_id",
        "m.sender_first_name", "m.media_description", "m.reply_to_msg_id",
        "m.forum_topic_id", "m.is_deleted", "m.deleted_at",
        "edit_date", "topic_title",
    ):
        assert col in sql, f"Missing column: {col}"


def test_left_join_topic_metadata() -> None:
    """Query includes LEFT JOIN on topic_metadata."""
    sql, _ = _build_list_messages_query(dialog_id=1, limit=1)
    assert "LEFT JOIN topic_metadata tm" in sql


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_limit_is_always_last_param() -> None:
    """LIMIT ? is always the last clause and last param regardless of filters."""
    for kwargs in [
        {"sender_id": 1},
        {"topic_id": 2, "anchor_msg_id": 3},
        {"unread_after_id": 4, "sender_name": "X"},
        {},
    ]:
        sql, params = _build_list_messages_query(dialog_id=100, limit=50, **kwargs)
        assert sql.rstrip().endswith("LIMIT ?")
        assert params[-1] == 50


def test_unread_and_anchor_both_add_gt_conditions() -> None:
    """unread_after_id and anchor with oldest both produce > conditions on different semantics."""
    sql, params = _build_list_messages_query(
        dialog_id=100, limit=20, direction="oldest",
        unread_after_id=200, anchor_msg_id=300,
    )
    # Both should produce "m.message_id > ?" but for different purposes
    assert sql.count("m.message_id > ?") == 2
    assert params == [100, 200, 300, 20]
