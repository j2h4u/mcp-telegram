"""Tests for structured MCP response helpers (TypedDict factories)."""

from __future__ import annotations

from mcp_telegram.tools.structured import (
    NavigationDirection,
    navigation_metadata,
    result_count_semantics,
    structured_warning,
    telegram_content,
)


class TestTelegramContent:
    def test_markup_kind(self) -> None:
        result = telegram_content("hello <b>world</b>", "message_text")
        assert result["text"] == "hello <b>world</b>"
        assert result["is_telegram_content"] is True
        assert result["content_kind"] == "message_text"

    def test_media_description_kind(self) -> None:
        result = telegram_content("[фото]", "media_description")
        assert result["content_kind"] == "media_description"

    def test_private_forward_name_kind(self) -> None:
        result = telegram_content("Alice", "private_forward_name")
        assert result["content_kind"] == "private_forward_name"


class TestStructuredWarning:
    def test_default_severity_is_warning(self) -> None:
        w = structured_warning("parse_error", "Could not parse date")
        assert w["kind"] == "parse_error"
        assert w["severity"] == "warning"
        assert w["message"] == "Could not parse date"
        assert "action" not in w

    def test_with_action(self) -> None:
        w = structured_warning("sync_gap", "Dialog not fully synced", severity="info", action="Retry after 60s")
        assert w["severity"] == "info"
        assert w["action"] == "Retry after 60s"

    def test_action_required_severity(self) -> None:
        w = structured_warning("auth_expired", "Session token expired", severity="action_required", action="Re-authenticate")
        assert w["severity"] == "action_required"


class TestNavigationMetadata:
    def test_next_token_with_has_more(self) -> None:
        meta = navigation_metadata("token_abc")
        assert meta["next_navigation"] == "token_abc"
        assert meta["has_more"] is True

    def test_no_next_token(self) -> None:
        meta = navigation_metadata(None)
        assert meta["next_navigation"] is None
        assert meta["has_more"] is False

    def test_explicit_has_more_overrides_auto(self) -> None:
        meta = navigation_metadata(None, has_more=True)
        assert meta["next_navigation"] is None
        assert meta["has_more"] is True

    def test_with_direction(self) -> None:
        meta = navigation_metadata("token", direction="older")
        assert meta["direction"] == "older"

    def test_with_direction_newer(self) -> None:
        meta = navigation_metadata("token", direction="newer")
        assert meta["direction"] == "newer"

    def test_with_anchor_message_id(self) -> None:
        meta = navigation_metadata("token", anchor_message_id=42)
        assert meta["anchor_message_id"] == 42

    def test_with_source_cursor(self) -> None:
        meta = navigation_metadata("token", source_cursor="cursor_xyz")
        assert meta["source_cursor"] == "cursor_xyz"

    def test_all_optional_fields(self) -> None:
        meta = navigation_metadata(
            "full_token",
            has_more=True,
            direction="around",
            anchor_message_id=100,
            source_cursor="sc_001",
        )
        assert meta["next_navigation"] == "full_token"
        assert meta["has_more"] is True
        assert meta["direction"] == "around"
        assert meta["anchor_message_id"] == 100
        assert meta["source_cursor"] == "sc_001"

    def test_direction_type_is_correct(self) -> None:
        assert "older" in NavigationDirection.__args__  # type: ignore[union-attr]
        assert "newer" in NavigationDirection.__args__  # type: ignore[union-attr]
        assert "around" in NavigationDirection.__args__  # type: ignore[union-attr]


class TestResultCountSemantics:
    def test_basic(self) -> None:
        r = result_count_semantics(42, "exact")
        assert r["count"] == 42
        assert r["result_count_semantics"] == "exact"

    def test_at_least_semantics(self) -> None:
        r = result_count_semantics(100, "at_least")
        assert r["count"] == 100
        assert r["result_count_semantics"] == "at_least"

    def test_estimated_semantics(self) -> None:
        r = result_count_semantics(500, "estimated")
        assert r["count"] == 500
        assert r["result_count_semantics"] == "estimated"

    def test_zero_count(self) -> None:
        r = result_count_semantics(0, "exact")
        assert r["count"] == 0
