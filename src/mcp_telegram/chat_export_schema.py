"""Fixed export schemas and deterministic export migrations."""

from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import cast

import ijson  # type: ignore[import-untyped]
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import ValidationError  # type: ignore[import-untyped]

from .chat_export_projection import compact_export_record, deduplicate_export_message, omit_empty_fields

INTERNAL_FORMAT_VERSION = 3
IDENTITY_FORMAT_VERSION = 4
SPARSE_FORMAT_VERSION = 5
CURRENT_FORMAT_VERSION = SPARSE_FORMAT_VERSION
SUPPORTED_FORMAT_VERSIONS = (1, 2, INTERNAL_FORMAT_VERSION, IDENTITY_FORMAT_VERSION, CURRENT_FORMAT_VERSION)
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


_GROUP: Facts = {"type": "object", "required": ["dialog_id"], "properties": {"dialog_id": _DIALOG_ID}}
_REACTOR = {
    "type": "object",
    "properties": {
        **_identity_fields("actor_"),
        "reaction": {"type": ["object", "string", "null"]},
        "date": _STRING,
        "raw": _OBJECT,
    },
}
LEGACY_V1_RECORD_SCHEMAS: dict[str, Facts] = {
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
LEGACY_V2_RECORD_SCHEMAS = LEGACY_V1_RECORD_SCHEMAS
V3_RECORD_SCHEMAS: dict[str, Facts] = {
    **LEGACY_V1_RECORD_SCHEMAS,
    "messages.item": {
        **LEGACY_V1_RECORD_SCHEMAS["messages.item"],
        "required": ["dialog_id", "message_id", "reactors"],
        "properties": {
            k: v
            for k, v in cast(Facts, LEGACY_V1_RECORD_SCHEMAS["messages.item"]["properties"]).items()
            if k not in {"message_key", "reply_key"}
        },
    },
}
_IDENTITY: Facts = {
    "type": "object",
    "properties": {
        "id": _NULLABLE_ID,
        "kind": _STRING,
        "name": _STRING,
        "username": _STRING,
        "is_admin": {"type": ["boolean", "null"]},
        "role": _STRING,
        "rank": _STRING,
        "metadata": {"type": "object"},
    },
}
_RECENT_REACTOR_REF = {
    "type": "object",
    "required": ["reactor"],
    "properties": {"reactor": {"type": "integer", "minimum": 0}},
    "additionalProperties": False,
}
_RAW_RECENT_REACTION = {"type": "object", "not": {"required": ["reactor"]}}


def _without_identity_fields(prefix: str) -> Facts:
    return {"patternProperties": {f"^{prefix}": False}}


V4_RECORD_SCHEMAS: dict[str, Facts] = {
    **V3_RECORD_SCHEMAS,
    "identities.item": _IDENTITY,
    "messages.item": {
        **V3_RECORD_SCHEMAS["messages.item"],
        "properties": {
            **{
                k: v
                for k, v in cast(Facts, V3_RECORD_SCHEMAS["messages.item"]["properties"]).items()
                if not k.startswith("author_") and k != "related_users"
            },
            "author": {"type": "integer", "minimum": 0},
            "related_users": {"type": "array", "items": {"type": "integer", "minimum": 0}},
            "reactors": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "actor": {"type": "integer", "minimum": 0},
                        "reaction": {"type": ["object", "string", "null"]},
                        "date": _STRING,
                        "raw": _OBJECT,
                    },
                    **_without_identity_fields("actor_"),
                },
            },
            "reactions": {
                "type": "object",
                "properties": {
                    "aggregate": {
                        "type": ["object", "null"],
                        "properties": {
                            "recent_reactions": {
                                "type": "array",
                                "items": {"oneOf": [_RAW_RECENT_REACTION, _RECENT_REACTOR_REF]},
                            },
                        },
                    },
                    "can_view_list": {"type": "boolean"},
                    "recent_reactions": {
                        "type": "array",
                        "items": {"oneOf": [_RAW_RECENT_REACTION, _RECENT_REACTOR_REF]},
                    },
                },
            },
        },
        **_without_identity_fields("author_"),
    },
    "admin_events.item": {
        **V3_RECORD_SCHEMAS["admin_events.item"],
        "properties": {
            **{
                k: v
                for k, v in cast(Facts, V3_RECORD_SCHEMAS["admin_events.item"]["properties"]).items()
                if not k.startswith("actor_") and k != "related_users"
            },
            "actor": {"type": "integer", "minimum": 0},
            "related_users": {"type": "array", "items": {"type": "integer", "minimum": 0}},
        },
        **_without_identity_fields("actor_"),
    },
}
V5_RECORD_SCHEMAS: dict[str, Facts] = {
    **V4_RECORD_SCHEMAS,
    "messages.item": {
        **V4_RECORD_SCHEMAS["messages.item"],
        "required": [
            key for key in cast(list[str], V4_RECORD_SCHEMAS["messages.item"]["required"]) if key != "reactors"
        ],
    },
}
RECORD_SCHEMAS = V5_RECORD_SCHEMAS
EXPORT_SCHEMA: Facts = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["format_version", "group", "metadata", "identities", "admin_events", "messages", "export"],
    "additionalProperties": False,
    "properties": {
        "format_version": {"const": CURRENT_FORMAT_VERSION, "type": "integer"},
        "group": RECORD_SCHEMAS["group"],
        "metadata": RECORD_SCHEMAS["metadata"],
        "identities": {"type": "array", "items": RECORD_SCHEMAS["identities.item"]},
        "admin_events": {"type": "array", "items": RECORD_SCHEMAS["admin_events.item"]},
        "messages": {"type": "array", "items": RECORD_SCHEMAS["messages.item"]},
        "export": RECORD_SCHEMAS["export"],
    },
}
Draft202012Validator.check_schema(EXPORT_SCHEMA)
V4_EXPORT_SCHEMA: Facts = {
    **EXPORT_SCHEMA,
    "properties": {
        **cast(Facts, EXPORT_SCHEMA["properties"]),
        "format_version": {"const": 4, "type": "integer"},
        "group": V4_RECORD_SCHEMAS["group"],
        "metadata": V4_RECORD_SCHEMAS["metadata"],
        "identities": {"type": "array", "items": V4_RECORD_SCHEMAS["identities.item"]},
        "admin_events": {"type": "array", "items": V4_RECORD_SCHEMAS["admin_events.item"]},
        "messages": {"type": "array", "items": V4_RECORD_SCHEMAS["messages.item"]},
        "export": V4_RECORD_SCHEMAS["export"],
    },
}
Draft202012Validator.check_schema(V4_EXPORT_SCHEMA)
_VALIDATORS = {
    version: {kind: Draft202012Validator(schema) for kind, schema in schemas.items()}
    for version, schemas in (
        (1, LEGACY_V1_RECORD_SCHEMAS),
        (2, LEGACY_V2_RECORD_SCHEMAS),
        (INTERNAL_FORMAT_VERSION, V3_RECORD_SCHEMAS),
        (IDENTITY_FORMAT_VERSION, V4_RECORD_SCHEMAS),
        (CURRENT_FORMAT_VERSION, V5_RECORD_SCHEMAS),
    )
}
EXPORT_SCHEMAS: dict[int, Facts] = {
    1: {
        **EXPORT_SCHEMA,
        "required": ["format_version", "group", "metadata", "admin_events", "messages", "export"],
        "properties": {
            **cast(Facts, EXPORT_SCHEMA["properties"]),
            "identities": False,
            "format_version": {"const": 1, "type": "integer"},
            "messages": {"type": "array", "items": LEGACY_V1_RECORD_SCHEMAS["messages.item"]},
            "admin_events": {"type": "array", "items": LEGACY_V1_RECORD_SCHEMAS["admin_events.item"]},
        },
    },
    2: {
        **EXPORT_SCHEMA,
        "required": ["format_version", "group", "metadata", "admin_events", "messages", "export"],
        "properties": {
            **cast(Facts, EXPORT_SCHEMA["properties"]),
            "identities": False,
            "format_version": {"const": 2, "type": "integer"},
            "messages": {"type": "array", "items": LEGACY_V2_RECORD_SCHEMAS["messages.item"]},
            "admin_events": {"type": "array", "items": LEGACY_V2_RECORD_SCHEMAS["admin_events.item"]},
        },
    },
    INTERNAL_FORMAT_VERSION: {
        **EXPORT_SCHEMA,
        "required": ["format_version", "group", "metadata", "admin_events", "messages", "export"],
        "properties": {
            **{k: v for k, v in cast(Facts, EXPORT_SCHEMA["properties"]).items() if k != "identities"},
            "identities": False,
            "format_version": {"const": INTERNAL_FORMAT_VERSION, "type": "integer"},
            "messages": {"type": "array", "items": V3_RECORD_SCHEMAS["messages.item"]},
            "admin_events": {"type": "array", "items": V3_RECORD_SCHEMAS["admin_events.item"]},
        },
    },
    IDENTITY_FORMAT_VERSION: V4_EXPORT_SCHEMA,
    CURRENT_FORMAT_VERSION: EXPORT_SCHEMA,
}


def validate_version(value: object) -> int:
    if type(value) is not int or value not in SUPPORTED_FORMAT_VERSIONS:
        raise ValueError("Unsupported export format_version; expected integer 1, 2, 3, 4, or 5")
    return value


def read_export_version(path: Path) -> int:
    with path.open("rb") as stream:
        events = cast(Iterator[tuple[str, str, object]], ijson.parse(stream))  # pyright: ignore[reportAny]
        for prefix, _event, value in events:
            if prefix == "format_version":
                return validate_version(value)
    raise ValueError("Missing export format_version")


def schema_for_version(version: int) -> Facts:
    return EXPORT_SCHEMAS[validate_version(version)]


def validate_record(
    kind: str, record: Mapping[str, object], version: int | None = None, *, internal: bool = False
) -> None:
    version = INTERNAL_FORMAT_VERSION if internal else CURRENT_FORMAT_VERSION if version is None else version
    version = validate_version(version)
    if kind not in _VALIDATORS[version]:
        raise ValueError("Unknown export record kind")
    # jsonschema validates arbitrary Python objects; its stub restricts inputs to JSON values.
    iter_errors = cast(Callable[[object], Iterator[ValidationError]], _VALIDATORS[version][kind].iter_errors)
    error = next(iter_errors(record), None)
    if error is not None:
        path = cast(Iterable[object], error.absolute_path)  # pyright: ignore[reportAny]
        constraint = cast(object, error.validator)  # pyright: ignore[reportAny]
        location = "/".join(str(part) for part in path)
        raise ValueError(f"Invalid export record {kind}/{location}: {constraint} constraint")
    if version == SPARSE_FORMAT_VERSION and omit_empty_fields(record) != dict(record):
        raise ValueError(f"Invalid export record {kind}: fields must use canonical sparse form")


def migrate_record(version: int, kind: str, record: Mapping[str, object], *, internal: bool = False) -> Facts:
    validate_version(version)
    if version >= IDENTITY_FORMAT_VERSION:
        if not internal:
            raise ValueError(f"Version {version} records must be expanded through the identity directory")
        validate_record(kind, record, internal=True)
        return dict(record)
    validate_record(kind, record, version)
    result = dict(record)
    if version == 1 and kind == "messages.item":
        result = deduplicate_export_message(result)
    if version < SPARSE_FORMAT_VERSION and kind in {"messages.item", "admin_events.item"}:
        result = compact_export_record(kind, result)
    validate_record(kind, result, internal=True)
    return cast(Facts, result)
