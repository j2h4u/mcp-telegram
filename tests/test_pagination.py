from __future__ import annotations

import base64
import json

import pytest

from mcp_telegram.pagination import decode_cursor, encode_cursor


def test_round_trip() -> None:
    """encode_cursor followed by decode_cursor returns original message_id."""
    token = encode_cursor(12345, 999)
    result = decode_cursor(token, 999)
    assert result == 12345


def test_cross_dialog_error() -> None:
    """decode_cursor with wrong expected_dialog_id raises ValueError."""
    token = encode_cursor(12345, 999)
    with pytest.raises(ValueError, match="999"):
        decode_cursor(token, 888)


def test_invalid_base64_raises() -> None:
    """decode_cursor with garbage string raises an exception."""
    with pytest.raises(Exception):  # noqa: B017
        decode_cursor("not-valid-base64!!!", 999)
