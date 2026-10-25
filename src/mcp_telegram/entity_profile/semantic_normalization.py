"""Interpret the finite profile manifest without transport objects or acquisition."""

import math
from base64 import b64encode
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import cast

from .contracts import (
    ObservationBoundary,
    ProfileFieldClass,
    ProfileFieldCoverage,
    ProfileFieldState,
    ProfileSemanticSnapshot,
)
from .field_manifest import PROFILE_FIELD_MANIFEST, ProfileConstructorRule, ProfileFieldRule

_MISSING = object()
_EMPTY_CONSTRUCTORS = frozenset({"UserProfilePhotoEmpty", "EmojiStatusEmpty"})
_NONRETURN_CONSTRUCTORS = frozenset({"UserStatusEmpty", "PhotoEmpty"})


def _read(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value).get(name, _MISSING)
    return cast(object, getattr(value, name, _MISSING))


def _constructor_name(value: object) -> str:
    if isinstance(value, Mapping):
        return str(cast(Mapping[str, object], value).get("_", "unknown"))
    cls = type(value)
    module = cls.__module__.removeprefix("telethon.tl.types").strip(".")
    return f"{module}.{cls.__name__}" if cls.__module__.startswith("telethon.tl.types.") else cls.__name__


def _attributes(value: object) -> frozenset[str]:
    values: object = value if isinstance(value, Mapping) else getattr(value, "__dict__", {})
    if not isinstance(values, Mapping):
        return frozenset()
    return frozenset(key for key in values if isinstance(key, str) and not key.startswith("_"))


def _primitive(value: object, types: tuple[str, ...]) -> object:
    if type(value) in (bool, int, str):
        compatible = type(value).__name__ in types or (type(value) is int and "float" in types)
        return value if compatible else _MISSING
    return _structured_primitive(value, types)


def _structured_primitive(value: object, types: tuple[str, ...]) -> object:
    if isinstance(value, float):
        return value if "float" in types and math.isfinite(value) else _MISSING
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": b64encode(value).decode("ascii")} if "bytes" in types else _MISSING
    if isinstance(value, (datetime, date)):
        return value.isoformat() if "datetime" in types or "date" in types else _MISSING
    return _MISSING


@dataclass
class _SemanticNormalizer:
    coverage: dict[str, ProfileFieldCoverage] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)
    active: set[int] = field(default_factory=set)

    def unknown(self, path: str, classification: ProfileFieldClass, reason: str) -> object:
        self.coverage[path] = ProfileFieldCoverage(classification, ProfileFieldState.UNKNOWN, reason=reason)
        self.diagnostics.append(f"{path}:{reason}")
        return _MISSING

    def absent(self, path: str, rule: ProfileFieldRule, *, authoritative: bool) -> object:
        if rule.flag and authoritative:
            return False
        if rule.clear_on_none and authoritative:
            self.coverage[path] = ProfileFieldCoverage(rule.classification, ProfileFieldState.CLEARED, True)
            return None
        if not rule.optional:
            return self.unknown(path, rule.classification, "required_value_missing")
        self.coverage[path] = ProfileFieldCoverage(
            rule.classification, ProfileFieldState.API_NOT_RETURNED, reason="endpoint_did_not_return_value"
        )
        return _MISSING

    def atom(self, value: object, rule: ProfileFieldRule, path: str, *, authoritative: bool) -> object:
        primitive = _primitive(value, rule.value_types)
        if primitive is not _MISSING:
            return primitive
        name = _constructor_name(value)
        if name not in rule.value_types:
            return self.unknown(path, rule.classification, "unexpected_constructor_or_value_type")
        return self.node(value, name, path, authoritative=authoritative, inherited=rule.classification)

    def vector(self, value: object, rule: ProfileFieldRule, path: str, *, authoritative: bool) -> object:
        if not isinstance(value, (list, tuple)):
            return self.unknown(path, rule.classification, "invalid_vector")
        output: list[object] = []
        for index, item in enumerate(cast(list[object] | tuple[object, ...], value)):
            normalized = self.atom(item, rule, f"{path}[{index}]", authoritative=authoritative)
            if normalized is _MISSING:
                output.append(None)
            else:
                output.append(normalized)
        return output

    def field_value(self, value: object, rule: ProfileFieldRule, path: str, *, authoritative: bool) -> object:
        if rule.classification is ProfileFieldClass.CAPABILITY:
            self.coverage[path] = ProfileFieldCoverage(
                rule.classification, ProfileFieldState.INAPPLICABLE, reason="transport_capability_excluded"
            )
            return _MISSING
        if value is _MISSING:
            return self.unknown(path, rule.classification, "field_missing")
        if value is None:
            value = self.absent(path, rule, authoritative=authoritative)
            if value is _MISSING or value is None:
                return value
        before = len(self.diagnostics)
        normalized = (
            self.vector(value, rule, path, authoritative=authoritative)
            if rule.vector
            else self.atom(value, rule, path, authoritative=authoritative)
        )
        if normalized is _MISSING:
            return normalized
        return self.record_value(
            value, normalized, rule, path, authoritative=authoritative and len(self.diagnostics) == before
        )

    def record_value(
        self, value: object, normalized: object, rule: ProfileFieldRule, path: str, *, authoritative: bool
    ) -> object:
        if _constructor_name(value) in _NONRETURN_CONSTRUCTORS:
            self.coverage[path] = ProfileFieldCoverage(
                rule.classification, ProfileFieldState.API_NOT_RETURNED, reason="empty_value_not_returned"
            )
            return normalized
        known_empty = _constructor_name(value) in _EMPTY_CONSTRUCTORS
        state = (
            ProfileFieldState.CLEARED
            if known_empty and rule.clear_on_none and authoritative
            else ProfileFieldState.PRESENT
        )
        self.coverage[path] = ProfileFieldCoverage(
            rule.classification,
            state,
            self.subtree_authority(path, authoritative),
            "explicit_empty_constructor" if state is ProfileFieldState.CLEARED else None,
        )
        return normalized

    def subtree_authority(self, path: str, authoritative: bool) -> bool:
        return authoritative and all(
            coverage.authoritative
            for key, coverage in self.coverage.items()
            if key.startswith((f"{path}.", f"{path}[")) and coverage.classification is not ProfileFieldClass.CAPABILITY
        )

    def fields(
        self,
        value: object,
        schema: ProfileConstructorRule,
        path: str,
        *,
        authoritative: bool,
        inherited: ProfileFieldClass,
    ) -> dict[str, object]:
        output: dict[str, object] = {}
        for name, original_rule in schema.fields.items():
            rule = original_rule
            if inherited is ProfileFieldClass.PRIVATE_FACT and rule.classification is ProfileFieldClass.PROFILE:
                rule = ProfileFieldRule(
                    rule.value_types,
                    ProfileFieldClass.PRIVATE_FACT,
                    rule.optional,
                    rule.vector,
                    rule.flag,
                    rule.clear_on_none,
                )
            normalized = self.field_value(_read(value, name), rule, f"{path}.{name}", authoritative=authoritative)
            if normalized is not _MISSING:
                output[name] = normalized
        for name in sorted(_attributes(value) - schema.fields.keys()):
            self.unknown(f"{path}.{name}", inherited, "unknown_field")
        return output

    def node(
        self,
        value: object,
        name: str,
        path: str,
        *,
        authoritative: bool,
        inherited: ProfileFieldClass = ProfileFieldClass.PROFILE,
    ) -> object:
        schema = PROFILE_FIELD_MANIFEST.get(name)
        if schema is None:
            return self.unknown(path, inherited, "unknown_constructor")
        if schema.protocol_only:
            self.coverage[path] = ProfileFieldCoverage(
                ProfileFieldClass.INAPPLICABLE, ProfileFieldState.INAPPLICABLE, reason="protocol_only_constructor"
            )
            self.diagnostics.append(f"{path}:protocol_only_constructor")
            return _MISSING
        if id(value) in self.active:
            return self.unknown(path, inherited, "constructor_cycle")
        actual_name = _constructor_name(value)
        concrete = actual_name == name and _read(value, "CONSTRUCTOR_ID") == schema.constructor_id
        if not concrete:
            self.diagnostics.append(f"{path}:constructor_identity_unverified")
        self.active.add(id(value))
        try:
            return {
                "constructor": name,
                **self.fields(value, schema, path, authoritative=authoritative and concrete, inherited=inherited),
            }
        finally:
            self.active.remove(id(value))


def normalize_semantic_profile(
    user: object,
    full_user: object,
    *,
    target_id: int,
    owner_account_id: int | None,
    observation: ObservationBoundary,
) -> ProfileSemanticSnapshot:
    """Normalize returned values only; the caller supplies authenticated ownership."""
    full_authority = _validate_target(user, full_user, target_id)
    normalizer = _SemanticNormalizer()
    minimal = _minimal_user(user, normalizer)
    user_type = "UserEmpty" if _constructor_name(user) == "UserEmpty" else "User"
    user_node = normalizer.node(user, user_type, "user", authoritative=not minimal)
    full_node = normalizer.node(full_user, "UserFull", "full_user", authoritative=full_authority)
    if minimal or user_type == "UserEmpty":
        normalizer.diagnostics.append("user:partial_user")
    if owner_account_id is None:
        normalizer.diagnostics.append("owner:unknown")
    fields: dict[str, object] = {}
    for prefix, node in (("user", user_node), ("full_user", full_node)):
        if isinstance(node, dict):
            fields.update({f"{prefix}.{key}": value for key, value in node.items() if key != "constructor"})
    return ProfileSemanticSnapshot(
        target_id, owner_account_id, fields, normalizer.coverage, observation, tuple(normalizer.diagnostics)
    )


def _minimal_user(user: object, normalizer: _SemanticNormalizer) -> bool:
    if _constructor_name(user) == "UserEmpty":
        return True
    raw = _read(user, "min")
    if raw is None or isinstance(raw, bool):
        return raw is True
    normalizer.diagnostics.append("user:min_flag_unknown")
    return True


def _validate_target(user: object, full_user: object, target_id: int) -> bool:
    user_id = _read(user, "id")
    if type(user_id) is not int or user_id != target_id:
        raise ValueError("semantic user does not match requested target")
    full_id = _read(full_user, "id")
    if full_id is _MISSING:
        return False
    if type(full_id) is not int or full_id != target_id:
        raise ValueError("semantic full user does not match requested target")
    return True
