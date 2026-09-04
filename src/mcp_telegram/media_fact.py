"""Canonical media facts and their safe storage/projection codecs."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast, get_args

type MediaKind = Literal[
    "photo",
    "video",
    "audio",
    "voice",
    "document",
    "animation",
    "sticker",
    "custom_emoji",
    "poll",
    "location",
    "venue",
    "contact",
    "link_preview",
    "game",
    "invoice",
    "dice",
    "story",
    "other",
]

# ``MediaKind`` is a PEP 695 type alias, so unwrap its value before asking
# ``typing`` for the ordered Literal arguments.  Keep this tuple as the one
# runtime/schema vocabulary source; the set below is only for membership tests.
MEDIA_KIND_VALUES: tuple[str, ...] = get_args(cast(object, MediaKind.__value__))
MEDIA_KINDS: frozenset[str] = frozenset(MEDIA_KIND_VALUES)


@dataclass(frozen=True, slots=True)
class MediaFact:
    """One normalized Telegram media fact."""

    kind: MediaKind
    payload: dict[str, object]


def is_transcribable_telegram_media(fact: MediaFact | None) -> bool:
    """Return whether a normalized Telegram media fact supports transcription.

    Voice messages are identified by their media kind.  Telegram round videos
    use the existing ``video`` kind and are admitted only when the canonical
    boolean marker is present.  Unknown, malformed, or non-Telegram-shaped
    values fail closed.
    """
    if fact is None or not isinstance(fact.payload, dict):
        return False
    try:
        _json_value(fact.payload)
    except TypeError, ValueError:
        return False
    if fact.kind == "voice":
        return True
    return fact.kind == "video" and fact.payload.get("round_message") is True


def _json_value(value: object) -> object:
    """Return a JSON-safe primitive/container, rejecting TL objects."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("media payload contains non-finite number")
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    raise TypeError(f"media payload contains unsupported value {type(value).__name__}")


def encode_media_payload(fact: MediaFact | None) -> str | None:
    """Encode a fact as canonical compact JSON; reject invalid writes."""
    if fact is None:
        return None
    if fact.kind not in MEDIA_KINDS:
        raise ValueError(f"unsupported media kind: {fact.kind!r}")
    if not isinstance(fact.payload, dict):
        raise TypeError("media payload must be a JSON object")
    payload = cast(dict[str, object], _json_value(fact.payload))
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def encode_media_fact(fact: MediaFact | None) -> tuple[str | None, str | None]:
    """Encode the database pair ``(media_kind, media_payload)`` strictly."""
    return (None, None) if fact is None else (fact.kind, encode_media_payload(fact))


def decode_media_fact(kind: object, payload: object) -> MediaFact | None:
    """Decode stored values safely, dropping malformed values.

    A valid discriminator with malformed payload retains the kind and receives
    an empty object.  This keeps the attachment visible without exposing
    untrusted/non-JSON data to callers.
    """
    if not isinstance(kind, str) or kind not in MEDIA_KINDS:
        return None
    if isinstance(payload, dict):
        try:
            normalized = cast(dict[str, object], _json_value(payload))
        except TypeError, ValueError:
            normalized = {}
        return MediaFact(cast(MediaKind, kind), normalized)
    if isinstance(payload, str):
        try:
            value = cast(object, json.loads(payload))
        except TypeError, ValueError, json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            try:
                return MediaFact(cast(MediaKind, kind), cast(dict[str, object], _json_value(value)))
            except TypeError, ValueError:
                pass
    return MediaFact(cast(MediaKind, kind), {})


def media_description(fact: MediaFact | None) -> str | None:
    """Project a normalized fact into the stable human-readable description."""
    if fact is None:
        return None
    return _describe_kind(fact.kind, fact.payload)


def _describe_kind(kind: MediaKind, payload: Mapping[str, object]) -> str | None:
    description: str | None
    if kind in {"video", "audio", "voice"}:
        description = _describe_duration_kind(kind, payload)
    elif kind in {"document", "sticker", "custom_emoji"}:
        description = _describe_document_kind(kind, payload)
    elif kind in {"poll", "location", "venue", "contact"}:
        description = _describe_place_kind(kind, payload)
    elif kind in {"link_preview", "game", "invoice", "dice"}:
        description = _describe_info_kind(kind, payload)
    elif kind == "photo":
        description = _describe_photo(payload)
    elif kind == "animation":
        description = "[анимация]"
    elif kind == "story":
        description = "[история]"
    else:
        description = "[медиа]"
    return description


def _describe_duration_kind(kind: MediaKind, payload: Mapping[str, object]) -> str:
    if kind == "video":
        return _describe_video(payload)
    if kind == "voice":
        return _duration_description("голосовое", payload)
    info = _join_present(payload, ("performer", "title"), " — ")
    return _duration_description(f"аудио: {info}" if info else "аудио", payload)


def _describe_video(payload: Mapping[str, object]) -> str:
    label = "кружок" if payload.get("round_message") is True else "видео"
    details: list[str] = []
    duration = payload.get("duration")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        minutes, seconds = divmod(int(duration), 60)
        details.append(f"{minutes}:{seconds:02d}")
    filename = payload.get("file_name")
    if isinstance(filename, str) and filename:
        compact_filename = _compact_filename(filename)
        if compact_filename:
            details.append(compact_filename)
    details.extend(_visual_flags(payload))
    return f"[{label}: {'; '.join(details)}]" if details else f"[{label}]"


def _describe_document_kind(kind: MediaKind, payload: Mapping[str, object]) -> str:
    if kind == "document":
        name = payload.get("file_name")
        return f"[документ: {name}]" if name else "[документ]"
    if kind == "custom_emoji":
        alt = payload.get("alt")
        return f"[кастомный эмодзи: {alt}]" if alt else "[кастомный эмодзи]"
    alt = payload.get("alt")
    set_name = payload.get("set_name")
    if alt and set_name:
        return f"[стикер: {alt}; набор {set_name}]"
    if alt:
        return f"[стикер: {alt}]"
    if set_name:
        return f"[стикер; набор {set_name}]"
    return "[стикер]"


def _describe_photo(payload: Mapping[str, object]) -> str:
    details: list[str] = []
    if payload.get("live_photo") is True:
        details.append("Live Photo")
    details.extend(_visual_flags(payload))
    return f"[фото: {'; '.join(details)}]" if details else "[фото]"


def _visual_flags(payload: Mapping[str, object]) -> list[str]:
    details: list[str] = []
    if payload.get("spoiler") is True:
        details.append("спойлер")
    ttl_seconds = payload.get("ttl_seconds")
    if isinstance(ttl_seconds, int) and not isinstance(ttl_seconds, bool) and ttl_seconds > 0:
        details.append("исчезающее")
    return details


def _compact_filename(value: str) -> str:
    """Keep an untrusted Telegram filename on one safe, compact line."""
    normalized = "".join(char if char.isprintable() and char not in "[]" else " " for char in value)
    return " ".join(normalized.split())


def _describe_place_kind(kind: MediaKind, payload: Mapping[str, object]) -> str | None:
    if kind == "poll":
        question = payload.get("question")
        return f"[опрос: «{question}»]" if question else "[опрос]"
    if kind == "location":
        return _describe_location(payload)
    if kind == "venue":
        info = _join_present(payload, ("title", "address"), ", ")
        return f"[место: {info}]" if info else "[место]"
    info = _join_present(payload, ("first_name", "last_name"), " ")
    phone = payload.get("phone_number")
    if phone:
        info = f"{info}, {phone}" if info else str(phone)
    return info or None


def _join_present(payload: Mapping[str, object], keys: tuple[str, ...], separator: str) -> str:
    return separator.join(str(payload[key]) for key in keys if payload.get(key))


def _describe_location(payload: Mapping[str, object]) -> str:
    lat, lon = payload.get("lat"), payload.get("long")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        return f"[геолокация: {lat:.4f}, {lon:.4f}]"
    return "[геолокация]"


def _describe_info_kind(kind: MediaKind, payload: Mapping[str, object]) -> str:
    if kind == "link_preview":
        return _describe_link_preview(payload)
    if kind == "game":
        value = payload.get("title")
        return f"[игра: {value}]" if value else "[игра]"
    if kind == "invoice":
        value = payload.get("title")
        return f"[счёт: {value}]" if value else "[счёт]"
    emoticon = payload.get("emoticon") or "🎲"
    value = payload.get("value")
    return f"[{emoticon} {value}]" if value is not None else f"[{emoticon}]"


def _describe_link_preview(payload: Mapping[str, object]) -> str:
    site_name = payload.get("site_name")
    title = payload.get("title")
    if site_name and title and site_name != title:
        return f"[ссылка: {site_name} — {title}]"
    summary = title or site_name or payload.get("url")
    return f"[ссылка: {summary}]" if summary else "[ссылка]"


def _duration_description(label: str, payload: Mapping[str, object]) -> str:
    duration = payload.get("duration")
    if not isinstance(duration, (int, float)):
        return f"[{label}]"
    minutes, seconds = divmod(int(duration), 60)
    return f"[{label}: {minutes}:{seconds:02d}]"


__all__ = [
    "MEDIA_KINDS",
    "MEDIA_KIND_VALUES",
    "MediaFact",
    "MediaKind",
    "decode_media_fact",
    "encode_media_fact",
    "encode_media_payload",
    "is_transcribable_telegram_media",
    "media_description",
]
