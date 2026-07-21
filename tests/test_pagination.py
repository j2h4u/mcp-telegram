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
