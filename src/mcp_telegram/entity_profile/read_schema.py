"""Closed envelopes for bounded local profile projections."""

from typing import cast

from .output_schema import (
    COLLECTION_CONSTRUCTORS,
    build_profile_fields_output_schema,
    collection_item_schema,
    history_value_schema,
)


def closed(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


_TIMESTAMP = {"type": ["string", "null"]}
_TEXT = {"type": ["string", "null"]}
_NUMBER = {"type": ["integer", "null"]}
_EVIDENCE = closed(
    {
        "path": {"type": "string"},
        "availability": {"type": "string"},
        "authoritative": {"type": "boolean"},
        "reason": _TEXT,
        "source": _TEXT,
        "observed_at": _TIMESTAMP,
        "observation_started_at": _TIMESTAMP,
        "observation_completed_at": _TIMESTAMP,
        "age_seconds": _NUMBER,
    }
)
_HISTORY_COVERAGE = closed(
    {
        "mode": {"const": "observed_changes_only"},
        "complete_past_history": {"const": False},
        "tracking_started_at": {"type": "null"},
        "first_retained_event_id": _NUMBER,
        "latest_retained_event_id": _NUMBER,
        "gaps": {"type": "array", "items": {"type": "string"}},
    }
)
_SUMMARY = closed(
    {
        "metadata": closed(
            {
                "pinned_to_top": {"type": "array", "items": {"type": "integer"}},
                "max_read_id": {"type": "integer"},
                "chat_notifications_enabled": {"type": "boolean"},
            }
        ),
        "metadata_evidence": {
            "type": "array",
            "items": closed(
                {
                    "name": {"enum": ["pinned_to_top", "max_read_id", "chat_notifications_enabled"]},
                    "observation_started_at": _TIMESTAMP,
                    "observation_completed_at": _TIMESTAMP,
                }
            ),
        },
        "reported_counts": {
            "type": "array",
            "items": closed(
                {"count": _NUMBER, "observation_started_at": _TIMESTAMP, "observation_completed_at": _TIMESTAMP}
            ),
        },
        "diagnostics": {"type": "array", "items": {"type": "string"}},
        "count_discrepancy": {"type": "boolean"},
        "identifiable": {"type": "boolean"},
        "status": {"type": "string"},
        "observed_at": _TIMESTAMP,
        "reason": _TEXT,
        "retry_at": _TIMESTAMP,
        "complete": {"type": "boolean"},
        "traversal_complete": {"type": "boolean"},
        "reported_count": _NUMBER,
        "retained_count": _NUMBER,
        "retained_unique_count": _NUMBER,
        "details_complete": {"type": ["boolean", "null"]},
        "comparable_membership": {"type": ["boolean", "null"]},
        "generation": _NUMBER,
        "scan": _NUMBER,
        "changed_during_scan": {"type": "boolean"},
        "published_generation": _NUMBER,
        "published_scan": _NUMBER,
        "observation_started_at": _TIMESTAMP,
        "observation_completed_at": _TIMESTAMP,
    }
)
_LEGACY_EVIDENCE = closed(
    {
        "owner_account_id": {"type": "integer"},
        "section": {"type": "string"},
        "observed_at": _TIMESTAMP,
        "stored_status": {"type": "string"},
        "stored_reason": _TEXT,
        "original_status": _TEXT,
        "traversal_coverage": {"const": "unknown"},
    }
)
_SOURCES = {
    "type": "array",
    "items": closed(
        {
            "source": {"enum": ["published", "active", "legacy"]},
            "cursor": {"type": "string"},
            "generation": {"type": "integer"},
            "scan": {"type": "integer"},
        }
    ),
}
_PAGE_COMMON = {
    "section": {"type": "string"},
    "source": {"type": "string"},
    "generation": _NUMBER,
    "scan": _NUMBER,
    "value_semantics": {"type": "string"},
    "membership_semantics": {"type": "string"},
    "coverage": {"enum": ["published_traversal", "partial_scan", "legacy_unknown", "not_observed"]},
    "next_cursor": _TEXT,
    "has_more": {"type": "boolean"},
    "returned_count": {"type": "integer"},
    "returned_active_count": {"type": "integer"},
    "available_sources": _SOURCES,
    "legacy_evidence": _LEGACY_EVIDENCE,
}


def _collection_page(section: str) -> dict:
    item = closed(
        {
            "item_key": _TEXT,
            "identity_kind": {"enum": ["remote", "observation", "legacy"]},
            "state": {"enum": ["retained", "deleted", "unsupported"]},
            "value": collection_item_schema(section),
            "observed_at": _TIMESTAMP,
            "age_seconds": _NUMBER,
            "field_evidence": {"type": "array", "items": _EVIDENCE},
        }
    )
    return closed({**_PAGE_COMMON, "section": {"const": section}, "items": {"type": "array", "items": item}})


def _history_event() -> dict:
    root = build_profile_fields_output_schema()
    keys = (
        list(cast(dict[str, object], root["properties"]))
        + ["legacy.name", "legacy.username", "owner.saved_contact_name"]
        + ["photo." + role for role in ("current", "public", "personal", "fallback")]
    )
    value_schemas = [history_value_schema(key) for key in keys]
    value_schemas.append({"type": ["string", "null"]})
    event = closed(
        {
            "previous_observations": {
                "type": "array",
                "items": closed(
                    {
                        "path": {"type": "string"},
                        "order": _NUMBER,
                        "observed_at": _TIMESTAMP,
                        "source": _TEXT,
                        "observation_started_at": _TIMESTAMP,
                        "observation_completed_at": _TIMESTAMP,
                        "age_seconds": _NUMBER,
                    }
                ),
            },
            "before_precision": {"enum": ["legacy_normalized", None]},
            "event_id": {"type": "integer"},
            "change_key": {"type": "string"},
            "category": {"enum": ["profile", "private_owner", "collection"]},
            "before": {"anyOf": value_schemas},
            "after": {"anyOf": value_schemas},
            "supported": {"type": "boolean"},
            "observed_at": _TIMESTAMP,
            "source": {"type": "string"},
            "age_seconds": _NUMBER,
            "observation_started_at": _TIMESTAMP,
            "observation_completed_at": _TIMESTAMP,
            "server_time": {"type": ["number", "null"]},
            "time_semantics": {"const": "observed_evidence_not_action_time"},
        }
    )
    event["allOf"] = [
        {
            "if": {
                "properties": {"change_key": {"const": key}, "supported": {"const": True}},
                "required": ["change_key", "supported"],
            },
            "then": {"properties": {"before": history_value_schema(key), "after": history_value_schema(key)}},
        }
        for key in keys
    ]
    event["allOf"].append(
        {
            "if": {"properties": {"category": {"const": "collection"}}, "required": ["category"]},
            "then": {"properties": {"before": {"type": ["string", "null"]}, "after": {"type": ["string", "null"]}}},
        }
    )
    return event


def profile_read_schemas() -> tuple[dict, dict]:
    fields = build_profile_fields_output_schema()
    definitions = cast(dict, fields.pop("$defs"))
    semantic = closed(
        {
            "fields": fields,
            "field_evidence": {"type": "array", "items": _EVIDENCE},
            "availability": {"enum": ["retained", "not_observed"]},
            "complete": {"type": "boolean"},
            "normalization_version": {"type": "string"},
            "source": {"type": "string"},
            "acquisition": closed(
                {"observation_started_at": _TIMESTAMP, "observation_completed_at": _TIMESTAMP, "age_seconds": _NUMBER}
            ),
        }
    )
    history = closed(
        {
            **_PAGE_COMMON,
            "section": {"const": "history"},
            "watermark": {"type": "integer"},
            "items": {"type": "array", "items": _history_event()},
        }
    )
    return {
        "semantic": semantic,
        "collections": closed(dict.fromkeys(COLLECTION_CONSTRUCTORS, _SUMMARY)),
        "history_coverage": _HISTORY_COVERAGE,
        "page": {"anyOf": [_collection_page(section) for section in COLLECTION_CONSTRUCTORS] + [history]},
    }, definitions
