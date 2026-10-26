"""Closed wire schema for retained facts from the finite personal-profile manifest."""

from collections.abc import Mapping
from copy import deepcopy
from typing import cast

from ..structured import TELEGRAM_CONTENT_OUTPUT_SCHEMA, TelegramContentKind, telegram_content
from .contracts import ProfileFieldClass
from .field_manifest import PROFILE_FIELD_MANIFEST, ProfileFieldRule

_PRIMITIVES: dict[str, dict[str, object]] = {
    "bool": {"type": "boolean"},
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "str": {"$ref": "#/$defs/telegram_content"},
    "datetime": {"type": "string", "description": "Source-provided ISO date or datetime."},
    "date": {"type": "string", "description": "Source-provided ISO date or datetime."},
    "bytes": {"$ref": "#/$defs/profile_bytes"},
}


def _retained_rules(name: str) -> dict[str, ProfileFieldRule]:
    fields = PROFILE_FIELD_MANIFEST[name].fields
    if name in {"StoryItem", "StoryItemSkipped"}:
        # A skipped observation does not erase older story details.
        fields = {**PROFILE_FIELD_MANIFEST["StoryItem"].fields, **PROFILE_FIELD_MANIFEST["StoryItemSkipped"].fields}
    return {name: rule for name, rule in fields.items() if rule.classification is not ProfileFieldClass.CAPABILITY}


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
    if not choices and not rule.vector:
        return None
    if rule.vector:
        # Unknown vector elements retain their positions as null placeholders.
        choices = [{"type": "array", "items": _union([*choices, {"type": "null"}])}]
    # Acquisition omission rules do not revoke accepted canonical clears.
    return _union([*choices, {"type": "null"}])


def _properties(rules: Mapping[str, ProfileFieldRule]) -> dict[str, object]:
    return {name: schema for name, rule in rules.items() if (schema := _field_schema(rule)) is not None}


def _constructor_schema(name: str) -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "constructor": {"const": name},
            **_properties(_retained_rules(name)),
        },
        "required": ["constructor"],
        "additionalProperties": False,
    }


def _reachable_constructors() -> list[str]:
    pending = ["User", "UserFull", "SavedStarGift", "Chat", "ChatForbidden", "ChatEmpty", "Channel", "ChannelForbidden"]
    visited: set[str] = set()
    while pending:
        name = pending.pop()
        constructor = PROFILE_FIELD_MANIFEST.get(name)
        if name in visited or constructor is None or constructor.protocol_only:
            continue
        visited.add(name)
        for rule in _retained_rules(name).values():
            pending.extend(child for child in rule.value_types if child in PROFILE_FIELD_MANIFEST)
    return sorted(visited)


def _root_rules() -> dict[str, ProfileFieldRule]:
    return {
        f"{prefix}.{name}": rule
        for prefix, constructor in (("user", "User"), ("full_user", "UserFull"))
        for name, rule in _retained_rules(constructor).items()
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
    return {"constructor": name, **_project_properties(node, _retained_rules(name), kind)}


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


COLLECTION_CONSTRUCTORS: dict[str, tuple[str, ...]] = {
    "common_chats": ("Chat", "ChatForbidden", "ChatEmpty", "Channel", "ChannelForbidden"),
    "avatar_history": ("Photo", "PhotoEmpty"),
    "saved_music": ("Document", "DocumentEmpty"),
    "saved_gifts": ("SavedStarGift",),
    "current_stories": ("StoryItem", "StoryItemSkipped", "StoryItemDeleted"),
    "pinned_stories": ("StoryItem", "StoryItemSkipped", "StoryItemDeleted"),
}


def collection_item_schema(section: str) -> dict[str, object]:
    canonical = cast(dict[str, object], _field_schema(ProfileFieldRule(COLLECTION_CONSTRUCTORS[section])))
    legacy = {"avatar_history": _LEGACY_PHOTO_SCHEMA, "common_chats": _LEGACY_CHAT_SCHEMA}.get(section)
    return {"anyOf": [canonical, legacy]} if legacy is not None else canonical


def project_collection_item(section: str, fields: Mapping[str, object]) -> object:
    return _project_atom(dict(fields), ProfileFieldRule(COLLECTION_CONSTRUCTORS[section]), "snippet")


def history_value_schema(change_key: str) -> dict[str, object]:
    rule = _root_rules().get(change_key)
    if rule is not None:
        return cast(dict[str, object], _field_schema(rule))
    if change_key in {"legacy.name", "legacy.username", "owner.saved_contact_name"}:
        return cast(dict[str, object], _field_schema(ProfileFieldRule(("str",))))
    if change_key.startswith("photo.") and change_key[6:] in {"current", "public", "personal", "fallback"}:
        return {"anyOf": [cast(dict[str, object], _field_schema(_PHOTO_RULE)), _LEGACY_PHOTO_SCHEMA]}
    if change_key.startswith("collection.") and change_key.split(".", 2)[1] in COLLECTION_CONSTRUCTORS:
        return {"type": ["string", "null"]}
    raise ValueError("Unsupported retained history key")


_PHOTO_RULE = ProfileFieldRule(("UserProfilePhoto", "UserProfilePhotoEmpty", "Photo", "PhotoEmpty"))
_LEGACY_PHOTO_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "photo_id": {"type": "integer"},
        "date": {"type": ["string", "null"]},
        "personal": {"type": "boolean"},
    },
    "required": ["photo_id"],
    "additionalProperties": False,
}


_LEGACY_CHAT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"},
        "name": {"anyOf": [{"$ref": "#/$defs/telegram_content"}, {"type": "null"}]},
        "username": {"anyOf": [{"$ref": "#/$defs/telegram_content"}, {"type": "null"}]},
        "type": {"type": ["string", "null"]},
    },
    "required": ["id"],
    "additionalProperties": False,
}


def project_legacy_collection_item(section: str, value: object) -> object:
    if not isinstance(value, dict):
        raise ValueError("Unsupported legacy collection value")
    if section == "avatar_history" and type(value.get("photo_id")) is int:
        return {key: value[key] for key in ("photo_id", "date") if key in value}
    if section == "common_chats" and type(value.get("id")) is int:
        return _project_legacy_chat(value)
    raise ValueError("Unsupported legacy collection value")


def _project_legacy_chat(value: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {key: value[key] for key in ("id", "type") if key in value}
    for key in ("name", "username"):
        if key in value:
            text = value[key]
            result[key] = telegram_content(text, "snippet") if isinstance(text, str) else None
    return result


def project_history_value(change_key: str, value: object) -> object:
    history_value_schema(change_key)
    if change_key.startswith("collection."):
        return value
    if change_key.startswith("photo."):
        if isinstance(value, dict) and "constructor" not in value:
            if set(value) - {"photo_id", "date", "personal"}:
                raise ValueError("Unsupported legacy photo evidence")
            return dict(value)
        return _project_atom(value, _PHOTO_RULE, "snippet")
    rule = _root_rules().get(change_key, ProfileFieldRule(("str",)))
    return _project_field(value, rule, _CONTENT_KINDS.get(change_key, "snippet"))
