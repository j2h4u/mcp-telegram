"""Telethon media classification and human-readable descriptions."""

from collections.abc import Sequence
from typing import cast

import telethon.tl.types as tl  # type: ignore[import-untyped]


def _safe_attr_chain(obj: object, *attrs: str) -> object | None:
    """Traverse a chain of getattr calls, returning None if any link is missing."""
    for attr in attrs:
        if obj is None:
            return None
        obj = getattr(obj, attr, None)
    return obj


def _describe_poll(media: object) -> str:
    question = _safe_attr_chain(media, "poll", "question")
    if question is None:
        return "[опрос]"
    q_text = getattr(question, "text", None) or str(question)
    return f"[опрос: «{q_text}»]" if q_text else "[опрос]"


def _describe_geo(media: object) -> str:
    lat = _safe_attr_chain(media, "geo", "lat")
    lon = _safe_attr_chain(media, "geo", "long")
    if lat is not None and lon is not None:
        return f"[геолокация: {lat:.4f}, {lon:.4f}]"
    return "[геолокация]"


def _describe_venue(media: object) -> str:
    title = getattr(media, "title", None)
    address = getattr(media, "address", None)
    info = ", ".join(filter(None, [title, address]))
    return f"[место: {info}]" if info else "[место]"


def _describe_contact(media: object) -> str:
    first = getattr(media, "first_name", "") or ""
    last = getattr(media, "last_name", "") or ""
    name = " ".join(filter(None, [first, last]))
    phone = getattr(media, "phone_number", "") or ""
    info = ", ".join(filter(None, [name, phone]))
    return f"[контакт: {info}]" if info else "[контакт]"


def _describe_dice(media: object) -> str:
    emoticon = getattr(media, "emoticon", "🎲") or "🎲"
    value = getattr(media, "value", None)
    return f"[{emoticon} {value}]" if value is not None else f"[{emoticon}]"


def _describe_game(media: object) -> str:
    title = _safe_attr_chain(media, "game", "title")
    return f"[игра: {title}]" if title else "[игра]"


def _describe_invoice(media: object) -> str:
    title = getattr(media, "title", None)
    return f"[счёт: {title}]" if title else "[счёт]"


def _describe_web_page(media: object) -> str:
    url = _safe_attr_chain(media, "webpage", "url")
    return f"[ссылка: {url}]" if url else "[ссылка]"


def _describe_document_sticker(attr: object) -> str:
    alt = getattr(attr, "alt", "") or ""
    return f"[стикер: {alt}]" if alt else "[стикер]"


def _describe_document_round_video(attr: object) -> str:
    dur = getattr(attr, "duration", 0) or 0
    m, s = divmod(int(dur), 60)
    return f"[кружок: {m}:{s:02d}]"


def _describe_document_audio(attr: object) -> str:
    dur = getattr(attr, "duration", 0) or 0
    m, s = divmod(int(dur), 60)
    if getattr(attr, "voice", False):
        return f"[голосовое: {m}:{s:02d}]"
    title = getattr(attr, "title", None)
    performer = getattr(attr, "performer", None)
    info = " — ".join(filter(None, [performer, title]))
    return f"[аудио: {info}, {m}:{s:02d}]" if info else f"[аудио: {m}:{s:02d}]"


def _describe_document_video(attr: object) -> str:
    dur = getattr(attr, "duration", 0) or 0
    m, s = divmod(int(dur), 60)
    return f"[видео: {m}:{s:02d}]"


def _describe_document_filename(doc: object, attr: tl.DocumentAttributeFilename) -> str:
    size = getattr(doc, "size", None)
    size_str = f", {size // 1024}KB" if size else ""
    return f"[документ: {attr.file_name}{size_str}]"


def describe_media(media: object) -> str:
    """Return a human-readable placeholder for a media attachment."""
    if isinstance(media, tl.MessageMediaEmpty):
        return ""

    if isinstance(media, tl.MessageMediaDocument):
        return describe_document(media)

    handlers = (
        (tl.MessageMediaPhoto, lambda _: "[фото]"),
        (tl.MessageMediaPoll, _describe_poll),
        (tl.MessageMediaGeoLive, lambda _: "[геолокация live]"),
        (tl.MessageMediaGeo, _describe_geo),
        (tl.MessageMediaVenue, _describe_venue),
        (tl.MessageMediaContact, _describe_contact),
        (tl.MessageMediaDice, _describe_dice),
        (tl.MessageMediaGame, _describe_game),
        (tl.MessageMediaStory, lambda _: "[история]"),
        (tl.MessageMediaInvoice, _describe_invoice),
        (tl.MessageMediaWebPage, _describe_web_page),
        (tl.MessageMediaUnsupported, lambda _: "[неподдерживаемый тип]"),
    )
    for media_type, handler in handlers:
        if isinstance(media, media_type):
            return handler(media)

    return f"[медиа: {type(media).__name__}]"


def describe_document(media: object) -> str:
    """Describe a MessageMediaDocument using its first matching attribute priority."""
    doc = cast(object | None, getattr(media, "document", None))
    if doc is None:
        return "[документ]"
    attrs = list(cast(Sequence[object], getattr(doc, "attributes", [])) or [])
    sticker_attr = next((attr for attr in attrs if isinstance(attr, tl.DocumentAttributeSticker)), None)
    round_video_attr = next(
        (
            attr
            for attr in attrs
            if isinstance(attr, tl.DocumentAttributeVideo) and cast(bool, getattr(attr, "round_message", False))
        ),
        None,
    )
    audio_attr = next((attr for attr in attrs if isinstance(attr, tl.DocumentAttributeAudio)), None)
    video_attr = next((attr for attr in attrs if isinstance(attr, tl.DocumentAttributeVideo)), None)
    filename_attr = next((attr for attr in attrs if isinstance(attr, tl.DocumentAttributeFilename)), None)

    description = "[документ]"
    if sticker_attr is not None:
        description = _describe_document_sticker(sticker_attr)
    elif round_video_attr is not None:
        description = _describe_document_round_video(round_video_attr)
    elif any(isinstance(attr, tl.DocumentAttributeAnimated) for attr in attrs):
        description = "[анимация]"
    elif audio_attr is not None:
        description = _describe_document_audio(audio_attr)
    elif video_attr is not None:
        description = _describe_document_video(video_attr)
    elif filename_attr is not None:
        description = _describe_document_filename(doc, filename_attr)

    return description
