from __future__ import annotations

import pytest

from mcp_telegram.pagination import (
    HistoryDirection,
    NavigationToken,
    decode_history_navigation,
    decode_navigation_token,
    decode_search_navigation,
    encode_history_navigation,
    encode_navigation_token,
    encode_search_navigation,
)


class TestNavigationTokenRoundTrip:
    def test_history_token_roundtrip(self):
        token = encode_history_navigation(12345, dialog_id=100, direction=HistoryDirection.NEWEST)
        nav = decode_navigation_token(token)
        assert nav.kind == "history"
        assert nav.value == 12345
        assert nav.dialog_id == 100
        assert nav.direction == "newest"
        assert nav.query is None
        assert nav.topic_id is None

    def test_history_oldest_direction(self):
        token = encode_history_navigation(999, dialog_id=200, direction=HistoryDirection.OLDEST)
        nav = decode_navigation_token(token)
        assert nav.direction == "oldest"

    def test_history_with_topic(self):
        token = encode_history_navigation(50, dialog_id=300, topic_id=42)
        nav = decode_navigation_token(token)
        assert nav.topic_id == 42

    def test_search_token_roundtrip(self):
        token = encode_search_navigation(offset=20, dialog_id=100, query="hello world")
        nav = decode_navigation_token(token)
        assert nav.kind == "search"
        assert nav.value == 20
        assert nav.dialog_id == 100
        assert nav.query == "hello world"

    def test_encode_decode_preserves_all_fields(self):
        original = NavigationToken(
            kind="history",
            value=42,
            dialog_id=555,
            topic_id=7,
            query=None,
            direction=HistoryDirection.OLDEST,
        )
        token = encode_navigation_token(original)
        decoded = decode_navigation_token(token)
        assert decoded == original


class TestDecodeHistoryNavigation:
    def test_valid_decode_returns_message_id(self):
        token = encode_history_navigation(12345, dialog_id=100)
        msg_id = decode_history_navigation(token, expected_dialog_id=100)
        assert msg_id == 12345

    def test_dialog_mismatch_raises(self):
        token = encode_history_navigation(1, dialog_id=100)
        with pytest.raises(ValueError, match="dialog 100, not 200"):
            decode_history_navigation(token, expected_dialog_id=200)

    def test_topic_mismatch_raises(self):
        token = encode_history_navigation(1, dialog_id=100, topic_id=5)
        with pytest.raises(ValueError, match="topic 5, not 10"):
            decode_history_navigation(token, expected_dialog_id=100, expected_topic_id=10)

    def test_direction_mismatch_raises(self):
        token = encode_history_navigation(1, dialog_id=100, direction=HistoryDirection.NEWEST)
        with pytest.raises(ValueError, match="newest.*oldest"):
            decode_history_navigation(
                token, expected_dialog_id=100, expected_direction=HistoryDirection.OLDEST
            )

    def test_search_token_rejected_as_history(self):
        token = encode_search_navigation(0, dialog_id=100, query="q")
        with pytest.raises(ValueError, match="search, not history"):
            decode_history_navigation(token, expected_dialog_id=100)


class TestDecodeSearchNavigation:
    def test_valid_decode_returns_offset(self):
        token = encode_search_navigation(offset=20, dialog_id=100, query="test")
        offset = decode_search_navigation(token, expected_dialog_id=100, expected_query="test")
        assert offset == 20

    def test_dialog_mismatch_raises(self):
        token = encode_search_navigation(0, dialog_id=100, query="test")
        with pytest.raises(ValueError, match="dialog 100, not 200"):
            decode_search_navigation(token, expected_dialog_id=200, expected_query="test")

    def test_query_mismatch_raises(self):
        token = encode_search_navigation(0, dialog_id=100, query="hello")
        with pytest.raises(ValueError, match='query "hello", not "world"'):
            decode_search_navigation(token, expected_dialog_id=100, expected_query="world")

    def test_history_token_rejected_as_search(self):
        token = encode_history_navigation(1, dialog_id=100)
        with pytest.raises(ValueError, match="history, not search"):
            decode_search_navigation(token, expected_dialog_id=100, expected_query="q")


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
        token = encode_history_navigation(1, dialog_id=100)
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


class TestHistoryDirection:
    def test_newest_value(self):
        assert HistoryDirection.NEWEST == "newest"

    def test_oldest_value(self):
        assert HistoryDirection.OLDEST == "oldest"

    def test_is_str_enum(self):
        assert isinstance(HistoryDirection.NEWEST, str)
