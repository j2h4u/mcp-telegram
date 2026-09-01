from __future__ import annotations

import json
import subprocess
import sys
from typing import cast
from unittest.mock import MagicMock

import pytest
import telethon.tl.types as tl  # type: ignore[import-untyped]

from mcp_telegram.media_fact import (
    MediaFact,
    decode_media_fact,
    encode_media_fact,
    encode_media_payload,
    is_transcribable_telegram_media,
    media_description,
)
from mcp_telegram.telethon_media import extract_media_fact


def test_codec_is_canonical_and_rejects_non_json_values() -> None:
    fact = MediaFact("document", {"file_name": "отчёт.pdf", "size": 42})
    kind, payload = encode_media_fact(fact)
    assert kind == "document"
    assert payload == '{"file_name":"отчёт.pdf","size":42}'
    assert decode_media_fact(kind, payload) == fact

    with pytest.raises(TypeError):
        encode_media_payload(MediaFact("document", {"raw": object()}))


def test_neutral_media_fact_import_does_not_load_telethon() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import mcp_telegram.media_fact; assert 'telethon' not in sys.modules"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("kind", "payload", "expected", "malformed_expected"),
    [
        ("photo", "{}", "[фото]", "[фото]"),
        ("video", '{"duration":65}', "[видео: 1:05]", "[видео]"),
        ("contact", '{"first_name":"Ada","phone_number":"+1"}', "Ada, +1", None),
        ("location", '{"lat":51.123456,"long":71.987654}', "[геолокация: 51.1235, 71.9877]", "[геолокация]"),
    ],
)
def test_projector_derives_description_only_from_valid_fact(
    kind: str, payload: str, expected: str, malformed_expected: str | None
) -> None:
    from mcp_telegram.media_fact import decode_media_fact

    assert media_description(decode_media_fact(kind, payload)) == expected
    assert media_description(decode_media_fact("not-a-kind", "legacy description")) is None
    assert media_description(decode_media_fact(kind, "not-json")) == malformed_expected


def test_extractor_normalizes_telethon_document_and_unknown_media() -> None:
    document = MagicMock(spec=tl.Document, size=2048, mime_type="application/pdf")
    document.attributes = [MagicMock(spec=tl.DocumentAttributeFilename, file_name="invoice.pdf")]
    media = MagicMock(spec=tl.MessageMediaDocument, document=document)
    assert extract_media_fact(media) == MediaFact("document", {"file_name": "invoice.pdf", "size": 2048})

    unknown = extract_media_fact(object())
    assert unknown == MediaFact("other", {"type": "object"})
    assert extract_media_fact(None) is None


def test_extractor_classifies_custom_emoji_before_generic_document() -> None:
    custom_emoji = tl.DocumentAttributeCustomEmoji(alt="📊", stickerset=tl.InputStickerSetEmpty())
    document = MagicMock(spec=tl.Document, size=128, mime_type="application/x-tgsticker")
    document.attributes = [custom_emoji]
    media = MagicMock(spec=tl.MessageMediaDocument, document=document)

    fact = extract_media_fact(media)
    assert fact is not None
    assert fact == MediaFact("custom_emoji", {"size": 128, "alt": "📊"})
    assert media_description(fact) == "[кастомный эмодзи: 📊]"
    assert "stickerset" not in fact.payload


def test_malformed_custom_emoji_attribute_falls_back_to_document() -> None:
    custom_emoji = MagicMock(spec=tl.DocumentAttributeCustomEmoji, alt=None)
    document = MagicMock(spec=tl.Document, size=128, mime_type="application/octet-stream")
    document.attributes = [custom_emoji]
    media = MagicMock(spec=tl.MessageMediaDocument, document=document)

    assert extract_media_fact(media) == MediaFact("document", {"size": 128})


def test_story_fact_keeps_telethon_story_item_id() -> None:
    media = tl.MessageMediaStory(peer=tl.PeerUser(7), id=42)

    assert extract_media_fact(media) == MediaFact("story", {"story_id": 42})


def test_extractor_payload_is_json_safe_and_has_no_telethon_objects() -> None:
    media = MagicMock(spec=tl.MessageMediaContact, phone_number="+7700", first_name="A", last_name="B", user_id=9)
    fact = extract_media_fact(media)
    assert fact is not None
    encoded = encode_media_payload(fact)
    assert encoded is not None
    assert json.loads(encoded) == {"first_name": "A", "last_name": "B", "phone_number": "+7700", "user_id": 9}


def test_projector_enriches_frequent_media_without_transport_details() -> None:
    assert (
        media_description(
            MediaFact("link_preview", {"url": "https://example.test", "site_name": "Example", "title": "Report"})
        )
        == "[ссылка: Example — Report]"
    )
    assert media_description(MediaFact("video", {"duration": 65, "round_message": True})) == "[кружок: 1:05]"
    assert media_description(MediaFact("photo", {"spoiler": True, "live_photo": True, "ttl_seconds": 30})) == (
        "[фото: Live Photo; спойлер; исчезающее]"
    )
    assert media_description(MediaFact("sticker", {"alt": "🙂", "set_name": "friendly_faces"})) == (
        "[стикер: 🙂; набор friendly_faces]"
    )


@pytest.mark.parametrize(
    ("fact", "expected"),
    [
        (MediaFact("voice", {}), True),
        (MediaFact("video", {"round_message": True}), True),
        (MediaFact("video", {"round_message": False}), False),
        (MediaFact("video", {}), False),
        (MediaFact("audio", {"round_message": True}), False),
        (MediaFact("story", {"round_message": True}), False),
        (MediaFact("video", {"round_message": "true"}), False),
        (MediaFact("video", {"round_message": True, "raw": object()}), False),
        (None, False),
    ],
)
def test_transcribable_media_predicate_is_fail_closed(fact: MediaFact | None, expected: bool) -> None:
    assert is_transcribable_telegram_media(fact) is expected


def test_extractor_preserves_only_agent_useful_frequent_media_facts() -> None:
    photo = tl.MessageMediaPhoto(spoiler=True, live_photo=True, ttl_seconds=30)
    assert extract_media_fact(photo) == MediaFact("photo", {"spoiler": True, "live_photo": True, "ttl_seconds": 30})

    sticker = tl.DocumentAttributeSticker(alt="🙂", stickerset=tl.InputStickerSetShortName("friendly_faces"))
    video = tl.DocumentAttributeVideo(duration=65, w=320, h=320, round_message=True)
    sticker_document = MagicMock(spec=tl.Document, size=1024, mime_type="application/x-tgsticker")
    sticker_document.attributes = [sticker]
    video_document = MagicMock(spec=tl.Document, size=2048, mime_type="video/mp4")
    video_document.attributes = [video]

    assert extract_media_fact(MagicMock(spec=tl.MessageMediaDocument, document=sticker_document)) == MediaFact(
        "sticker", {"size": 1024, "alt": "🙂", "set_name": "friendly_faces"}
    )
    assert extract_media_fact(MagicMock(spec=tl.MessageMediaDocument, document=video_document)) == MediaFact(
        "video", {"size": 2048, "duration": 65, "round_message": True}
    )


@pytest.mark.parametrize(
    ("flag", "expected"),
    [
        ("video", MediaFact("video", {"round_message": False})),
        ("round", MediaFact("video", {"round_message": True})),
        ("voice", MediaFact("voice", {})),
    ],
)
def test_extractor_uses_document_wrapper_media_flags(flag: str, expected: MediaFact) -> None:
    if flag == "video":
        media = tl.MessageMediaDocument(document=None, video=True)
    elif flag == "round":
        media = tl.MessageMediaDocument(document=None, round=True)
    else:
        media = tl.MessageMediaDocument(document=None, voice=True)
    assert extract_media_fact(media) == expected


def test_extractor_and_projector_enrich_new_video_with_filename_and_wrapper_flags() -> None:
    video_attribute = tl.DocumentAttributeVideo(duration=65, w=1920, h=1080, round_message=False)
    filename_attribute = tl.DocumentAttributeFilename(file_name="report.mp4")
    document = MagicMock(spec=tl.Document, size=2048, mime_type="video/mp4")
    document.attributes = [video_attribute, filename_attribute]
    media = tl.MessageMediaDocument(document=document, video=True, spoiler=True, ttl_seconds=30)

    fact = extract_media_fact(media)

    assert fact == MediaFact(
        "video",
        {
            "size": 2048,
            "duration": 65,
            "round_message": False,
            "file_name": "report.mp4",
            "spoiler": True,
            "ttl_seconds": 30,
        },
    )
    assert media_description(fact) == "[видео: 1:05; report.mp4; спойлер; исчезающее]"


def test_round_video_keeps_round_label_and_filename() -> None:
    video_attribute = tl.DocumentAttributeVideo(duration=5, w=320, h=320, round_message=True)
    filename_attribute = tl.DocumentAttributeFilename(file_name="circle.mp4")
    document = MagicMock(spec=tl.Document, size=512, mime_type="video/mp4")
    document.attributes = [video_attribute, filename_attribute]
    media = tl.MessageMediaDocument(document=document, round=True)

    fact = extract_media_fact(media)

    assert fact == MediaFact("video", {"size": 512, "duration": 5, "round_message": True, "file_name": "circle.mp4"})
    assert media_description(fact) == "[кружок: 0:05; circle.mp4]"


@pytest.mark.parametrize("ttl_seconds", [None, 0, -1, 1.5, float("inf"), True, "30"])
def test_video_omits_malformed_or_non_positive_wrapper_ttl(ttl_seconds: object) -> None:
    media = MagicMock(spec=tl.MessageMediaDocument, document=None, video=True, ttl_seconds=ttl_seconds)

    assert extract_media_fact(media) == MediaFact("video", {"round_message": False})


def test_video_omits_malformed_filename() -> None:
    filename_attribute = MagicMock(spec=tl.DocumentAttributeFilename, file_name="")
    document = MagicMock(spec=tl.Document, size=512, mime_type="video/mp4")
    document.attributes = [filename_attribute]

    assert extract_media_fact(tl.MessageMediaDocument(document=document, video=True)) == MediaFact(
        "video", {"size": 512, "round_message": False}
    )


@pytest.mark.parametrize("ttl_seconds", [0, -1, 1.5, float("inf"), True, "30"])
def test_photo_omits_malformed_or_non_positive_ttl(ttl_seconds: object) -> None:
    media = tl.MessageMediaPhoto(ttl_seconds=cast(int | None, ttl_seconds))

    assert extract_media_fact(media) == MediaFact("photo", {})


def test_video_filename_description_is_compact_and_single_line() -> None:
    filename_attribute = tl.DocumentAttributeFilename(file_name="report\n[evil]\x1b\t.mp4")
    document = MagicMock(spec=tl.Document, size=512, mime_type="video/mp4")
    document.attributes = [filename_attribute]
    fact = extract_media_fact(tl.MessageMediaDocument(document=document, video=True))

    assert media_description(fact) == "[видео: report evil .mp4]"
    assert "\n" not in (media_description(fact) or "")
    assert "[evil]" not in (media_description(fact) or "")
