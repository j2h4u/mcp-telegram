"""Closed wire schema for the finite, normalized personal-profile manifest."""

from collections.abc import Mapping
from copy import deepcopy
from typing import cast

from ..structured import TELEGRAM_CONTENT_OUTPUT_SCHEMA, TelegramContentKind, telegram_content
from .contracts import ProfileFieldClass
from .field_manifest import PROFILE_FIELD_MANIFEST, ProfileConstructorRule, ProfileFieldRule

_PRIMITIVES: dict[str, dict[str, object]] = {
    "bool": {"type": "boolean"},
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "str": {"$ref": "#/$defs/telegram_content"},
    "datetime": {"type": "string", "description": "Source-provided ISO date or datetime."},
    "date": {"type": "string", "description": "Source-provided ISO date or datetime."},
    "bytes": {"$ref": "#/$defs/profile_bytes"},
}


def _retained_rules(constructor: ProfileConstructorRule) -> dict[str, ProfileFieldRule]:
    return {
        name: rule
        for name, rule in constructor.fields.items()
        if rule.classification is not ProfileFieldClass.CAPABILITY
    }


def _atom_schema(name: str) -> dict[str, object] | None:
    if name in _PRIMITIVES:
        return _PRIMITIVES[name]
    constructor = PROFILE_FIELD_MANIFEST.get(name)
    if constructor is None or constructor.protocol_only:
        return None
    return {"$ref": f"#/$defs/{name}"}


def _union(choices: list[dict[str, object]]) -> dict[str, object]:
    return choices[0] if len(choices) == 1 else {"anyOf": choices}


def _field_schema(rule: ProfileFieldRule) -> dict[str, object] | None:
    choices = [schema for name in rule.value_types if (schema := _atom_schema(name)) is not None]
    if rule.vector:
        # Unknown vector elements retain their positions as null placeholders.
        choices = [{"type": "array", "items": _union([*choices, {"type": "null"}])}]
    if rule.clear_on_none:
        choices.append({"type": "null"})
    return _union(choices) if choices else None


def _properties(rules: Mapping[str, ProfileFieldRule]) -> dict[str, object]:
    return {name: schema for name, rule in rules.items() if (schema := _field_schema(rule)) is not None}


def _constructor_schema(name: str) -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "constructor": {"const": name},
            **_properties(_retained_rules(PROFILE_FIELD_MANIFEST[name])),
        },
        "required": ["constructor"],
        "additionalProperties": False,
    }


def _reachable_constructors() -> list[str]:
    pending = ["User", "UserFull"]
    visited: set[str] = set()
    while pending:
        name = pending.pop()
        constructor = PROFILE_FIELD_MANIFEST.get(name)
        if name in visited or constructor is None or constructor.protocol_only:
            continue
        visited.add(name)
        for rule in _retained_rules(constructor).values():
            pending.extend(child for child in rule.value_types if child in PROFILE_FIELD_MANIFEST)
    return sorted(visited)


def _root_rules() -> dict[str, ProfileFieldRule]:
    return {
        f"{prefix}.{name}": rule
        for prefix, constructor in (("user", "User"), ("full_user", "UserFull"))
        for name, rule in _retained_rules(PROFILE_FIELD_MANIFEST[constructor]).items()
    }


def build_profile_fields_output_schema() -> dict[str, object]:
    """Build once per constructor; references preserve recursive TL shapes finitely.

    When embedded in another schema, hoist these definitions to that schema's
    root: JSON Schema references resolve against the whole document.
    """
    content = deepcopy(TELEGRAM_CONTENT_OUTPUT_SCHEMA)
    cast(dict[str, object], content["properties"])["is_telegram_content"] = {"const": True}
    return {
        "type": "object",
        "properties": _properties(_root_rules()),
        "additionalProperties": False,
        "$defs": {
            "telegram_content": content,
            "profile_bytes": {
                "type": "object",
                "properties": {
                    "encoding": {"const": "base64"},
                    "data": {"type": "string", "contentEncoding": "base64"},
                },
                "required": ["encoding", "data"],
                "additionalProperties": False,
            },
            **{name: _constructor_schema(name) for name in _reachable_constructors()},
        },
    }


_CONTENT_KINDS: dict[str, TelegramContentKind] = {
    "full_user.about": "about",
    "full_user.note": "note",
    "full_user.private_forward_name": "private_forward_name",
    "full_user.business_intro": "business_intro",
    "full_user.business_location": "business_location",
}


def _project_atom(value: object, rule: ProfileFieldRule, kind: TelegramContentKind) -> object:
    if isinstance(value, str) and "str" in rule.value_types:
        return telegram_content(value, kind)
    if not isinstance(value, dict):
        return value
    node = cast(dict[str, object], value)
    if "bytes" in rule.value_types:
        return {"encoding": node["encoding"], "data": node["data"]}
    name = node.get("constructor")
    if not isinstance(name, str) or name not in rule.value_types:
        raise ValueError("Unexpected normalized profile constructor")
    constructor = PROFILE_FIELD_MANIFEST[name]
    if constructor.protocol_only:
        raise ValueError("Protocol-only profile constructor")
    return {"constructor": name, **_project_properties(node, _retained_rules(constructor), kind)}


def _project_field(value: object, rule: ProfileFieldRule, kind: TelegramContentKind) -> object:
    if rule.vector and isinstance(value, list):
        return [_project_atom(item, rule, kind) for item in cast(list[object], value)]
    return _project_atom(value, rule, kind)


def _project_properties(
    values: Mapping[str, object], rules: Mapping[str, ProfileFieldRule], kind: TelegramContentKind
) -> dict[str, object]:
    return {name: _project_field(values[name], rule, kind) for name, rule in rules.items() if name in values}


def project_profile_fields(fields: Mapping[str, object]) -> dict[str, object]:
    """Mark semantic text as untrusted without changing retained values or positions."""
    return {
        name: _project_field(fields[name], rule, _CONTENT_KINDS.get(name, "snippet"))
        for name, rule in _root_rules().items()
        if name in fields
    }
