from __future__ import annotations

from typing import cast

import pytest

from mcp_telegram.temporal import (
    format_timestamp,
    normalize_temporal_output_schema,
    parse_utc_boundary,
    project_temporal_response,
    validate_timezone,
)
from mcp_telegram.tools.account_trace import TRACE_ACCOUNT_MESSAGES_OUTPUT_SCHEMA
from mcp_telegram.tools.reading import _search_result_structured_rows


def test_timezone_validation_rejects_unknown_zone_without_fallback() -> None:
    assert validate_timezone("Asia/Almaty") == "Asia/Almaty"
    with pytest.raises(ValueError, match="Unknown IANA timezone"):
        validate_timezone("Mars/Olympus")


@pytest.mark.parametrize("value", ["2026-01-01T00:00:00+05:00", "2026-01-01T00:00:00", "2026-01-01T00:00:00-00:00"])
def test_parse_utc_boundary_requires_explicit_utc(value: str) -> None:
    assert parse_utc_boundary("2026-01-01T00:00:00Z", field="since") == 1_767_225_600
    assert parse_utc_boundary("2026-01-01T00:00:00+00:00", field="since") == 1_767_225_600
    with pytest.raises(ValueError, match="must include the UTC offset Z or \\+00:00"):
        parse_utc_boundary(value, field="since")


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-01 00:00:00Z",
        "2026-01-01T00:00:00,123Z",
        "2026-01-01T00:00:00+0000",
        "2026-01-01T00:00:00.1234567Z",
        "2026-01-01T00:00Z",
        "２０２６-01-01T00:00:00Z",
    ],
)
def test_parse_utc_boundary_rejects_malformed_or_non_ascii_rfc3339(value: str) -> None:
    with pytest.raises(ValueError, match="RFC 3339 timestamp in UTC"):
        parse_utc_boundary(value, field="since")


def test_project_temporal_response_uses_one_rendered_timestamp_and_context() -> None:
    projected = project_temporal_response(
        {
            "messages": [{"sent_at": 1_700_000_000, "date": "legacy", "edit_date": 1_700_000_060}],
            "query_since": 1_700_000_000,
        },
        "Asia/Almaty",
    )

    messages = cast(list[dict[str, object]], projected["messages"])
    message = messages[0]
    assert message == {
        "sent_at": format_timestamp(1_700_000_000, "Asia/Almaty"),
        "edit_date": format_timestamp(1_700_000_060, "Asia/Almaty"),
    }
    assert projected["query_since"] == 1_700_000_000
    assert projected["time_context"] == {
        "timezone": "Asia/Almaty",
        "canonical": "UTC",
        "query_boundaries": "UTC",
        "telegram_event_timestamps": "source_provided_only",
        "technical_timestamps": "not_telegram_events",
    }


def test_project_temporal_response_keeps_date_when_sent_at_is_null() -> None:
    projected = project_temporal_response({"date": "2026-01-01", "sent_at": None}, "UTC")

    assert projected["date"] == "2026-01-01"
    assert projected["sent_at"] is None


def test_project_temporal_response_discloses_non_utc_zone_without_rows() -> None:
    projected = project_temporal_response({"messages": []}, "Asia/Almaty")

    assert projected["time_context"] == {
        "timezone": "Asia/Almaty",
        "canonical": "UTC",
        "query_boundaries": "UTC",
        "telegram_event_timestamps": "source_provided_only",
        "technical_timestamps": "not_telegram_events",
    }


def test_search_result_date_projects_in_requested_timezone() -> None:
    rows = _search_result_structured_rows(
        [{"dialog_id": 42, "message_id": 7, "sent_at": 1_700_000_000, "text": "needle"}],
        "needle",
    )

    assert rows[0]["date"] == 1_700_000_000
    response: dict[str, object] = {"results": rows}
    utc_results = cast(list[dict[str, object]], project_temporal_response(response, "UTC")["results"])
    assert utc_results[0]["date"] == "2023-11-14T22:13:20+00:00"
    almaty_results = cast(list[dict[str, object]], project_temporal_response(response, "Asia/Almaty")["results"])
    assert almaty_results[0]["date"] == "2023-11-15T04:13:20+06:00"


def test_normalize_temporal_output_schema_matches_projection() -> None:
    schema = {
        "type": "object",
        "properties": {
            "sent_at": {"type": "integer"},
            "date": {"type": "string"},
            "nested": {
                "type": "object",
                "properties": {
                    "edited_at": {"type": ["integer", "null"]},
                    "created_at": {"type": ["integer", "string", "null"]},
                },
            },
        },
        "required": ["sent_at", "date", "nested"],
        "additionalProperties": False,
    }

    normalized = normalize_temporal_output_schema(schema)
    assert normalized is not schema
    assert normalized is not None
    properties = cast(dict[str, object], normalized["properties"])
    sent_at = cast(dict[str, object], properties["sent_at"])
    nested = cast(dict[str, object], properties["nested"])
    nested_properties = cast(dict[str, object], nested.get("properties", {}))
    assert sent_at == {"type": "string"}
    assert nested_properties["edited_at"] == {"type": ["string", "null"]}
    assert nested_properties["created_at"] == {"type": ["string", "null"]}
    assert normalized["required"] == ["sent_at", "nested"]
    assert "time_context" in properties
    original_properties = cast(dict[str, object], schema["properties"])
    assert original_properties["sent_at"] == {"type": "integer"}


def test_account_trace_as_of_schema_matches_temporal_projection() -> None:
    normalized = normalize_temporal_output_schema(TRACE_ACCOUNT_MESSAGES_OUTPUT_SCHEMA)

    assert normalized is not None
    coverage = cast(dict[str, object], cast(dict[str, object], normalized["properties"])["coverage"])
    as_of = cast(dict[str, object], cast(dict[str, object], coverage["properties"])["as_of"])
    assert as_of == {"type": "string"}
    assert "time_context" in cast(dict[str, object], normalized["properties"])


def test_account_trace_as_of_result_projects_in_requested_timezone() -> None:
    projected = project_temporal_response(
        {
            "coverage": {
                "state": "unknown",
                "observed_message_count": 0,
                "dialogs_considered": 0,
                "dialogs_considered_basis": "none",
                "dialogs_with_hits": 0,
                "dialogs_with_gaps": 0,
                "as_of": 1_700_000_100,
            }
        },
        "Asia/Almaty",
    )

    coverage = cast(dict[str, object], projected["coverage"])
    assert coverage["as_of"] == "2023-11-15T04:15:00+06:00"
