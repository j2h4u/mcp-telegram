from __future__ import annotations

from mcp_telegram.tools.structured import (
    navigation_metadata,
    result_count_semantics,
    structured_warning,
    telegram_content,
)


def test_telegram_content_marks_text() -> None:
    result = telegram_content("Hello, world", "message_text")

    assert result["text"] == "Hello, world"
    assert result["is_telegram_content"] is True
    assert result["content_kind"] == "message_text"


def test_telegram_content_snippet_kind() -> None:
    result = telegram_content("short", "snippet")

    assert result["content_kind"] == "snippet"


def test_structured_warning_default_severity() -> None:
    result = structured_warning("timeout", "Request timed out")

    assert result["kind"] == "timeout"
    assert result["severity"] == "warning"
    assert result["message"] == "Request timed out"
    assert "action" not in result


def test_structured_warning_with_action() -> None:
    result = structured_warning("sync_gap", "Dialog not synced", severity="action_required", action="Call MarkDialogForSync")

    assert result["kind"] == "sync_gap"
    assert result["severity"] == "action_required"
    assert result["action"] == "Call MarkDialogForSync"


def test_navigation_metadata_basic() -> None:
    result = navigation_metadata(next_navigation="older|123")

    assert result["next_navigation"] == "older|123"
    assert result["has_more"] is True


def test_navigation_metadata_none_has_no_more() -> None:
    result = navigation_metadata(next_navigation=None)

    assert result["next_navigation"] is None
    assert result["has_more"] is False


def test_navigation_metadata_explicit_has_more() -> None:
    result = navigation_metadata(next_navigation=None, has_more=True)

    assert result["has_more"] is True


def test_navigation_metadata_with_direction() -> None:
    result = navigation_metadata(next_navigation="older|1", direction="older")

    assert result["direction"] == "older"


def test_navigation_metadata_with_anchor() -> None:
    result = navigation_metadata(next_navigation="around|42", anchor_message_id=42)

    assert result["anchor_message_id"] == 42


def test_navigation_metadata_with_source_cursor() -> None:
    result = navigation_metadata(next_navigation="newer|1", source_cursor="origin-1")

    assert result["source_cursor"] == "origin-1"


def test_navigation_metadata_all_optional_fields() -> None:
    result = navigation_metadata(
        next_navigation="forward|99",
        has_more=False,
        direction="forward",
        anchor_message_id=99,
        source_cursor="src-99",
    )

    assert result["next_navigation"] == "forward|99"
    assert result["has_more"] is False
    assert result["direction"] == "forward"
    assert result["anchor_message_id"] == 99
    assert result["source_cursor"] == "src-99"
    assert "direction" not in set(navigation_metadata("token", has_more=True))


def test_result_count_semantics() -> None:
    result = result_count_semantics(42, "count is the total number of messages returned")

    assert result["count"] == 42
    assert result["result_count_semantics"] == "count is the total number of messages returned"
