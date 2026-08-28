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
