"""Shared temporal contract for MCP structured responses."""

from __future__ import annotations

import re
from contextvars import ContextVar
from copy import deepcopy
from datetime import UTC, datetime
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "UTC"
response_timezone: ContextVar[str] = ContextVar("response_timezone", default=DEFAULT_TIMEZONE)

# Keep the input contract narrower than ``datetime.fromisoformat``.  The
# public boundary fields document RFC 3339 UTC timestamps with an explicit
# ``T`` separator and either ``Z`` or ``+00:00``; Python's ISO parser also
# accepts space separators, comma fractions, and compact offsets.  Datetime
# stores microseconds, so accepting at most six fractional digits avoids
# silently losing precision during epoch conversion.
_RFC3339_DATETIME_PATTERN = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?"
_RFC3339_DATETIME_RE = re.compile(_RFC3339_DATETIME_PATTERN)
_RFC3339_OFFSET_RE = re.compile(_RFC3339_DATETIME_PATTERN + r"[+-][0-9]{2}:[0-9]{2}")
_RFC3339_UTC_RE = re.compile(_RFC3339_DATETIME_PATTERN + r"(?:Z|\+00:00)")


def validate_timezone(value: str) -> str:
    """Validate and normalize an explicit IANA timezone identifier."""
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown IANA timezone: {value}") from exc
    return value


def parse_utc_boundary(value: str | None, *, field: str) -> int | None:
    """Parse an absolute RFC 3339 UTC boundary into Unix seconds."""
    if value is None:
        return None
    if _RFC3339_UTC_RE.fullmatch(value) is None:
        # Preserve a useful distinction for otherwise well-formed timestamps
        # that omit the offset or use a non-UTC offset. Everything else stays
        # under the generic grammar error so callers can correct malformed
        # RFC 3339 input without guessing at the accepted UTC forms.
        if _RFC3339_DATETIME_RE.fullmatch(value) is not None or _RFC3339_OFFSET_RE.fullmatch(value) is not None:
            try:
                datetime.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"{field} must be an RFC 3339 timestamp in UTC") from exc
            raise ValueError(f"{field} must include the UTC offset Z or +00:00")
        raise ValueError(f"{field} must be an RFC 3339 timestamp in UTC")
    try:
        # ``fromisoformat`` handles the calendar/range validation after the
        # grammar check above.  Normalize ``Z`` for Python versions where the
        # parser does not recognize the RFC 3339 UTC designator directly.
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an RFC 3339 timestamp in UTC") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{field} must include the UTC offset Z or +00:00")
    return int(parsed.timestamp())


def format_timestamp(value: int | float, timezone: str) -> str:
    """Render Unix seconds once, in the requested response timezone."""
    return datetime.fromtimestamp(value, tz=UTC).astimezone(ZoneInfo(timezone)).isoformat(timespec="seconds")


def _is_temporal_key(key: str) -> bool:
    # The suffix only identifies values that need timezone presentation. It must
    # not be used to infer Telegram event provenance: technical observation
    # clocks are rendered the same way but are explicitly called out as such in
    # ``time_context``.
    # ``as_of`` is the observation cutoff used by account-trace coverage. It
    # is an epoch timestamp despite not carrying the usual ``*_at`` suffix,
    # so keep it in the same presentation contract as the other timestamps.
    return key in {"date", "as_of"} or key.endswith(("_at", "_date"))


_TIME_CONTEXT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "timezone": {
            "type": "string",
            "description": "Presentation timezone for every rendered temporal field.",
        },
        "canonical": {"type": "string", "const": "UTC"},
        "query_boundaries": {"type": "string", "const": "UTC"},
        "telegram_event_timestamps": {
            "type": "string",
            "const": "source_provided_only",
            "description": "Telegram event times are included only when supplied by Telegram.",
        },
        "technical_timestamps": {
            "type": "string",
            "const": "not_telegram_events",
            "description": "Technical observation clocks are not Telegram event times.",
        },
    },
    "required": [
        "timezone",
        "canonical",
        "query_boundaries",
        "telegram_event_timestamps",
        "technical_timestamps",
    ],
    "additionalProperties": False,
}


def _has_integer_type(schema: object) -> bool:
    schema_map = cast(dict[str, object], schema) if isinstance(schema, dict) else None
    schema_type = schema_map.get("type") if schema_map is not None else None
    return schema_type == "integer" or (isinstance(schema_type, list) and "integer" in schema_type)


def _rendered_timestamp_type(schema: object) -> object:
    """Return the schema type after Unix timestamps become ISO-8601 strings."""
    if not isinstance(schema, dict):
        return schema
    schema_map = cast(dict[str, object], schema)
    schema_type = schema_map.get("type")
    if schema_type == "integer":
        schema_map["type"] = "string"
    elif isinstance(schema_type, list) and "integer" in schema_type:
        rendered_types: list[object] = []
        for item in schema_type:
            rendered_item = "string" if item == "integer" else item
            if rendered_item not in rendered_types:
                rendered_types.append(rendered_item)
        schema_map["type"] = rendered_types
    return schema_map


def _normalize_schema_node(node: object) -> bool:
    """Normalize timestamp fields in a JSON schema node in place."""
    if isinstance(node, list):
        return any(_normalize_schema_node(item) for item in node)
    if not isinstance(node, dict):
        return False

    found_temporal = False
    node_map = cast(dict[str, object], node)
    properties = node_map.get("properties")
    if isinstance(properties, dict):
        sent_at_numeric = _has_integer_type(properties.get("sent_at"))
        for key, child in properties.items():
            if _is_temporal_key(key) and _has_integer_type(child):
                _rendered_timestamp_type(child)
                found_temporal = True
            if _normalize_schema_node(child):
                found_temporal = True

        # Message rows historically required both sent_at and a duplicate date.
        # The response projection retains only sent_at, so date must be optional.
        required = node_map.get("required")
        if isinstance(required, list) and "date" in required and "sent_at" in properties and sent_at_numeric:
            node_map["required"] = [field for field in required if field != "date"]

    for key, child in node_map.items():
        if key != "properties" and _normalize_schema_node(child):
            found_temporal = True
    return found_temporal


def normalize_temporal_output_schema(schema: dict[str, object] | None) -> dict[str, object] | None:
    """Align a tool's output schema with the shared temporal response contract.

    Schemas without numeric temporal fields are returned unchanged. This keeps
    non-temporal tools' descriptors stable while every timestamp-bearing tool
    advertises rendered strings and the optional ``time_context`` object.
    """
    if schema is None:
        return None
    normalized = deepcopy(schema)
    if not _normalize_schema_node(normalized):
        return schema
    properties = normalized.setdefault("properties", {})
    if isinstance(properties, dict):
        properties.setdefault("time_context", deepcopy(_TIME_CONTEXT_SCHEMA))
    return normalized


def _project(value: object, timezone: str) -> tuple[object, bool]:
    if isinstance(value, list):
        projected = [_project(item, timezone) for item in cast(list[object], value)]
        return [item for item, _ in projected], any(found for _, found in projected)
    if not isinstance(value, dict):
        return value, False

    value_map = cast(dict[str, object], value)
    output: dict[str, object] = {}
    found_temporal = False
    for key, item in value_map.items():
        # Older message contracts duplicated the same Telegram moment as sent_at
        # (Unix seconds) and date (UTC text). Keep the canonical semantic field once.
        if (
            key == "date"
            and isinstance(value_map.get("sent_at"), (int, float))
            and not isinstance(value_map.get("sent_at"), bool)
        ):
            found_temporal = True
            continue
        if _is_temporal_key(key) and isinstance(item, (int, float)) and not isinstance(item, bool):
            output[key] = format_timestamp(item, timezone)
            found_temporal = True
            continue
        child, child_found = _project(item, timezone)
        output[key] = child
        found_temporal = found_temporal or child_found
    return output, found_temporal


def project_temporal_response(content: dict[str, object], timezone: str) -> dict[str, object]:
    """Apply the common timestamp representation and declare its semantics."""
    projected_value, found = _project(content, timezone)
    projected = cast(dict[str, object], projected_value)
    # A non-UTC request must disclose the selected rendering zone even when
    # this particular page has no timestamp values (for example, an empty or
    # null-only result).  UTC remains implicit for empty results to preserve
    # compact responses while retaining the canonical default.
    if found or timezone != DEFAULT_TIMEZONE:
        projected["time_context"] = {
            "timezone": timezone,
            "canonical": "UTC",
            "query_boundaries": "UTC",
            "telegram_event_timestamps": "source_provided_only",
            "technical_timestamps": "not_telegram_events",
        }
    return projected
