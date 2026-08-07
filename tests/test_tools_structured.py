from __future__ import annotations

from mcp_telegram.tools.structured import navigation_metadata, result_count_semantics


class TestNavigationMetadata:
    def test_null_next_with_explicit_has_more_false(self):
        result = navigation_metadata(None, has_more=False)

        assert result["next_navigation"] is None
        assert result["has_more"] is False
        assert "direction" not in result
        assert "anchor_message_id" not in result
        assert "source_cursor" not in result

    def test_null_next_defaults_to_has_more_false(self):
        result = navigation_metadata(None)

        assert result["next_navigation"] is None
        assert result["has_more"] is False

    def test_non_null_next_defaults_to_has_more_true(self):
        result = navigation_metadata("page2-token")

        assert result["next_navigation"] == "page2-token"
        assert result["has_more"] is True

    def test_has_more_overrides_default(self):
        result = navigation_metadata("page2-token", has_more=False)

        assert result["has_more"] is False

    def test_with_direction(self):
        result = navigation_metadata("token", direction="newer")

        assert result["direction"] == "newer"

    def test_with_anchor_message_id(self):
        result = navigation_metadata("token", anchor_message_id=42)

        assert result["anchor_message_id"] == 42

    def test_with_source_cursor(self):
        result = navigation_metadata("token", source_cursor="src_1")

        assert result["source_cursor"] == "src_1"

    def test_all_optional_fields(self):
        result = navigation_metadata(
            "full-token",
            has_more=True,
            direction="newer",
            anchor_message_id=99,
            source_cursor="ref_abc",
        )

        assert result["next_navigation"] == "full-token"
        assert result["has_more"] is True
        assert result["direction"] == "newer"
        assert result["anchor_message_id"] == 99
        assert result["source_cursor"] == "ref_abc"


class TestResultCountSemantics:
    def test_basic_semantics(self):
        result = result_count_semantics(5, "exact")

        assert result == {
            "count": 5,
            "result_count_semantics": "exact",
        }

    def test_zero_count(self):
        result = result_count_semantics(0, "at_least")

        assert result["count"] == 0
        assert result["result_count_semantics"] == "at_least"
