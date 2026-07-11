"""Direct tests for _build_list_messages_query — the dynamic SQL builder.

Covers: direction combinations, anchor/cursor pagination, filter stacking,
unread_after_id, and parameter ordering.

Phase 39.1-02: params are a dict with named keys (dialog_id, limit, self_id,
and optional filter keys). SQL uses :name placeholders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from mcp_telegram.daemon_api import _build_list_messages_query, _ListMessagesDbRequest


@dataclass(frozen=True, slots=True)
class _ListMessagesQueryReq:
    dialog_id: int
    limit: int
    self_id: int | None
    direction: str
    anchor_msg_id: int | None
    anchor_sent_at: int | None
    sender_id: int | None
    sender_name: str | None
    topic_id: int | None
    unread_after_id: int | None


def _build_list_messages_query_req(**overrides: object) -> _ListMessagesDbRequest:
    data: dict[str, object] = {
        "dialog_id": 100,
        "limit": 20,
        "self_id": None,
        "direction": "newest",
        "anchor_msg_id": None,
        "anchor_sent_at": None,
        "sender_id": None,
        "sender_name": None,
        "topic_id": None,
        "unread_after_id": None,
    }
    data.update(cast(dict[str, object], overrides))
    return cast(
        _ListMessagesDbRequest,
        _ListMessagesQueryReq(
            dialog_id=int(cast(int | str, data["dialog_id"])),
            limit=int(cast(int | str, data["limit"])),
            self_id=None if data["self_id"] is None else int(cast(int | str, data["self_id"])),
            direction=str(data["direction"]),
            anchor_msg_id=None if data["anchor_msg_id"] is None else int(cast(int | str, data["anchor_msg_id"])),
            anchor_sent_at=None if data["anchor_sent_at"] is None else int(cast(int | str, data["anchor_sent_at"])),
            sender_id=None if data["sender_id"] is None else int(cast(int | str, data["sender_id"])),
            sender_name=data["sender_name"] if data["sender_name"] is None else str(data["sender_name"]),
            topic_id=None if data["topic_id"] is None else int(cast(int | str, data["topic_id"])),
            unread_after_id=None if data["unread_after_id"] is None else int(cast(int | str, data["unread_after_id"])),
        ),
    )


# ---------------------------------------------------------------------------
# Baseline — no filters
# ---------------------------------------------------------------------------


def test_baseline_newest() -> None:
    """Default direction=newest produces DESC order and dialog_id + limit + self_id params."""
    sql, params = _build_list_messages_query(_build_list_messages_query_req())
    assert "ORDER BY m.message_id DESC" in sql
    assert "m.is_deleted = 0" in sql
    assert params["dialog_id"] == 100
    assert params["limit"] == 20
    assert "self_id" in params


def test_baseline_oldest() -> None:
    """direction=oldest produces ASC order."""
    sql, params = _build_list_messages_query(_build_list_messages_query_req(direction="oldest"))
    assert "ORDER BY m.message_id ASC" in sql
    assert params["dialog_id"] == 100
    assert params["limit"] == 20


# ---------------------------------------------------------------------------
# Anchor (cursor pagination)
# ---------------------------------------------------------------------------


def test_anchor_newest_uses_less_than() -> None:
    """With direction=newest, anchor filters message_id < :anchor_msg_id."""
    sql, params = _build_list_messages_query(_build_list_messages_query_req(anchor_msg_id=500))
    assert "m.message_id < :anchor_msg_id" in sql
    assert params["anchor_msg_id"] == 500


def test_anchor_oldest_uses_greater_than() -> None:
    """With direction=oldest, anchor filters message_id > :anchor_msg_id."""
    sql, params = _build_list_messages_query(_build_list_messages_query_req(direction="oldest", anchor_msg_id=500))
    assert "m.message_id > :anchor_msg_id" in sql
    assert params["anchor_msg_id"] == 500


def test_timestamp_anchor_oldest_breaks_message_id_ties() -> None:
    """Chronological cursors compare sent_at first, then message_id."""
    sql, params = _build_list_messages_query(
        _build_list_messages_query_req(direction="oldest", anchor_msg_id=500, anchor_sent_at=1_700_000_500)
    )
    assert "m.sent_at > :anchor_sent_at" in sql
    assert "m.message_id > :anchor_msg_id" in sql
    assert params["anchor_sent_at"] == 1_700_000_500


def test_timestamp_anchor_newest_breaks_message_id_ties() -> None:
    """Newest chronological cursors use the inverse tie-breaker."""
    sql, params = _build_list_messages_query(
        _build_list_messages_query_req(anchor_msg_id=500, anchor_sent_at=1_700_000_500)
    )
    assert "m.sent_at < :anchor_sent_at" in sql
    assert "m.message_id < :anchor_msg_id" in sql
    assert params["anchor_sent_at"] == 1_700_000_500


# ---------------------------------------------------------------------------
# Sender filters
# ---------------------------------------------------------------------------


def test_sender_id_filter() -> None:
    """sender_id adds an exact-match condition."""
    sql, params = _build_list_messages_query(_build_list_messages_query_req(sender_id=42))
    assert "m.sender_id = :filter_sender_id" in sql
    assert params["filter_sender_id"] == 42


def test_sender_name_filter() -> None:
    """sender_name adds a LIKE condition with wildcards."""
    sql, params = _build_list_messages_query(_build_list_messages_query_req(sender_name="Alice"))
    assert "m.sender_first_name LIKE :sender_name_pattern ESCAPE" in sql
    assert params["sender_name_pattern"] == "%Alice%"


def test_sender_name_like_escapes_special_chars() -> None:
    """%, _, and \\ in sender_name are escaped so they match literally."""
    sql, params = _build_list_messages_query(_build_list_messages_query_req(sender_name="100% real_name\\here"))
    assert "ESCAPE '\\'" in sql
    assert params["sender_name_pattern"] == "%100\\% real\\_name\\\\here%"


def test_sender_id_takes_precedence_over_name() -> None:
    """When both sender_id and sender_name are provided, sender_id wins."""
    sql, params = _build_list_messages_query(_build_list_messages_query_req(sender_id=42, sender_name="Alice"))
    assert "m.sender_id = :filter_sender_id" in sql
    assert "LIKE" not in sql and "sender_name_pattern" not in params
    assert params["filter_sender_id"] == 42


# ---------------------------------------------------------------------------
# Topic filter
# ---------------------------------------------------------------------------


def test_topic_id_filter() -> None:
    """topic_id adds a forum_topic_id condition."""
    sql, params = _build_list_messages_query(_build_list_messages_query_req(topic_id=7))
    assert "m.forum_topic_id = :topic_id" in sql
    assert params["topic_id"] == 7


# ---------------------------------------------------------------------------
# Unread filter
# ---------------------------------------------------------------------------


def test_unread_after_id_filter() -> None:
    """unread_after_id adds a message_id > condition."""
    sql, params = _build_list_messages_query(_build_list_messages_query_req(unread_after_id=300))
    assert "m.message_id > :unread_after_id" in sql
    assert params["unread_after_id"] == 300


# ---------------------------------------------------------------------------
# Combined filters — parameter ordering
# ---------------------------------------------------------------------------


def test_all_filters_combined() -> None:
    """All filters together produce the full params dict."""
    sql, params = _build_list_messages_query(
        _build_list_messages_query_req(
            self_id=99999,
            direction="oldest",
            anchor_msg_id=500,
            sender_id=42,
            topic_id=7,
            unread_after_id=300,
        )
    )
    assert params["dialog_id"] == 100
    assert params["limit"] == 20
    assert params["self_id"] == 99999
    assert params["filter_sender_id"] == 42
    assert params["topic_id"] == 7
    assert params["unread_after_id"] == 300
    assert params["anchor_msg_id"] == 500
    assert "ORDER BY m.message_id ASC" in sql


def test_topic_and_sender_name_combined() -> None:
    """topic_id + sender_name produce correct stacking."""
    sql, params = _build_list_messages_query(_build_list_messages_query_req(limit=10, sender_name="Bob", topic_id=3))
    assert "LIKE" in sql and "ESCAPE" in sql
    assert "forum_topic_id = :topic_id" in sql
    assert params["sender_name_pattern"] == "%Bob%"
    assert params["topic_id"] == 3


# ---------------------------------------------------------------------------
# Column structure
# ---------------------------------------------------------------------------


def test_select_has_expected_columns() -> None:
    """SELECT includes all expected columns, including Phase 39.1-02 additions."""
    sql, _ = _build_list_messages_query(_build_list_messages_query_req(dialog_id=1, limit=1))
    for col in (
        "m.message_id",
        "m.sent_at",
        "m.text",
        "m.sender_id",
        "m.media_description",
        "m.reply_to_msg_id",
        "m.forum_topic_id",
        "m.is_deleted",
        "m.deleted_at",
        "edit_date",
        "topic_title",
        # Phase 39.1-02
        "effective_sender_id",
        "m.is_service",
        "m.out",
        "m.dialog_id",
    ):
        assert col in sql, f"Missing column: {col}"


def test_left_join_topic_metadata() -> None:
    """Query includes LEFT JOIN on topic_metadata."""
    sql, _ = _build_list_messages_query(_build_list_messages_query_req(dialog_id=1, limit=1))
    assert "LEFT JOIN topic_metadata tm" in sql


def test_effective_sender_id_case_expression_present() -> None:
    """Phase 39.1-02: SQL contains the CASE expression for DM direction collapse."""
    sql, params = _build_list_messages_query(_build_list_messages_query_req(dialog_id=1, limit=1))
    assert ":self_id" in sql
    assert "WHEN m.dialog_id > 0 AND m.out = 1 THEN :self_id" in sql


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_limit_is_always_last_clause() -> None:
    """LIMIT :limit is always the last clause regardless of filters."""
    for kwargs in [
        {"sender_id": 1},
        {"topic_id": 2, "anchor_msg_id": 3},
        {"unread_after_id": 4, "sender_name": "X"},
        {},
    ]:
        sql, params = _build_list_messages_query(_build_list_messages_query_req(limit=50, **kwargs))
        assert sql.rstrip().endswith("LIMIT :limit")
        assert params["limit"] == 50


def test_unread_and_anchor_both_add_gt_conditions() -> None:
    """unread_after_id and anchor with oldest both produce > conditions with distinct names."""
    sql, params = _build_list_messages_query(
        _build_list_messages_query_req(
            direction="oldest",
            unread_after_id=200,
            anchor_msg_id=300,
        )
    )
    assert "m.message_id > :unread_after_id" in sql
    assert "m.message_id > :anchor_msg_id" in sql
    assert params["unread_after_id"] == 200
    assert params["anchor_msg_id"] == 300
