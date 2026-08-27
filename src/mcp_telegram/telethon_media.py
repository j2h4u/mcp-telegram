"""Telethon adapter for extracting normalized media facts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

import telethon.tl.types as tl  # type: ignore[import-untyped]

from .media_fact import MediaFact


def _attr(obj: object, name: str, default: object | None = None) -> object | None:
    value = getattr(obj, name, default)
    return default if value is None else value


def _number(value: object) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _first_attribute(attrs: Sequence[object], attribute_type: type[object]) -> object | None:
    return next((attribute for attribute in attrs if isinstance(attribute, attribute_type)), None)


def _text_payload(media: object, keys: Sequence[str]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key in keys:
        value = _text(_attr(media, key))
        if value:
            payload[key] = value
    return payload


def _location_payload(geo: object) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key in ("lat", "long"):
        number = _number(_attr(geo, key))
        if number is not None:
            payload[key] = number
    return payload


def extract_media_fact(media: object | None) -> MediaFact | None:
    """Extract one normalized fact from a Telethon ``Message.media`` value."""
    if media is None or isinstance(media, tl.MessageMediaEmpty):
        return None
    if isinstance(media, tl.MessageMediaPhoto):
        return MediaFact("photo", {})
    if isinstance(media, tl.MessageMediaDocument):
        return _extract_document(media)
    fact = _extract_location_or_poll(media)
    if fact is not None:
        return fact
    fact = _extract_rich_media(media)
    if fact is not None:
        return fact
    # Keep unknown media distinguishable from the legacy unresolved marker.
    # The class name is a stable, JSON-safe hint and never includes Telegram
    # object reprs or payload text.
    return MediaFact("other", {"type": type(media).__name__})


def _extract_document(media: object) -> MediaFact:
    document = _attr(media, "document")
    attrs = list(cast(Sequence[object], _attr(document, "attributes", []) or []))
    base = _document_base(document)
    sticker_fact = _extract_sticker(attrs, base)
    if sticker_fact is not None:
        return sticker_fact
    visual_fact = _extract_visual(document, attrs, base)
    if visual_fact is not None:
        return visual_fact
    audio = _first_attribute(attrs, tl.DocumentAttributeAudio)
    if audio is not None:
        return _audio_fact(audio, base)
    _add_filename(base, _first_attribute(attrs, tl.DocumentAttributeFilename))
    return MediaFact("document", base)


def _extract_sticker(attrs: Sequence[object], base: dict[str, object]) -> MediaFact | None:
    sticker = _first_attribute(attrs, tl.DocumentAttributeSticker)
    if sticker is None:
        return None
    alt = _text(_attr(sticker, "alt"))
    if alt:
        base["alt"] = alt
    return MediaFact("sticker", base)


def _extract_visual(document: object | None, attrs: Sequence[object], base: dict[str, object]) -> MediaFact | None:
    if any(isinstance(attribute, tl.DocumentAttributeAnimated) for attribute in attrs):
        return MediaFact("animation", base)
    video = _first_attribute(attrs, tl.DocumentAttributeVideo)
    if video is not None:
        duration = _number(_attr(video, "duration"))
        if duration is not None:
            base["duration"] = duration
        return MediaFact("video", base)
    mime_type = _attr(document, "mime_type")
    if isinstance(mime_type, str) and mime_type.lower() == "image/gif":
        return MediaFact("animation", base)
    return None


def _add_filename(base: dict[str, object], filename: object | None) -> None:
    if filename is None:
        return
    value = _text(_attr(filename, "file_name"))
    if value:
        base["file_name"] = value


def _document_base(document: object | None) -> dict[str, object]:
    base: dict[str, object] = {}
    size = _number(_attr(document, "size"))
    if size is not None:
        base["size"] = size
    return base


def _audio_fact(audio: object, base: dict[str, object]) -> MediaFact:
    duration = _number(_attr(audio, "duration"))
    if duration is not None:
        base["duration"] = duration
    for key in ("title", "performer"):
        value = _text(_attr(audio, key))
        if value:
            base[key] = value
    return MediaFact("voice" if _attr(audio, "voice", False) else "audio", base)


def _extract_location_or_poll(media: object) -> MediaFact | None:
    if isinstance(media, tl.MessageMediaPoll):
        question = _attr(_attr(media, "poll"), "question")
        value = _text(_attr(question, "text")) or _text(question)
        return MediaFact("poll", {"question": value} if value else {})
    if isinstance(media, (tl.MessageMediaGeo, tl.MessageMediaGeoLive)):
        geo = _attr(media, "geo") or media
        return MediaFact("location", _location_payload(geo))
    if isinstance(media, tl.MessageMediaVenue):
        return MediaFact("venue", _text_payload(media, ("title", "address", "provider", "venue_id")))
    if isinstance(media, tl.MessageMediaContact):
        contact_payload = _text_payload(media, ("phone_number", "first_name", "last_name"))
        user_id = _number(_attr(media, "user_id"))
        if user_id is not None:
            contact_payload["user_id"] = user_id
        return MediaFact("contact", contact_payload)
    return None


def _extract_rich_media(media: object) -> MediaFact | None:
    fact: MediaFact | None = None
    if isinstance(media, tl.MessageMediaWebPage):
        webpage = _attr(media, "webpage") or media
        fact = MediaFact(
            "link_preview", _text_payload(webpage, ("url", "display_url", "title", "description", "site_name"))
        )
    elif isinstance(media, tl.MessageMediaGame):
        game = _attr(media, "game") or media
        fact = MediaFact("game", _text_payload(game, ("title", "description")))
    elif isinstance(media, tl.MessageMediaInvoice):
        fact = _invoice_fact(media)
    elif isinstance(media, tl.MessageMediaDice):
        fact = _dice_fact(media)
    elif isinstance(media, tl.MessageMediaStory):
        story_id = _number(_attr(media, "story_id"))
        fact = MediaFact("story", {"story_id": story_id} if story_id is not None else {})
    return fact


def _invoice_fact(media: object) -> MediaFact:
    payload = _text_payload(media, ("title", "description", "currency"))
    total = _number(_attr(media, "total_amount"))
    if total is not None:
        payload["total_amount"] = total
    return MediaFact("invoice", payload)


def _dice_fact(media: object) -> MediaFact:
    payload: dict[str, object] = {}
    emoticon = _text(_attr(media, "emoticon"))
    dice_value = _number(_attr(media, "value"))
    if emoticon:
        payload["emoticon"] = emoticon
    if dice_value is not None:
        payload["value"] = dice_value
    return MediaFact("dice", payload)


__all__ = ["MediaFact", "extract_media_fact"]
