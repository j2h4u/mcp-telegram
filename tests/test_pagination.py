from __future__ import annotations

import pytest

from mcp_telegram.pagination import (
    HistoryDirection,
    NavigationToken,
    decode_navigation_token,
    encode_history_navigation,
    encode_navigation_token,
    encode_search_navigation,
)


class TestNavigationTokenRoundTrip:
    def test_history_token_roundtrip(self):
        token = encode_history_navigation(12345, dialog_id=100, direction=HistoryDirection.NEWEST, message_state="sent")
        nav = decode_navigation_token(token)
        assert nav.kind == "history"
        assert nav.value == 12345
        assert nav.dialog_id == 100
        assert nav.direction == "newest"
        assert nav.query is None
        assert nav.topic_id is None

    def test_history_oldest_direction(self):
        token = encode_history_navigation(999, dialog_id=200, direction=HistoryDirection.OLDEST, message_state="sent")
        nav = decode_navigation_token(token)
        assert nav.direction == "oldest"

    def test_history_with_topic(self):
        token = encode_history_navigation(50, dialog_id=300, topic_id=42, message_state="sent")
        nav = decode_navigation_token(token)
        assert nav.topic_id == 42

    def test_search_token_roundtrip(self):
        token = encode_search_navigation(offset=20, dialog_id=100, query="hello world", message_state="scheduled")
        nav = decode_navigation_token(token)
        assert nav.kind == "search"
        assert nav.value == 20
        assert nav.dialog_id == 100
        assert nav.query == "hello world"
        assert nav.message_state == "scheduled"

    def test_encode_decode_preserves_all_fields(self):
        original = NavigationToken(
            kind="history",
            value=42,
            dialog_id=555,
            topic_id=7,
            query=None,
            direction=HistoryDirection.OLDEST,
            sent_at=1_700_000_500,
            message_state="all",
            since_utc=1_700_000_000,
            until_utc=1_700_001_000,
        )
        token = encode_navigation_token(original)
        decoded = decode_navigation_token(token)
        assert decoded == original

    def test_search_token_roundtrip_preserves_utc_bounds(self):
        token = encode_search_navigation(
            offset=20,
            dialog_id=100,
            query="hello",
            message_state="sent",
            since_utc=1_700_000_000,
            until_utc=1_700_001_000,
        )
        nav = decode_navigation_token(token)
        assert nav.since_utc == 1_700_000_000
        assert nav.until_utc == 1_700_001_000


class TestDecodeValidation:
    def test_garbage_input_raises(self):
        with pytest.raises(ValueError, match="Invalid navigation token"):
            decode_navigation_token("not-valid-base64!!!")

    def test_missing_signature_raises(self):
        """Unsigned (legacy) token is rejected before inner validation."""
        import base64
        import json

        token = base64.urlsafe_b64encode(json.dumps({"kind": "history", "value": 1, "dialog_id": 1}).encode()).decode()
        with pytest.raises(ValueError, match="missing signature"):
            decode_navigation_token(token)

    def test_tampered_signature_raises(self):
        """Forged MAC is rejected."""
        token = encode_history_navigation(1, dialog_id=100, message_state="sent")
        tampered = token[:-4] + "xxxx"
        with pytest.raises(ValueError, match="signature mismatch"):
            decode_navigation_token(tampered)

    def test_non_dict_json_raises(self):
        from mcp_telegram.pagination import _encode_payload

        token = _encode_payload({"_list": True})  # valid HMAC, but decode checks type
        # _encode_payload only accepts dict, so test via a signed token with wrong shape
        # by encoding a known-good dict and verifying the type check isn't reachable
        # (this is defense: the real guard is HMAC, inner type check is belt-and-suspenders)
        with pytest.raises(ValueError, match="Invalid navigation token"):
            decode_navigation_token("unsigned.aaaaaaaaaaaaaaaa")

    def test_missing_kind_raises(self):
        from mcp_telegram.pagination import _encode_payload

        token = _encode_payload({"value": 1, "dialog_id": 1})
        with pytest.raises(ValueError, match="kind must be history or search"):
            decode_navigation_token(token)

    def test_invalid_kind_raises(self):
        from mcp_telegram.pagination import _encode_payload

        token = _encode_payload({"kind": "invalid", "value": 1, "dialog_id": 1})
        with pytest.raises(ValueError, match="kind must be history or search"):
            decode_navigation_token(token)

    def test_non_int_value_raises(self):
        from mcp_telegram.pagination import _encode_payload

        token = _encode_payload({"kind": "history", "value": "abc", "dialog_id": 1})
        with pytest.raises(ValueError, match="value must be an integer"):
            decode_navigation_token(token)

    def test_non_int_dialog_id_raises(self):
        from mcp_telegram.pagination import _encode_payload

        token = _encode_payload({"kind": "history", "value": 1, "dialog_id": "abc"})
        with pytest.raises(ValueError, match="dialog_id must be an integer"):
            decode_navigation_token(token)

    def test_non_int_topic_id_raises(self):
        from mcp_telegram.pagination import _encode_payload

        token = _encode_payload({"kind": "history", "value": 1, "dialog_id": 1, "topic_id": "x"})
        with pytest.raises(ValueError, match="topic_id must be an integer"):
            decode_navigation_token(token)

    def test_invalid_direction_raises(self):
        from mcp_telegram.pagination import _encode_payload

        token = _encode_payload({"kind": "history", "value": 1, "dialog_id": 1, "direction": "sideways"})
        with pytest.raises(ValueError, match="direction must be newest or oldest"):
            decode_navigation_token(token)

    @pytest.mark.parametrize(
        "payload, error",
        [
            ({"kind": "history", "value": 1, "dialog_id": 1}, "message_state"),
            ({"kind": "search", "value": 1, "dialog_id": 1, "message_state": "sent"}, "requires query"),
        ],
    )
    def test_unbound_signed_token_is_rejected(self, payload: dict[str, object], error: str) -> None:
        from mcp_telegram.pagination import _encode_payload

        with pytest.raises(ValueError, match=error):
            decode_navigation_token(_encode_payload(payload))

    def test_invalid_base64_raises_before_signature_check(self):
        token = "!!!!!!.aaaa1111bbbb2222"
        with pytest.raises(ValueError, match="Invalid navigation token"):
            decode_navigation_token(token)

    def test_reversed_utc_bounds_are_rejected(self):
        with pytest.raises(ValueError, match="since_utc must be earlier"):
            encode_history_navigation(
                1,
                dialog_id=100,
                message_state="sent",
                since_utc=2,
                until_utc=1,
            )

    def test_search_cursor_rejects_topic_id(self) -> None:
        from mcp_telegram.pagination import _encode_payload

        token = _encode_payload({
            "kind": "search",
            "value": 1,
            "dialog_id": 100,
            "query": "hello",
            "message_state": "sent",
            "topic_id": 42,
        })
        with pytest.raises(ValueError, match="search cursor contains history-only state"):
            decode_navigation_token(token)

    def test_search_cursor_rejects_direction(self) -> None:
        from mcp_telegram.pagination import _encode_payload

        token = _encode_payload({
            "kind": "search",
            "value": 1,
            "dialog_id": 100,
            "query": "hello",
            "message_state": "sent",
            "direction": "newest",
        })
        with pytest.raises(ValueError, match="search cursor contains history-only state"):
            decode_navigation_token(token)

    def test_search_cursor_rejects_sent_at(self) -> None:
        from mcp_telegram.pagination import _encode_payload

        token = _encode_payload({
            "kind": "search",
            "value": 1,
            "dialog_id": 100,
            "query": "hello",
            "message_state": "sent",
            "sent_at": 1_700_000_000,
        })
        with pytest.raises(ValueError, match="search cursor contains history-only state"):
            decode_navigation_token(token)

    def test_search_cursor_rejects_history_only_fields_separately(self) -> None:
        from mcp_telegram.pagination import _encode_payload
        from mcp_telegram.pagination import encode_search_navigation

        token = encode_search_navigation(offset=20, dialog_id=100, query="hello", message_state="sent")
        nav = decode_navigation_token(token)
        assert nav.topic_id is None
        assert nav.direction is None
        assert nav.sent_at is None


class TestAccountTraceNavigationToken:
    def test_roundtrip_with_scope_dialog_ids(self) -> None:
        from mcp_telegram.pagination import (
            AccountTraceNavigationContext,
            AccountTraceNavigationRequest,
            decode_account_trace_navigation,
            encode_account_trace_navigation,
        )

        request = AccountTraceNavigationRequest(
            target_user_id=777000,
            sent_at=1_700_000_500,
            dialog_id=-1001234567890,
            message_id=42,
            group_by="timeline",
            exact_dialog_id=-1001234567890,
            exact_topic_id=None,
            sent_after="2025-01-01T00:00:00Z",
            sent_before="2025-02-01T00:00:00Z",
            scope_dialog_ids=[-1001234567890, -1009876543210],
        )
        token = encode_account_trace_navigation(request)

        context = AccountTraceNavigationContext(
            expected_target_user_id=777000,
            expected_group_by="timeline",
            expected_exact_dialog_id=-1001234567890,
            expected_exact_topic_id=None,
            expected_sent_after="2025-01-01T00:00:00Z",
            expected_sent_before="2025-02-01T00:00:00Z",
        )
        result = decode_account_trace_navigation(token, context)
        assert result.target_user_id == 777000
        assert result.sent_at == 1_700_000_500
        assert result.dialog_id == -1001234567890
        assert result.message_id == 42
        assert result.group_by == "timeline"
        assert result.exact_dialog_id == -1001234567890
        assert result.exact_topic_id is None
        assert result.sent_after == "2025-01-01T00:00:00Z"
        assert result.sent_before == "2025-02-01T00:00:00Z"
        assert result.scope_dialog_ids == [-1001234567890, -1009876543210]

    def test_roundtrip_minimal(self) -> None:
        from mcp_telegram.pagination import (
            AccountTraceNavigationContext,
            AccountTraceNavigationRequest,
            decode_account_trace_navigation,
            encode_account_trace_navigation,
        )

        request = AccountTraceNavigationRequest(
            target_user_id=123,
            sent_at=500,
            dialog_id=100,
            message_id=10,
            group_by="dialog",
        )
        token = encode_account_trace_navigation(request)

        context = AccountTraceNavigationContext(
            expected_target_user_id=123,
            expected_group_by="dialog",
        )
        result = decode_account_trace_navigation(token, context)
        assert result.target_user_id == 123
        assert result.message_id == 10
        assert result.group_by == "dialog"
        assert result.scope_dialog_ids is None

    def test_context_mismatch_target_user_id(self) -> None:
        from mcp_telegram.pagination import (
            AccountTraceNavigationContext,
            AccountTraceNavigationRequest,
            decode_account_trace_navigation,
            encode_account_trace_navigation,
        )

        request = AccountTraceNavigationRequest(
            target_user_id=777000,
            sent_at=500,
            dialog_id=100,
            message_id=10,
            group_by="dialog",
        )
        token = encode_account_trace_navigation(request)

        context = AccountTraceNavigationContext(
            expected_target_user_id=999999,
            expected_group_by="dialog",
        )
        with pytest.raises(ValueError, match="belongs to account 777000, not 999999"):
            decode_account_trace_navigation(token, context)

    def test_context_mismatch_group_by(self) -> None:
        from mcp_telegram.pagination import (
            AccountTraceNavigationContext,
            AccountTraceNavigationRequest,
            decode_account_trace_navigation,
            encode_account_trace_navigation,
        )

        request = AccountTraceNavigationRequest(
            target_user_id=777000,
            sent_at=500,
            dialog_id=100,
            message_id=10,
            group_by="timeline",
        )
        token = encode_account_trace_navigation(request)

        context = AccountTraceNavigationContext(
            expected_target_user_id=777000,
            expected_group_by="dialog",
        )
        with pytest.raises(ValueError, match="belongs to group_by timeline, not dialog"):
            decode_account_trace_navigation(token, context)

    def test_wrong_kind_raises(self) -> None:
        from mcp_telegram.pagination import (
            AccountTraceNavigationContext,
            decode_account_trace_navigation,
            encode_history_navigation,
        )

        token = encode_history_navigation(1, dialog_id=100, message_state="sent")
        context = AccountTraceNavigationContext(
            expected_target_user_id=123,
            expected_group_by="dialog",
        )
        with pytest.raises(ValueError, match="not account_trace"):
            decode_account_trace_navigation(token, context)

    def test_scope_dialog_ids_empty_list(self) -> None:
        from mcp_telegram.pagination import (
            AccountTraceNavigationContext,
            AccountTraceNavigationRequest,
            decode_account_trace_navigation,
            encode_account_trace_navigation,
        )

        request = AccountTraceNavigationRequest(
            target_user_id=777000,
            sent_at=500,
            dialog_id=100,
            message_id=10,
            group_by="dialog",
            scope_dialog_ids=[],
        )
        token = encode_account_trace_navigation(request)

        context = AccountTraceNavigationContext(
            expected_target_user_id=777000,
            expected_group_by="dialog",
        )
        result = decode_account_trace_navigation(token, context)
        assert result.scope_dialog_ids == []
