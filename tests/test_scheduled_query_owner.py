from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from mcp_telegram.daemon_scheduled_queries import (
    build_scheduled_list_query,
    build_scheduled_search_query,
    scheduled_row_to_wire,
)


@dataclass(frozen=True)
class _ListRequest:
    dialog_id: int = 17
    limit: int = 10
    direction: str = "newest"
    anchor_msg_id: int | None = None
    anchor_sent_at: int | None = None
    sender_id: int | None = None
    sender_name: str | None = None
    topic_id: int | None = None


def test_scheduled_list_builder_has_explicit_clauses_and_bound_values() -> None:
    sql, params = build_scheduled_list_query(
        _ListRequest(sender_name="100% real_name\\here", topic_id=4),
        scheduled_now=1_700_000_000,
    )

    assert "WHERE sm.dialog_id = :dialog_id" in sql
    assert "sm.message_state = 'scheduled'" in sql
    assert "sm.scheduled_at > :scheduled_now" in sql
    assert "sm.sender_first_name LIKE :sender_name_pattern" in sql
    assert "sm.forum_topic_id = :topic_id" in sql
    assert "100% real_name" not in sql
    assert params == {
        "dialog_id": 17,
        "scheduled_now": 1_700_000_000,
        "limit": 10,
        "sender_name_pattern": "%100\\% real\\_name\\\\here%",
        "topic_id": 4,
    }


def test_scheduled_search_builder_uses_conditional_scope_clause() -> None:
    sql, params = build_scheduled_search_query(
        dialog_id=0,
        own_dialog_ids=[17, 23],
        query="needle",
        limit=5,
        offset=10,
        scheduled_now=1_700_000_000,
    )

    assert "scheduled_messages_fts MATCH :query" in sql
    assert "sm.dialog_id IN (:own_scope_0, :own_scope_1)" in sql
    assert "17" not in sql and "23" not in sql
    assert params["own_scope_0"] == 17
    assert params["own_scope_1"] == 23


def test_scheduled_row_mapper_is_the_lifecycle_wire_shape() -> None:
    row = {
        "message_id": 9,
        "sent_at": 1_700_000_100,
        "dialog_id": 17,
        "text": "needle",
        "sender_id": 42,
        "sender_first_name": "Me",
        "is_deleted": 0,
        "is_service": 0,
        "out": 1,
        "published_at": None,
    }

    mapped = scheduled_row_to_wire(row, inclusion_basis=("direct_message",))

    assert mapped["message_state"] == "scheduled"
    assert mapped["scheduled_at"] == mapped["sent_at"] == 1_700_000_100
    assert mapped["published_at"] is None
    assert mapped["unpublished"] is True
    assert mapped["unseen"] is True
    assert mapped["inclusion_basis"] == ["direct_message"]


def test_scheduled_query_owner_has_no_fragile_sql_mutation() -> None:
    path = Path("src/mcp_telegram/daemon_scheduled_queries.py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    fragile_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"replace", "format"}
    ]

    assert not fragile_calls
    assert not any(isinstance(node, ast.JoinedStr) for node in ast.walk(tree))
