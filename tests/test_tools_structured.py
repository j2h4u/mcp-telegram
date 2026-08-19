from __future__ import annotations

from mcp_telegram.tools.structured import navigation_metadata, result_count_semantics


def test_navigation_metadata_defaults_and_preserves_cursor_context() -> None:
    assert navigation_metadata(None) == {"next_navigation": None, "has_more": False}
    assert navigation_metadata(
        "next-token",
        direction="older",
        anchor_message_id=42,
        source_cursor="source-token",
    ) == {
        "next_navigation": "next-token",
        "has_more": True,
        "direction": "older",
        "anchor_message_id": 42,
        "source_cursor": "source-token",
    }


def test_navigation_metadata_allows_explicit_has_more_override() -> None:
    assert navigation_metadata("next-token", has_more=False)["has_more"] is False


def test_result_count_semantics_exposes_count_contract() -> None:
    assert result_count_semantics(0, "at_least") == {
        "count": 0,
        "result_count_semantics": "at_least",
    }
