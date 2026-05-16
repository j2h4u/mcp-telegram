from __future__ import annotations

import pytest

from account_trace_fixtures import (
    make_channel_signature_evidence,
    open_trace_db,
    seed_topic,
    seed_trace_fragment,
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
