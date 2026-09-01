from __future__ import annotations

import json
import subprocess
import sys
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
