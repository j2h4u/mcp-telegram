from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.reading.scheduled_projection import (
    build_scheduled_list_query,
    build_scheduled_search_query,
    scheduled_row_to_wire,
)
from mcp_telegram.scheduled_contracts import scheduled_message_version


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
    since_utc: int | None = None
    until_utc: int | None = None


def test_scheduled_list_builder_has_explicit_clauses_and_bound_values() -> None:
    sql, params = build_scheduled_list_query(
        _ListRequest(sender_name="100% real_name\\here", topic_id=4),
        scheduled_now=1_700_000_000,
    )

    assert "WHERE sm.dialog_id = :dialog_id" in sql
    assert "sm.message_state IN ('scheduled', 'unknown_missing')" in sql
    assert "scheduled_now" not in params
    assert "sm.sender_first_name LIKE :sender_name_pattern" in sql
    assert "sm.forum_topic_id = :topic_id" in sql
    assert "100% real_name" not in sql
    assert params == {
        "dialog_id": 17,
        "limit": 10,
        "sender_name_pattern": "%100\\% real\\_name\\\\here%",
        "topic_id": 4,
    }


def test_scheduled_search_builder_uses_account_queue_without_own_only_scope() -> None:
    sql, params = build_scheduled_search_query(
        dialog_id=0,
        query="needle",
        limit=5,
        offset=10,
    )

    assert "scheduled_messages_fts MATCH :query" in sql
    assert "own_scope" not in sql
    assert "dialog_id" not in params


def test_scheduled_queries_use_half_open_utc_bounds() -> None:
    list_sql, list_params = build_scheduled_list_query(
        _ListRequest(since_utc=1_700_000_000, until_utc=1_700_001_000),
        scheduled_now=1_600_000_000,
    )
    search_sql, search_params = build_scheduled_search_query(
        dialog_id=17,
        query="needle",
        limit=5,
        offset=0,
        since_utc=1_700_000_000,
        until_utc=1_700_001_000,
    )

    for sql, params in ((list_sql, list_params), (search_sql, search_params)):
        assert "scheduled_at >= :since_utc" in sql
        assert "scheduled_at < :until_utc" in sql
        assert params["since_utc"] == 1_700_000_000
        assert params["until_utc"] == 1_700_001_000


def test_scheduled_row_mapper_is_the_lifecycle_wire_shape() -> None:
    row = {
        "message_id": 9,
        "sent_at": 1_700_000_100,
        "scheduled_at": 1_700_000_100,
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


def test_scheduled_version_accepts_negative_chat_ids_and_binds_account_queue_order() -> None:
    first = scheduled_message_version(7, -100123, 9, 12)
    assert first == scheduled_message_version(7, -100123, 9, 12)
    assert first != scheduled_message_version(8, -100123, 9, 12)
    assert first != scheduled_message_version(7, -100123, 10, 12)
    assert first != scheduled_message_version(7, -100123, 9, 13)


def test_scheduled_query_owner_has_no_fragile_sql_mutation() -> None:
    path = Path("src/mcp_telegram/reading/scheduled_projection.py")
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


def test_scheduled_version_preserves_existing_token_bytes() -> None:
    assert scheduled_message_version(7, -100123, 9, 12) == "685cdb150dd4a124236983c0"


def test_scheduled_operation_key_bound_is_shared_by_schema_input_and_fence() -> None:
    from mcp_telegram.scheduled_contracts import SCHEDULED_OPERATION_KEY_LIMIT
    from mcp_telegram.scheduled_receipts_schema import SCHEDULED_OPERATION_RECEIPT_DDL
    from mcp_telegram.scheduled_write_fence import ScheduledMutationIntent
    from mcp_telegram.tools.scheduled import ManageScheduledMessage

    assert SCHEDULED_OPERATION_KEY_LIMIT == 128
    properties = cast(dict[str, dict[str, object]], ManageScheduledMessage.model_json_schema()["properties"])
    assert properties["operation_key"]["maxLength"] == SCHEDULED_OPERATION_KEY_LIMIT
    assert f"length(operation_key) BETWEEN 1 AND {SCHEDULED_OPERATION_KEY_LIMIT}" in SCHEDULED_OPERATION_RECEIPT_DDL
    ScheduledMutationIntent(1, 2, "cancel", "x" * 128, "fingerprint", 1, 3, None, None)
    with pytest.raises(ValueError, match="operation_key"):
        ScheduledMutationIntent(1, 2, "cancel", "x" * 129, "fingerprint", 1, 3, None, None)
