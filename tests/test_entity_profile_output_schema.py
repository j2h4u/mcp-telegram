"""Actual normalized personal-profile facts match a finite, closed wire schema."""

import json
import sqlite3
from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator, ValidationError
from telethon.tl import types

from mcp_telegram.entity_profile import output_schema, semantic_normalization
from mcp_telegram.entity_profile.collection_repository import store_semantic_snapshot
from mcp_telegram.entity_profile.contracts import ObservationBoundary, ProfileFieldObservation, ProfileSemanticSnapshot
from mcp_telegram.entity_profile.field_manifest import PROFILE_FIELD_MANIFEST
from mcp_telegram.entity_profile.output_schema import build_profile_fields_output_schema, project_profile_fields
from mcp_telegram.entity_profile.repository import EntityProfileRepository
from mcp_telegram.entity_profile.semantic_normalization import normalize_semantic_profile
from mcp_telegram.sync_transactions import write_savepoint

_DATE = datetime(2025, 1, 2, tzinfo=UTC)


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _items(value: object) -> list[object]:
    assert isinstance(value, list)
    return cast(list[object], value)


def _full() -> types.UserFull:
    return types.UserFull(
        id=42, settings=types.PeerSettings(), notify_settings=types.PeerNotifySettings(), common_chats_count=0
    )


def _normalize(full: types.UserFull, user: types.User | None = None) -> ProfileSemanticSnapshot:
    return normalize_semantic_profile(
        user if user is not None else types.User(id=42),
        full,
        target_id=42,
        owner_account_id=77,
        observation=ObservationBoundary(100, 101),
    )


@pytest.fixture(scope="module")
def schema() -> dict[str, object]:
    return build_profile_fields_output_schema()


@pytest.fixture(scope="module")
def validator(schema: dict[str, object]) -> Draft202012Validator:
    return Draft202012Validator(schema)


@pytest.fixture
def rich_snapshot() -> ProfileSemanticSnapshot:
    music = types.Document(
        id=8,
        access_hash=987654321,
        file_reference=b"private-ref",
        date=_DATE,
        mime_type="audio/ogg",
        size=99,
        dc_id=4,
        attributes=[types.DocumentAttributeAudio(duration=12, title="song", waveform=b"\x00\xff")],
    )
    photo = types.Photo(
        id=10,
        access_hash=987654321,
        file_reference=b"private-ref",
        date=_DATE,
        dc_id=4,
        sizes=[types.PhotoSize(type="m", w=100, h=200, size=12)],
        video_sizes=[types.VideoSize(type="v", w=300, h=400, size=99, video_start_ts=1.25)],
    )
    gift = types.StarGift(id=9, sticker=music, stars=5, convert_stars=3, title="gift")
    webpage = types.WebPage(
        id=11,
        url="https://example.test",
        display_url="example.test",
        hash=0,
        attributes=[types.WebPageAttributeUniqueStarGift(gift=gift)],
    )
    story = types.StoryItem(
        id=12,
        date=_DATE,
        expire_date=_DATE,
        caption="story",
        media=types.MessageMediaWebPage(webpage),
    )
    full = _full()
    full.note = types.TextWithEntities(
        text="  💬 note\n",
        entities=[types.MessageEntityFormattedDate(offset=2, length=2, date=_DATE, relative=True)],
    )
    full.profile_photo, full.personal_photo, full.fallback_photo = photo, photo, types.PhotoEmpty(id=13)
    full.business_intro = types.BusinessIntro(title=" title ", description=" intro ", sticker=music)
    full.business_work_hours = types.BusinessWorkHours(
        timezone_id="UTC",
        weekly_open=[types.BusinessWeeklyOpen(start_minute=60, end_minute=120)],
        open_now=True,
    )
    full.saved_music = music
    full.stories = types.PeerStories(peer=types.PeerUser(42), stories=[story])
    user = types.User(id=42, first_name="", last_name="Юникод💬", access_hash=987654321)
    user.photo = types.UserProfilePhoto(photo_id=10, dc_id=4, stripped_thumb=b"\x00\xff")
    return _normalize(full, user)


def test_schema_is_closed_finite_and_imports_the_isolated_source(schema: dict[str, object]) -> None:
    source = Path(__file__).resolve().parents[1] / "src/mcp_telegram/entity_profile"
    assert Path(output_schema.__file__).resolve() == source / "output_schema.py"
    assert Path(semantic_normalization.__file__).resolve() == source / "semantic_normalization.py"
    Draft202012Validator.check_schema(schema)
    definitions = _mapping(schema["$defs"])
    assert 300 < len(definitions) <= len(PROFILE_FIELD_MANIFEST) + 2
    assert len(json.dumps(schema, separators=(",", ":"))) < 230_000
    assert schema["additionalProperties"] is False
    for name, value in definitions.items():
        definition = _mapping(value)
        assert definition["additionalProperties"] is False
        if name in PROFILE_FIELD_MANIFEST:
            assert _mapping(_mapping(definition["properties"])["constructor"])["const"] == name
            assert not PROFILE_FIELD_MANIFEST[name].protocol_only


def test_real_rich_profile_keeps_facts_and_marks_text_untrusted(
    rich_snapshot: ProfileSemanticSnapshot,
    validator: Draft202012Validator,
) -> None:
    before = deepcopy(dict(rich_snapshot.fields))
    wire = project_profile_fields(rich_snapshot.fields)
    validator.validate(wire)
    assert rich_snapshot.fields == before
    assert _mapping(wire["user.first_name"])["text"] == ""
    assert _mapping(wire["user.last_name"])["text"] == "Юникод💬"
    note = _mapping(wire["full_user.note"])
    assert _mapping(note["text"]) == {
        "text": "  💬 note\n",
        "is_telegram_content": True,
        "content_kind": "note",
    }
    entity = _mapping(_items(note["entities"])[0])
    assert (entity["constructor"], entity["offset"], entity["length"], entity["date"]) == (
        "MessageEntityFormattedDate",
        2,
        2,
        _DATE.isoformat(),
    )
    photo = _mapping(wire["full_user.profile_photo"])
    assert photo["date"] == _DATE.isoformat()
    assert _mapping(_items(photo["sizes"])[0])["w"] == 100
    assert _mapping(_items(photo["video_sizes"])[0])["video_start_ts"] == 1.25
    assert _mapping(wire["full_user.personal_photo"])["id"] == 10
    assert _mapping(wire["full_user.fallback_photo"])["constructor"] == "PhotoEmpty"
    assert _mapping(_mapping(wire["user.photo"])["stripped_thumb"]) == {"encoding": "base64", "data": "AP8="}
    assert _mapping(wire["full_user.saved_music"])["id"] == 8
    assert _mapping(wire["full_user.business_work_hours"])["open_now"] is True
    story = _mapping(_items(_mapping(wire["full_user.stories"])["stories"])[0])
    webpage = _mapping(_mapping(story["media"])["webpage"])
    gift = _mapping(_mapping(_items(webpage["attributes"])[0])["gift"])
    assert gift["constructor"] == "StarGift"
    assert _mapping(gift["title"])["text"] == "gift"
    assert "private-ref" not in json.dumps(wire) and "987654321" not in json.dumps(wire)


def test_clears_missing_children_and_unknown_vector_positions_are_lossless(validator: Draft202012Validator) -> None:
    full = _full()
    full.note = types.TextWithEntities(text="text", entities=[])
    full.note.entities = [object(), types.MessageEntityBold(offset=1, length=2)]
    snapshot = _normalize(full)
    wire = project_profile_fields(snapshot.fields)
    validator.validate(wire)
    assert wire["full_user.about"] is None
    entities = _items(_mapping(wire["full_user.note"])["entities"])
    assert entities[0] is None
    assert _mapping(entities[1])["offset"] == 1
    validator.validate({"full_user.note": {"constructor": "TextWithEntities"}})
    validator.validate({})


@pytest.mark.parametrize("property_name", ["access_hash", "file_reference", "unexpected"])
def test_closed_schema_rejects_unknown_and_transport_properties(
    rich_snapshot: ProfileSemanticSnapshot,
    validator: Draft202012Validator,
    property_name: str,
) -> None:
    wire = project_profile_fields(rich_snapshot.fields)
    _mapping(wire["full_user.profile_photo"])[property_name] = "secret"
    with pytest.raises(ValidationError):
        validator.validate(wire)
    with pytest.raises(ValidationError):
        validator.validate({f"user.{property_name}": "secret"})


def test_schema_rejects_raw_text_false_trust_and_protocol_objects(validator: Draft202012Validator) -> None:
    with pytest.raises(ValidationError):
        validator.validate({"user.first_name": "raw Telegram text"})
    with pytest.raises(ValidationError):
        validator.validate(
            {"user.first_name": {"text": "text", "is_telegram_content": False, "content_kind": "snippet"}}
        )
    with pytest.raises(ValidationError):
        validator.validate({"full_user.stories": {"constructor": "InputPeerUser", "user_id": 42}})
    fields: Mapping[str, object] = {"user.access_hash": 123, "user.first_name": "safe"}
    assert project_profile_fields(fields) == {
        "user.first_name": {"text": "safe", "is_telegram_content": True, "content_kind": "snippet"}
    }


def test_projector_rejects_unexpected_or_protocol_only_constructors() -> None:
    with pytest.raises(ValueError, match="Unexpected normalized profile constructor"):
        project_profile_fields({"full_user.note": {"constructor": "Photo"}})
    with pytest.raises(ValueError, match="Protocol-only profile constructor"):
        project_profile_fields({"user.emoji_status": {"constructor": "InputEmojiStatusCollectible"}})


def test_newer_canonical_clear_survives_stale_snapshot_and_validates(
    make_synced_db: Callable[[], sqlite3.Connection],
    validator: Draft202012Validator,
) -> None:
    conn = make_synced_db()
    snapshot = _normalize(_full(), types.User(id=42, phone="123"))
    repository = EntityProfileRepository(conn, section_ttl_seconds=300)
    with write_savepoint(conn):
        conn.execute("INSERT INTO entities(id,type,updated_at) VALUES(42,'user',1)")
        assert store_semantic_snapshot(conn, snapshot, 1) is not None
        assert repository.apply_profile_update(
            42,
            {"phone": None},
            observation=ProfileFieldObservation(77, 3, 150, "update"),
        ) == frozenset({"phone"})
        merged = store_semantic_snapshot(conn, snapshot, 2)
    assert merged is not None
    assert merged.after["user.phone"] is None
    before = deepcopy(dict(merged.after))
    wire = project_profile_fields(merged.after)
    assert wire["user.phone"] is None
    assert merged.after == before
    validator.validate(wire)
    persisted = cast(
        tuple[str, str, str] | None,
        conn.execute(
            "SELECT fields_json,coverage_json,provenance_json FROM entity_profile_semantic_snapshots "
            "WHERE owner_account_id=77 AND entity_id=42"
        ).fetchone(),
    )
    assert persisted is not None
    fields = _mapping(cast(object, json.loads(persisted[0])))
    coverage = _mapping(cast(object, json.loads(persisted[1])))
    provenance = _mapping(cast(object, json.loads(persisted[2])))
    assert fields["user.phone"] is None
    assert _mapping(coverage["user.phone"])["state"] == "unknown"
    assert _mapping(_mapping(provenance["fields"])["user.phone"])["order"] == 3
