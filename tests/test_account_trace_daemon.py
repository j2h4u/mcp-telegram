from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from account_trace_fixtures import (
    make_channel_signature_evidence,
    open_trace_db,
    seed_channel_signature_message,
    seed_dialog,
    seed_entity,
    seed_message,
    seed_synced_dialog,
    seed_topic,
    seed_trace_fragment,
)
from mcp_telegram.daemon_api import (
    DaemonAPIServer,
    _build_trace_account_messages_query,
)
from mcp_telegram.models import (
    TraceCoverageGap,
    TraceCoverageSummary,
    TraceEvidenceItem,
    TraceResolvedAccount,
)
from mcp_telegram.pagination import (
    AccountTraceNavigationToken,
    decode_account_trace_navigation,
    encode_account_trace_navigation,
)


@pytest.fixture()
def trace_server(tmp_path):
    conn = open_trace_db(tmp_path)
    client = AsyncMock()
    server = DaemonAPIServer(conn, client, asyncio.Event())
    server.self_id = 101
    try:
        yield server, conn, client
    finally:
        conn.close()


def test_trace_typed_dict_contracts_are_importable() -> None:
    resolved: TraceResolvedAccount = {
        "confidence": "resolved",
        "account_id": 101,
        "display_name": "Alice Example",
        "username": "alice",
        "candidate_ids": [],
        "display_aliases": ["Alice Example", "alice"],
        "resolution_source": "entities",
    }
    evidence: TraceEvidenceItem = make_channel_signature_evidence()  # type: ignore[assignment]
    coverage: TraceCoverageSummary = {
        "state": "partial",
        "observed_message_count": 1,
        "dialogs_considered": 1,
        "dialogs_considered_basis": "evidence_related",
        "dialogs_with_hits": 1,
        "dialogs_with_gaps": 0,
        "as_of": 1_700_000_000,
    }
    gap: TraceCoverageGap = {
        "kind": "observed_zero",
        "severity": "info",
        "detail": "No authored-message evidence in considered coverage.",
    }

    assert resolved["confidence"] == "resolved"
    assert evidence["evidence_kind"] == "authored_message"
    assert evidence["authorship_basis"] == "post_author_signature"
    assert evidence["author_signature"] == "Alice Example"
    assert coverage["state"] == "partial"
    assert gap["severity"] == "info"


def test_account_trace_navigation_roundtrip() -> None:
    token = encode_account_trace_navigation(
        target_user_id=101,
        sent_at=1_700_000_001,
        dialog_id=-100123,
        message_id=55,
        group_by="timeline",
        exact_dialog_id=-100123,
        exact_topic_id=7,
        sent_after="2024-01-01T00:00:00Z",
        sent_before="2024-02-01T00:00:00Z",
    )

    decoded = decode_account_trace_navigation(
        token,
        expected_target_user_id=101,
        expected_group_by="timeline",
        expected_exact_dialog_id=-100123,
        expected_exact_topic_id=7,
        expected_sent_after="2024-01-01T00:00:00Z",
        expected_sent_before="2024-02-01T00:00:00Z",
    )

    assert decoded == AccountTraceNavigationToken(
        target_user_id=101,
        sent_at=1_700_000_001,
        dialog_id=-100123,
        message_id=55,
        group_by="timeline",
        exact_dialog_id=-100123,
        exact_topic_id=7,
        sent_after="2024-01-01T00:00:00Z",
        sent_before="2024-02-01T00:00:00Z",
    )


def test_account_trace_navigation_rejects_target_mismatch() -> None:
    token = encode_account_trace_navigation(
        target_user_id=101,
        sent_at=1,
        dialog_id=2,
        message_id=3,
        group_by="dialog",
    )

    with pytest.raises(ValueError, match="account 101, not 202"):
        decode_account_trace_navigation(
            token,
            expected_target_user_id=202,
            expected_group_by="dialog",
        )


def test_account_trace_navigation_rejects_topic_scope_mismatch() -> None:
    token = encode_account_trace_navigation(
        target_user_id=101,
        sent_at=1,
        dialog_id=2,
        message_id=3,
        group_by="timeline",
        exact_topic_id=8,
    )

    with pytest.raises(ValueError, match="topic scope 8, not 9"):
        decode_account_trace_navigation(
            token,
            expected_target_user_id=101,
            expected_group_by="timeline",
            expected_exact_topic_id=9,
        )


def test_account_trace_navigation_rejects_time_bound_mismatch() -> None:
    token = encode_account_trace_navigation(
        target_user_id=101,
        sent_at=1,
        dialog_id=2,
        message_id=3,
        group_by="timeline",
        sent_after="2024-01-01T00:00:00Z",
    )

    with pytest.raises(ValueError, match="sent_after"):
        decode_account_trace_navigation(
            token,
            expected_target_user_id=101,
            expected_group_by="timeline",
            expected_sent_after="2024-01-02T00:00:00Z",
        )


def test_trace_fragment_fixture_uses_dialog_level_topic_sentinel(tmp_path) -> None:
    conn = open_trace_db(tmp_path)
    try:
        seed_topic(conn, dialog_id=-100123, topic_id=1, title="General")
        seed_trace_fragment(
            conn,
            target_user_id=101,
            dialog_id=-100123,
            topic_id=0,
            status="pending",
        )
        conn.commit()

        real_topic_ids = [
            row[0]
            for row in conn.execute("SELECT topic_id FROM topic_metadata WHERE dialog_id = -100123")
        ]
        fragment = conn.execute(
            """
            SELECT topic_id, status, created_at, updated_at
            FROM trace_coverage_fragments
            WHERE target_user_id = 101 AND dialog_id = -100123
            """
        ).fetchone()

        assert real_topic_ids == [1]
        assert 0 not in real_topic_ids
        assert fragment == (0, "pending", 1_700_000_000, 1_700_000_000)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_resolve_trace_exact_account_id_from_entities(trace_server) -> None:
    server, conn, _client = trace_server
    seed_entity(conn, entity_id=123, name="Alice Example", username="alice")
    conn.commit()

    result = await server._resolve_trace_account({"exact_account_id": 123})

    assert result["confidence"] == "resolved"
    assert result["account_id"] == 123
    assert result["username"] == "alice"
    assert result["resolution_source"] == "entities_exact_id"


@pytest.mark.asyncio
async def test_resolve_trace_unknown_numeric_does_not_call_client(trace_server) -> None:
    server, _conn, client = trace_server

    result = await server._resolve_trace_account({"account": "123"})

    assert result["confidence"] == "unresolved"
    assert result["resolution_source"] == "unresolved_numeric_id"
    client.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_trace_username_local_entity_path(trace_server) -> None:
    server, conn, client = trace_server
    seed_entity(conn, entity_id=123, name="Alice Example", username="alice")
    conn.commit()

    result = await server._resolve_trace_account({"account": "@alice"})

    assert result["confidence"] == "resolved"
    assert result["account_id"] == 123
    assert result["resolution_source"] == "entities_username"
    client.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_trace_username_lookup_upserts_user(trace_server) -> None:
    server, conn, client = trace_server
    client.return_value = SimpleNamespace(
        users=[
            SimpleNamespace(
                id=123,
                first_name="Alice",
                last_name="Example",
                username="alice",
                bot=False,
            )
        ]
    )

    result = await server._resolve_trace_account({"account": "@alice"})

    assert result["confidence"] == "resolved"
    assert result["account_id"] == 123
    assert result["resolution_source"] == "telegram_username_lookup"
    assert client.await_count == 1
    row = conn.execute("SELECT id, name, username FROM entities WHERE id = 123").fetchone()
    assert tuple(row) == (123, "Alice Example", "alice")


@pytest.mark.asyncio
async def test_resolve_trace_username_lookup_failure_is_structured(trace_server) -> None:
    server, _conn, client = trace_server
    client.side_effect = RuntimeError("not visible")

    result = await server._resolve_trace_account({"account": "https://t.me/alice"})

    assert result["confidence"] == "unresolved"
    assert result["resolution_source"] == "telegram_username_lookup_failed"
    assert client.await_count == 1


@pytest.mark.asyncio
async def test_resolve_trace_fuzzy_ambiguous_returns_candidate_ids(trace_server) -> None:
    server, conn, _client = trace_server
    now = int(time.time())
    seed_entity(conn, entity_id=123, name="Alex One", username="alex1", updated_at=now)
    seed_entity(conn, entity_id=124, name="Alex Two", username="alex2", updated_at=now)
    conn.commit()

    result = await server._resolve_trace_account({"account": "Alex"})

    assert result["confidence"] == "ambiguous"
    assert result["resolution_source"] == "entities_fuzzy_candidates"
    assert set(result["candidate_ids"]) == {123, 124}


def test_trace_query_uses_effective_sender_topic_and_signature_params() -> None:
    sql, params = _build_trace_account_messages_query(
        target_user_id=101,
        self_id=101,
        limit=51,
        post_author_aliases=["Alice Example", "alice"],
        exact_dialog_id=-100123,
        exact_topic_id=5,
        sent_after_ts=1_700_000_000,
        sent_before_ts=1_700_100_000,
    )

    assert "CASE WHEN m.is_service = 1 THEN NULL" in sql
    assert "m.sender_id = :target_user_id" not in sql
    assert "m.forum_topic_id = :exact_topic_id" in sql
    assert "m.post_author AS author_signature" in sql
    assert "m.post_author IN (:post_author_alias_0, :post_author_alias_1)" in sql
    assert "ORDER BY m.sent_at DESC, m.dialog_id DESC, m.message_id DESC" in sql
    assert params["self_id"] == 101
    assert params["target_user_id"] == 101
    assert params["limit"] == 51
    assert params["exact_dialog_id"] == -100123
    assert params["exact_topic_id"] == 5
    assert params["sent_after"] == 1_700_000_000
    assert params["sent_before"] == 1_700_100_000


@pytest.mark.asyncio
async def test_dispatch_routes_trace_account_messages(trace_server) -> None:
    server, conn, _client = trace_server
    seed_entity(conn, entity_id=101, name="Me", username="me")
    conn.commit()

    result = await server._dispatch({"method": "trace_account_messages", "exact_account_id": 101})

    assert result["ok"] is True
    assert result["data"]["resolved_account"]["account_id"] == 101


@pytest.mark.asyncio
async def test_trace_includes_outgoing_dm_effective_sender(trace_server) -> None:
    server, conn, _client = trace_server
    seed_entity(conn, entity_id=101, name="Me", username="me")
    seed_dialog(conn, dialog_id=222, name="Alice", dialog_type="User")
    seed_synced_dialog(conn, dialog_id=222)
    seed_message(
        conn,
        dialog_id=222,
        message_id=10,
        sent_at=1_700_000_010,
        text="outgoing dm",
        sender_id=None,
        out=1,
    )
    conn.commit()

    result = await server._trace_account_messages({"exact_account_id": 101, "limit": 10})

    evidence = result["data"]["groups"][0]["evidence"]
    assert evidence[0]["message_id"] == 10
    assert evidence[0]["effective_sender_id"] == 101
    assert evidence[0]["authorship_basis"] == "effective_sender_id"


@pytest.mark.asyncio
async def test_trace_includes_channel_signature_without_numeric_identity_claim(trace_server) -> None:
    server, conn, _client = trace_server
    seed_entity(conn, entity_id=101, name="Alice Example", username="alice")
    seed_dialog(conn, dialog_id=-100123, name="Channel", dialog_type="Channel")
    seed_synced_dialog(conn, dialog_id=-100123)
    seed_channel_signature_message(
        conn,
        dialog_id=-100123,
        message_id=42,
        sent_at=1_700_000_020,
        signature="Alice Example",
    )
    conn.commit()

    result = await server._trace_account_messages({"exact_account_id": 101, "limit": 10})

    evidence = result["data"]["groups"][0]["evidence"]
    assert evidence[0]["message_id"] == 42
    assert evidence[0]["authorship_basis"] == "post_author_signature"
    assert evidence[0]["effective_sender_id"] == -100123
    assert evidence[0]["author_signature"] == "Alice Example"


@pytest.mark.asyncio
async def test_trace_exact_topic_scope_filters_rows(trace_server) -> None:
    server, conn, _client = trace_server
    seed_entity(conn, entity_id=101, name="Me", username="me")
    seed_dialog(conn, dialog_id=-100222, name="Forum", dialog_type="Forum")
    seed_synced_dialog(conn, dialog_id=-100222)
    seed_topic(conn, dialog_id=-100222, topic_id=5, title="Five")
    seed_topic(conn, dialog_id=-100222, topic_id=7, title="Seven")
    seed_message(
        conn,
        dialog_id=-100222,
        message_id=50,
        sent_at=1_700_000_050,
        sender_id=101,
        forum_topic_id=5,
    )
    seed_message(
        conn,
        dialog_id=-100222,
        message_id=70,
        sent_at=1_700_000_070,
        sender_id=101,
        forum_topic_id=7,
    )
    conn.commit()

    result = await server._trace_account_messages(
        {"exact_account_id": 101, "exact_dialog_id": -100222, "exact_topic_id": 5}
    )

    evidence = result["data"]["groups"][0]["evidence"]
    assert [item["message_id"] for item in evidence] == [50]
    assert evidence[0]["topic_id"] == 5
    assert evidence[0]["topic_title"] == "Five"


@pytest.mark.asyncio
async def test_trace_excludes_service_rows(trace_server) -> None:
    server, conn, _client = trace_server
    seed_entity(conn, entity_id=101, name="Me", username="me")
    seed_dialog(conn, dialog_id=222, name="Alice", dialog_type="User")
    seed_message(conn, dialog_id=222, message_id=1, sent_at=1, sender_id=101, is_service=1)
    conn.commit()

    result = await server._trace_account_messages({"exact_account_id": 101})

    assert result["data"]["groups"] == []
    assert result["data"]["next_navigation"] is None


@pytest.mark.asyncio
async def test_trace_dialog_grouping_uses_current_page_and_topic_key(trace_server) -> None:
    server, conn, _client = trace_server
    seed_entity(conn, entity_id=101, name="Me", username="me")
    seed_dialog(conn, dialog_id=-100222, name="Forum", dialog_type="Forum")
    seed_topic(conn, dialog_id=-100222, topic_id=5, title="Five")
    seed_message(conn, dialog_id=-100222, message_id=1, sent_at=1, sender_id=101, forum_topic_id=5)
    seed_message(conn, dialog_id=-100222, message_id=2, sent_at=2, sender_id=101, forum_topic_id=5)
    conn.commit()

    result = await server._trace_account_messages(
        {"exact_account_id": 101, "group_by": "dialog", "limit": 1}
    )

    groups = result["data"]["groups"]
    assert len(groups) == 1
    assert groups[0]["group_key"] == "dialog:-100222:topic:5"
    assert [item["message_id"] for item in groups[0]["evidence"]] == [2]
    assert result["data"]["next_navigation"] is not None


@pytest.mark.asyncio
async def test_trace_exact_limit_without_extra_row_has_no_next_navigation(trace_server) -> None:
    server, conn, _client = trace_server
    seed_entity(conn, entity_id=101, name="Me", username="me")
    seed_message(conn, dialog_id=222, message_id=1, sent_at=1, sender_id=101)
    conn.commit()

    result = await server._trace_account_messages({"exact_account_id": 101, "limit": 1})

    assert result["data"]["coverage"]["observed_message_count"] == 1
    assert result["data"]["next_navigation"] is None
