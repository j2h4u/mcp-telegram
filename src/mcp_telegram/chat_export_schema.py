"""Fixed export schemas and the deterministic v1-to-v2 migration."""

from collections.abc import Mapping
from pathlib import Path
from typing import cast

import ijson
from jsonschema import Draft202012Validator

from .chat_export_projection import deduplicate_export_message

CURRENT_FORMAT_VERSION = 2
SUPPORTED_FORMAT_VERSIONS = (1, CURRENT_FORMAT_VERSION)
type Facts = dict[str, object]

_ID = {"type": "string", "pattern": "^[1-9][0-9]*$"}
_DIALOG_ID = {"type": "string", "pattern": "^-[1-9][0-9]*$"}
_NULLABLE_ID = {"type": ["string", "null"], "pattern": "^-?[1-9][0-9]*$"}
_STRING = {"type": ["string", "null"]}
_OBJECT = {"type": ["object", "null"]}
_OBJECTS = {"type": ["array", "null"], "items": {"type": "object"}}


def _identity_fields(prefix: str) -> Facts:
    return {
        prefix + "id": _NULLABLE_ID,
        **{prefix + key: _STRING for key in ("kind", "name", "username", "role", "rank")},
        prefix + "is_admin": {"type": ["boolean", "null"]},
        prefix + "metadata": {"type": "object"},
    }


_GROUP = {"type": "object", "required": ["dialog_id"], "properties": {"dialog_id": _DIALOG_ID}}
_REACTOR = {
    "type": "object",
    "properties": {
        **_identity_fields("actor_"),
        "reaction": {"type": ["object", "string", "null"]},
        "date": _STRING,
        "raw": _OBJECT,
    },
}
LEGACY_V1_RECORD_SCHEMAS = {
    "group": _GROUP,
    "metadata": {
        "type": "object",
        "required": ["order", "peers"],
        "properties": {
            "order": {"type": "string"},
            "peers": {"type": "array", "minItems": 1, "items": _GROUP},
            "exporter": {"type": "object"},
        },
    },
    "messages.item": {
        "type": "object",
        "required": ["dialog_id", "message_id", "message_key", "reactors"],
        "properties": {
            **_identity_fields("author_"),
            "dialog_id": _DIALOG_ID,
            "message_id": _ID,
            "message_key": {"type": "string"},
            "kind": {"enum": ["message", "service"]},
            "text": {"type": "string"},
            "date": _STRING,
            "edited_at": _STRING,
            "topic_id": _NULLABLE_ID,
            "grouped_id": _NULLABLE_ID,
            "reply_to_dialog_id": _NULLABLE_ID,
            "reply_to_message_id": _NULLABLE_ID,
            "reply_key": _STRING,
            "entities": _OBJECTS,
            "related_users": {"type": "array", "items": {"type": "object"}},
            "service_action": _OBJECT,
            "topic": _OBJECT,
            "metadata": {"type": "object"},
            "reactions": {
                "type": "object",
                "properties": {"aggregate": _OBJECT, "can_view_list": {"type": "boolean"}},
            },
            "reactors": {"type": "array", "items": _REACTOR},
        },
    },
    "admin_events.item": {
        "type": "object",
        "required": ["dialog_id", "event_id"],
        "properties": {
            **_identity_fields("actor_"),
            "dialog_id": _DIALOG_ID,
            "event_id": _ID,
            "date": _STRING,
            "action": _OBJECT,
            "related_users": {"type": "array", "items": {"type": "object"}},
        },
    },
    "export": {
        "type": "object",
        "required": ["messages", "admin_events", "reactors"],
        "additionalProperties": False,
        "properties": {key: {"type": "integer", "minimum": 0} for key in ("messages", "admin_events", "reactors")},
    },
}
RECORD_SCHEMAS = LEGACY_V1_RECORD_SCHEMAS
EXPORT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["format_version", "group", "metadata", "admin_events", "messages", "export"],
    "additionalProperties": False,
    "properties": {
        "format_version": {"const": CURRENT_FORMAT_VERSION, "type": "integer"},
        "group": RECORD_SCHEMAS["group"],
        "metadata": RECORD_SCHEMAS["metadata"],
        "admin_events": {"type": "array", "items": RECORD_SCHEMAS["admin_events.item"]},
        "messages": {"type": "array", "items": RECORD_SCHEMAS["messages.item"]},
        "export": RECORD_SCHEMAS["export"],
    },
}
Draft202012Validator.check_schema(EXPORT_SCHEMA)
_VALIDATORS = {kind: Draft202012Validator(schema) for kind, schema in RECORD_SCHEMAS.items()}
EXPORT_SCHEMAS = {
    1: {
        **EXPORT_SCHEMA,
        "properties": {**EXPORT_SCHEMA["properties"], "format_version": {"const": 1, "type": "integer"}},
    },
    CURRENT_FORMAT_VERSION: EXPORT_SCHEMA,
}


def validate_version(value: object) -> int:
    if type(value) is not int or value not in SUPPORTED_FORMAT_VERSIONS:
        raise ValueError("Unsupported export format_version; expected integer 1 or 2")
    return value


def read_export_version(path: Path) -> int:
    with path.open("rb") as stream:
        for prefix, _event, value in ijson.parse(stream):
            if prefix == "format_version":
                return validate_version(value)
    raise ValueError("Missing export format_version")


def schema_for_version(version: int) -> Facts:
    return cast(Facts, EXPORT_SCHEMAS[validate_version(version)])


def validate_record(kind: str, record: Mapping[str, object]) -> None:
    if kind not in _VALIDATORS:
        raise ValueError("Unknown export record kind")
    error = next(_VALIDATORS[kind].iter_errors(record), None)
    if error is not None:
        location = "/".join(str(part) for part in error.absolute_path)
        raise ValueError(f"Invalid export record {kind}/{location}: {error.validator} constraint")


def migrate_record(version: int, kind: str, record: Mapping[str, object]) -> Facts:
    validate_version(version)
    validate_record(kind, record)
    result = deduplicate_export_message(record) if version == 1 and kind == "messages.item" else dict(record)
    validate_record(kind, result)
    return cast(Facts, result)
