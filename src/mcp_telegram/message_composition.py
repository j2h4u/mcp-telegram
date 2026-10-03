"""JSON composition facts for Telegram formatting and service actions."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import cast


def normalize_telegram_fact(value: object) -> object:
    """Normalize materialized Telegram facts without importing Telegram clients."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (list, tuple)):
        return [normalize_telegram_fact(item) for item in value]
    if isinstance(value, dict):
        return _normalize_fact_dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return normalize_telegram_fact(to_dict())
    raise TypeError(f"Unsupported Telegram fact: {type(value).__name__}")


def _normalize_fact_dict(value: dict[object, object]) -> dict[str, object]:
    if not all(isinstance(key, str) for key in value):
        raise TypeError("Telegram fact object keys must be strings")
    if value.get("_") in {"PhotoCachedSize", "PhotoStrippedSize"} and isinstance(value.get("bytes"), bytes):
        value = {
            **{key: item for key, item in value.items() if key != "bytes"},
            "bytes_omitted": True,
            "bytes_length": len(cast(bytes, value["bytes"])),
        }
    return {cast(str, key): normalize_telegram_fact(item) for key, item in value.items()}


def _encode(value: object) -> str:
    return json.dumps(
        normalize_telegram_fact(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _fact_attr(message: object, name: str) -> object:
    # Test doubles may synthesize arbitrary attributes that are not Telegram facts.
    if type(message).__module__.startswith("unittest.mock"):
        return vars(message).get(name)
    return getattr(message, name, None)


def extract_message_composition(message: object) -> tuple[str | None, str | None]:
    """Capture all formatting entities and the typed service action on observation."""
    entities = _fact_attr(message, "entities")
    action = _fact_attr(message, "action")
    return _encode(entities or []), _encode(action) if action is not None else None


def decode_formatting_entities(payload: str | None) -> list[dict[str, object]] | None:
    """Return observed entities, preserving NULL as unknown historical coverage."""
    if payload is None:
        return None
    value = cast(object, json.loads(payload))
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("formatting_entities must contain an array of entity objects")
    return cast(list[dict[str, object]], value)


def decode_service_action(payload: str | None) -> dict[str, object] | None:
    """Return the full typed service payload, or no recorded action."""
    if payload is None:
        return None
    value = cast(object, json.loads(payload))
    if not isinstance(value, dict):
        raise ValueError("service_action must contain an action object")
    return cast(dict[str, object], value)
