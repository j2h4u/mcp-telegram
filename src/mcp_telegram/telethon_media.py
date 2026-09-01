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


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _positive_integer(value: object) -> int | None:
    integer = _integer(value)
    return integer if integer is not None and integer > 0 else None


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
        return MediaFact("photo", _photo_payload(media))
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
    raw_attrs = _attr(document, "attributes", [])
    attrs = (
        list(cast(Sequence[object], raw_attrs))
        if isinstance(raw_attrs, Sequence) and not isinstance(raw_attrs, (str, bytes, bytearray))
        else []
    )
    base = _document_base(document)
    wrapper_voice = _attr(media, "voice") is True
    if wrapper_voice:
        audio = _first_attribute(attrs, tl.DocumentAttributeAudio)
        if audio is not None:
            return _audio_fact(audio, base, force_voice=True)
        return MediaFact("voice", base)
    # Custom emoji is checked first because it is a specialized document
    # attribute and must not be hidden by the generic document fallback.
    sticker_fact = _extract_custom_emoji(attrs, base)
    if sticker_fact is None:
        sticker_fact = _extract_sticker(attrs, base)
    if sticker_fact is not None:
        return sticker_fact
    visual_fact = _extract_visual(document, attrs, base, media)
    if visual_fact is not None:
        return visual_fact
    audio = _first_attribute(attrs, tl.DocumentAttributeAudio)
    if audio is not None:
        return _audio_fact(audio, base)
    _add_filename(base, _first_attribute(attrs, tl.DocumentAttributeFilename))
    return MediaFact("document", base)


def _photo_payload(media: object) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key in ("spoiler", "live_photo"):
        if _attr(media, key) is True:
            payload[key] = True
    ttl_seconds = _positive_integer(_attr(media, "ttl_seconds"))
    if ttl_seconds is not None:
        payload["ttl_seconds"] = ttl_seconds
    return payload


def _extract_sticker(attrs: Sequence[object], base: dict[str, object]) -> MediaFact | None:
    sticker = _first_attribute(attrs, tl.DocumentAttributeSticker)
    if sticker is None:
        return None
    alt = _text(_attr(sticker, "alt"))
    if alt:
        base["alt"] = alt
    set_name = _text(_attr(_attr(sticker, "stickerset"), "short_name"))
    if set_name:
        base["set_name"] = set_name
    return MediaFact("sticker", base)


def _extract_custom_emoji(attrs: Sequence[object], base: dict[str, object]) -> MediaFact | None:
    custom_emoji = _first_attribute(attrs, tl.DocumentAttributeCustomEmoji)
    if custom_emoji is None:
        return None
    # A custom-emoji attribute without its required textual alternative is
    # malformed. Keep the pre-existing generic-document fallback in that case.
    alt = _text(_attr(custom_emoji, "alt"))
    if alt is None:
        return None
    base["alt"] = alt
    return MediaFact("custom_emoji", base)


def _extract_visual(
    document: object | None,
    attrs: Sequence[object],
    base: dict[str, object],
    media: object,
) -> MediaFact | None:
    video = _first_attribute(attrs, tl.DocumentAttributeVideo)
    if video is not None or _is_wrapper_video(media):
        return _video_fact(attrs, base, media, video)
    if any(isinstance(attribute, tl.DocumentAttributeAnimated) for attribute in attrs):
        return MediaFact("animation", base)
    mime_type = _attr(document, "mime_type")
    if isinstance(mime_type, str) and mime_type.lower() == "image/gif":
        return MediaFact("animation", base)
    return None


def _is_wrapper_video(media: object) -> bool:
    return _attr(media, "video") is True or _attr(media, "round") is True


def _video_fact(attrs: Sequence[object], base: dict[str, object], media: object, video: object | None) -> MediaFact:
    _add_filename(base, _first_attribute(attrs, tl.DocumentAttributeFilename))
    duration = _number(_attr(video, "duration")) if video is not None else None
    if duration is not None:
        base["duration"] = duration
    base["round_message"] = _attr(media, "round") is True or _attr(video, "round_message") is True
    _add_visual_wrapper_flags(base, media)
    return MediaFact("video", base)


def _add_visual_wrapper_flags(base: dict[str, object], media: object) -> None:
    """Add only the safe MessageMediaDocument flags for a visual fact."""
    if _attr(media, "spoiler") is True:
        base["spoiler"] = True
    ttl_seconds = _positive_integer(_attr(media, "ttl_seconds"))
    if ttl_seconds is not None:
        base["ttl_seconds"] = ttl_seconds


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


def _audio_fact(audio: object, base: dict[str, object], *, force_voice: bool = False) -> MediaFact:
    duration = _number(_attr(audio, "duration"))
    if duration is not None:
        base["duration"] = duration
    for key in ("title", "performer"):
        value = _text(_attr(audio, key))
        if value:
            base[key] = value
    return MediaFact("voice" if force_voice or _attr(audio, "voice", False) is True else "audio", base)


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
        # Telethon exposes the TL ``stories.StoryItem`` identifier as ``id``.
        # Keep the canonical key explicit so it cannot be confused with the
        # containing Telegram message id.
        story_id = _integer(_attr(media, "id"))
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
