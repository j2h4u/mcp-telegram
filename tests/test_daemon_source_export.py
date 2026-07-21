"""Tests for daemon_source_export parsing and serialization helpers.

Covers cursor encode/decode, watermark parsing, ISO formatting, and clamp.
All helpers are pure functions with no I/O — deterministic by design.
"""

from __future__ import annotations

import pytest

from mcp_telegram.daemon_source_export import (
    _clamp,
    _parse_source_cursor,
    _parse_source_watermark,
    _source_cursor,
    _source_iso,
)


class TestSourceCursor:
    def test_roundtrip_preserves_dialog_and_message_ids(self) -> None:
        cursor = _source_cursor(dialog_id=-100123, message_id=42)
        assert _parse_source_cursor(cursor) == (-100123, 42)

    def test_parse_none_returns_none(self) -> None:
        assert _parse_source_cursor(None) is None

    def test_parse_non_string_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid_cursor"):
            _parse_source_cursor(123)

    def test_parse_wrong_prefix_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid_cursor"):
            _parse_source_cursor("other:v1:dialog:1:message:2")

    def test_parse_garbage_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid_cursor"):
            _parse_source_cursor("not-even-a-cursor")


class TestParseSourceWatermark:
    def test_none_returns_none(self) -> None:
        assert _parse_source_watermark(None) is None

    def test_int_passthrough(self) -> None:
        assert _parse_source_watermark(1234567890) == 1234567890

    def test_string_digits_parsed_as_int(self) -> None:
        assert _parse_source_watermark("1234567890") == 1234567890

    def test_string_whitespace_around_digits(self) -> None:
        assert _parse_source_watermark(" 1234567890 ") == 1234567890

    def test_empty_string_returns_none(self) -> None:
        assert _parse_source_watermark("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert _parse_source_watermark("   ") is None

    def test_iso_format_parsed_to_timestamp(self) -> None:
        # 2024-01-15T12:30:00 UTC → 1705321800
        assert _parse_source_watermark("2024-01-15T12:30:00+00:00") == 1705321800

    def test_invalid_string_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid_updated_after"):
            _parse_source_watermark("not-a-number-or-date")

    def test_non_int_non_string_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid_updated_after"):
            _parse_source_watermark([1, 2, 3])


class TestSourceIso:
    def test_valid_epoch_returns_iso_format(self) -> None:
        assert _source_iso(1705321800) == "2024-01-15T12:30:00.000000Z"

    def test_none_returns_none(self) -> None:
        assert _source_iso(None) is None


class TestClamp:
    def test_value_below_low_returns_low(self) -> None:
        assert _clamp(0, 1, 500) == 1

    def test_value_within_range_unchanged(self) -> None:
        assert _clamp(50, 1, 500) == 50

    def test_value_above_high_returns_high(self) -> None:
        assert _clamp(999, 1, 500) == 500

    def test_value_at_low_boundary_unchanged(self) -> None:
        assert _clamp(1, 1, 500) == 1

    def test_value_at_high_boundary_unchanged(self) -> None:
        assert _clamp(500, 1, 500) == 500
