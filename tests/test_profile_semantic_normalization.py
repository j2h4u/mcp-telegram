"""Semantic TL229 coverage, fidelity and authority without Telegram acquisition."""

import inspect
import json
import re
import sqlite3
import typing
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from telethon.tl import alltlobjects, types  # type: ignore[import-untyped]
from telethon.tl.tlobject import TLObject  # type: ignore[import-untyped]

from mcp_telegram.dialog_identity import publish_dialog_identity
from mcp_telegram.dialog_identity_contracts import IDENTITY_OMITTED
from mcp_telegram.entity_profile.contracts import (
    NORMALIZATION_VERSION,
    ObservationBoundary,
    ProfileFieldClass,
    ProfileFieldState,
    ProfileSemanticSnapshot,
)
from mcp_telegram.entity_profile.field_manifest import PROFILE_FIELD_MANIFEST, TL_PROFILE_LAYER
from mcp_telegram.entity_profile.full_user_normalization import normalize_full_user_response
from mcp_telegram.entity_profile.repository import _apply_identity_patch
from mcp_telegram.entity_profile.semantic_normalization import normalize_semantic_profile

BOUNDARY = ObservationBoundary(100.0, 101.0)


def _full(**kwargs: object) -> types.UserFull:
    result = types.UserFull(
        id=42, settings=types.PeerSettings(), notify_settings=types.PeerNotifySettings(), common_chats_count=0
    )
    for name, value in kwargs.items():
        setattr(result, name, value)
    return result


def _snapshot(
    user: object | None = None, full: object | None = None, *, owner: int | None = 77
) -> ProfileSemanticSnapshot:
    result = normalize_full_user_response(
        SimpleNamespace(
            users=[user if user is not None else types.User(id=42, first_name="Ada")],
            full_user=full if full is not None else _full(),
            chats=[],
        ),
        target_id=42,
        target_kind="user",
        observation=BOUNDARY,
        owner_account_id=owner,
    )
    assert result.semantic_snapshot is not None
    return result.semantic_snapshot


def _constructor_name(cls: type) -> str:
    module = cls.__module__.removeprefix("telethon.tl.types").strip(".")
    return f"{module}.{cls.__name__}" if module else cls.__name__


def _tl_children(annotation: object) -> list[type]:
    if isinstance(annotation, type) and issubclass(annotation, TLObject):
        return [annotation]
    return [child for arg in cast(tuple[object, ...], typing.get_args(annotation)) for child in _tl_children(arg)]


def _schema_value_types(annotation: object) -> set[str]:
    if annotation is type(None):
        return set()
    if isinstance(annotation, type):
        return {_constructor_name(annotation) if issubclass(annotation, TLObject) else annotation.__name__}
    return {name for arg in cast(tuple[object, ...], typing.get_args(annotation)) for name in _schema_value_types(arg)}


def test_manifest_exhaustively_matches_installed_recursive_schema() -> None:
    assert alltlobjects.LAYER == TL_PROFILE_LAYER == 229
    queue = deque([types.User, types.UserFull, types.UserEmpty, types.SavedStarGift])
    actual: dict[str, set[str]] = {}
    while queue:
        cls = queue.popleft()
        name = _constructor_name(cls)
        if name in actual:
            continue
        parameters = set(inspect.signature(cls).parameters)
        actual[name] = parameters
        schema = PROFILE_FIELD_MANIFEST[name]
        assert schema.constructor_id == cls.CONSTRUCTOR_ID
        if schema.protocol_only:
            assert not schema.fields
            assert cls.__name__.startswith("Input")
            assert not cls.__name__.startswith(("InputStickerSet", "InputChannel", "InputGroupCall"))
            continue
        assert set(schema.fields) == parameters, name
        assert all(isinstance(rule.classification, ProfileFieldClass) for rule in schema.fields.values())
        if not inspect.isfunction(cls.__init__):
            continue
        globals_ = dict(cls.__init__.__globals__)
        # Telethon 1.45's generated broad alias omits this namespaced definition.
        globals_["TypeEmojiGameOutcome"] = types.messages.EmojiGameOutcome
        hints = cast(dict[str, object], typing.get_type_hints(cls.__init__, globalns=globals_))
        flags = set(re.findall(r"_(\w+) = bool\(", inspect.getsource(cls.from_reader)))
        for field, annotation in hints.items():
            assert set(schema.fields[field].value_types) == _schema_value_types(annotation)
            assert schema.fields[field].flag is (field in flags)
            queue.extend(_tl_children(annotation))
    assert set(actual) == set(PROFILE_FIELD_MANIFEST)


def test_exact_note_text_utf16_entities_and_legacy_note_key_are_lossless() -> None:
    note = " \n💬汉e\u0301  "
    entities = [
        types.MessageEntityBold(offset=2, length=2),
        types.MessageEntityCustomEmoji(offset=4, length=1, document_id=99),
    ]
    full = _full(note=types.TextWithEntities(text=note, entities=entities))
    response = normalize_full_user_response(
        SimpleNamespace(users=[types.User(id=42)], full_user=full, chats=[]),
        target_id=42,
        target_kind="user",
        observation=BOUNDARY,
        owner_account_id=77,
    )
    assert response.full_profile.payload is not None
    assert response.full_profile.payload["note"] == note
    assert response.semantic_snapshot is not None
    normalized = cast(dict[str, object], response.semantic_snapshot.fields["full_user.note"])
    assert normalized["text"] == note
    normalized_entities = cast(list[dict[str, object]], normalized["entities"])
    assert [(e["offset"], e["length"]) for e in normalized_entities] == [(2, 2), (4, 1)]
    assert len("💬".encode("utf-16-le")) // 2 == 2
    assert (
        json.loads(json.dumps(response.semantic_snapshot.fields, ensure_ascii=False))
        == response.semantic_snapshot.fields
    )
    assert response.semantic_snapshot.observation is BOUNDARY
    assert response.semantic_snapshot.normalization_version == NORMALIZATION_VERSION == "entity-profile-full-user-v2"


def test_nested_business_usernames_emoji_and_profile_flags_are_preserved() -> None:
    user = types.User(
        id=42,
        deleted=True,
        support=True,
        usernames=[types.Username(username="alias", active=False, editable=True)],
        emoji_status=types.EmojiStatusCollectible(
            1, 2, "title", "slug", 3, 4, 5, 6, 7, until=datetime(2030, 1, 1, tzinfo=UTC)
        ),
    )
    sticker = types.Document(
        id=9,
        access_hash=987654321,
        file_reference=b"private-ref",
        date=None,
        mime_type="image/webp",
        size=40,
        dc_id=4,
        attributes=[
            types.DocumentAttributeSticker(alt="🙂", stickerset=types.InputStickerSetShortName(short_name="set"))
        ],
    )
    full = _full(
        business_work_hours=types.BusinessWorkHours(
            timezone_id="UTC", weekly_open=[types.BusinessWeeklyOpen(start_minute=10, end_minute=20)], open_now=True
        ),
        business_intro=types.BusinessIntro(title=" title ", description=" intro ", sticker=sticker),
    )
    snapshot = _snapshot(user, full)
    assert snapshot.fields["user.deleted"] is True and snapshot.fields["user.support"] is True
    aliases = cast(list[dict[str, object]], snapshot.fields["user.usernames"])
    assert aliases[0]["active"] is False and aliases[0]["editable"] is True
    emoji = cast(dict[str, object], snapshot.fields["user.emoji_status"])
    assert emoji["collectible_id"] == 1 and emoji["until"] == "2030-01-01T00:00:00+00:00"
    hours = cast(dict[str, object], snapshot.fields["full_user.business_work_hours"])
    assert hours["weekly_open"] == [{"constructor": "BusinessWeeklyOpen", "start_minute": 10, "end_minute": 20}]
    intro = cast(dict[str, object], snapshot.fields["full_user.business_intro"])
    assert intro["title"] == " title " and intro["description"] == " intro "
    assert cast(dict[str, object], intro["sticker"])["id"] == 9
    assert "private-ref" not in json.dumps(snapshot.fields) and "987654321" not in json.dumps(snapshot.fields)
    # InputStickerSet references are legitimately returned in sticker metadata.
    attribute = cast(list[dict[str, object]], cast(dict[str, object], intro["sticker"])["attributes"])[0]
    assert attribute["stickerset"] == {"constructor": "InputStickerSetShortName", "short_name": "set"}
    assert snapshot.complete


def test_photo_roles_dimensions_video_metadata_and_private_capabilities_are_separate() -> None:
    photo = types.Photo(
        id=10,
        access_hash=987654321,
        file_reference=b"private-ref",
        date=datetime(2025, 1, 2, tzinfo=UTC),
        sizes=[types.PhotoSize(type="m", w=100, h=200, size=12)],
        dc_id=5,
        video_sizes=[types.VideoSize(type="v", w=300, h=400, size=99, video_start_ts=1.25)],
    )
    snapshot = normalize_semantic_profile(
        types.User(id=42),
        _full(profile_photo=photo, personal_photo=photo, fallback_photo=types.PhotoEmpty(id=11)),
        target_id=42,
        owner_account_id=77,
        observation=BOUNDARY,
    )
    public = cast(dict[str, object], snapshot.fields["full_user.profile_photo"])
    assert public["id"] == 10 and public["date"] == "2025-01-02T00:00:00+00:00"
    assert cast(list[dict[str, object]], public["sizes"])[0]["w"] == 100
    assert cast(list[dict[str, object]], public["video_sizes"])[0]["video_start_ts"] == 1.25
    assert snapshot.coverage["full_user.personal_photo"].classification is ProfileFieldClass.PRIVATE_FACT
    assert snapshot.fields["full_user.fallback_photo"] == {"constructor": "PhotoEmpty", "id": 11}
    assert snapshot.coverage["full_user.fallback_photo"].state is ProfileFieldState.API_NOT_RETURNED
    assert not {"access_hash", "file_reference", "dc_id"} & public.keys()
    assert snapshot.coverage["full_user.profile_photo.access_hash"].classification is ProfileFieldClass.CAPABILITY
    assert snapshot.coverage["full_user.profile_photo.access_hash"].state is ProfileFieldState.INAPPLICABLE
    assert "987654321" not in json.dumps(snapshot.fields)
    assert "private-ref" not in json.dumps(snapshot.fields)
    assert snapshot.endpoint == "users.GetFullUser" and snapshot.observation is BOUNDARY


def test_none_nonreturn_clear_and_min_authority_are_distinct() -> None:
    full = _snapshot()
    assert full.complete
    assert full.coverage["user.phone"].state is ProfileFieldState.API_NOT_RETURNED
    assert not full.coverage["user.phone"].authoritative
    assert full.coverage["full_user.birthday"].state is ProfileFieldState.API_NOT_RETURNED
    assert full.coverage["full_user.note"].state is ProfileFieldState.CLEARED
    assert full.coverage["full_user.note"].authoritative
    assert full.fields["user.support"] is False
    minimum = _snapshot(types.User(id=42, min=True, first_name="Partial"))
    assert not minimum.complete
    assert not minimum.coverage["user.first_name"].authoritative
    assert not minimum.coverage["user.photo"].authoritative
    assert minimum.fields["user.first_name"] == "Partial"
    assert "user.photo" not in minimum.fields


def test_unknown_fields_nested_values_and_partial_attributes_cannot_complete_or_leak() -> None:
    user = types.User(id=42)
    unknown_field = "future_secret"
    setattr(user, unknown_field, "not exposed")
    note_value = types.TextWithEntities("known", [types.MessageEntityBold(0, 1)])
    note_value.entities = [types.MessageEntityBold(0, 1), SimpleNamespace(secret="unknown")]
    full = _full(note=note_value)
    snapshot = _snapshot(user, full)
    assert not snapshot.complete
    assert "user.future_secret" not in snapshot.fields
    assert snapshot.coverage["user.future_secret"].state is ProfileFieldState.UNKNOWN
    note = cast(dict[str, object], snapshot.fields["full_user.note"])
    assert cast(list[object], note["entities"])[0] == {"constructor": "MessageEntityBold", "offset": 0, "length": 1}
    assert cast(list[object], note["entities"])[1] is None
    assert not snapshot.coverage["full_user.note"].authoritative
    assert "unknown" not in json.dumps(snapshot.fields)
    partial = _snapshot(full=SimpleNamespace(id=42, note=None))
    assert not partial.complete and partial.coverage["full_user.settings"].state is ProfileFieldState.UNKNOWN
    assert not partial.coverage["full_user.note"].authoritative


def test_identity_account_and_empty_user_boundaries_are_explicit() -> None:
    snapshot = _snapshot(owner=None)
    assert not snapshot.complete
    with pytest.raises(ValueError, match="account"):
        snapshot.require_account(77)
    with pytest.raises(ValueError):
        _snapshot(owner=0)
    with pytest.raises(ValueError, match="target"):
        normalize_semantic_profile(types.User(id=99), _full(), target_id=42, owner_account_id=77, observation=BOUNDARY)
    foreign = _full()
    foreign.id = 99
    with pytest.raises(ValueError, match="target"):
        normalize_semantic_profile(types.User(id=42), foreign, target_id=42, owner_account_id=77, observation=BOUNDARY)
    empty = _snapshot(types.UserEmpty(id=42))
    assert not empty.complete and empty.fields["user.id"] == 42
    assert "user.deleted" not in empty.fields
    # Session rotation is not part of account ownership or semantic availability.
    retained = _snapshot()
    retained.require_account(77)
    assert _snapshot().fields == retained.fields
    with pytest.raises(ValueError, match="account"):
        retained.require_account(88)


def test_settings_prices_ratings_bot_music_gift_and_story_summaries_roundtrip() -> None:
    settings = types.PeerSettings(
        charge_paid_message_stars=17, registration_month="2024-03", phone_country="KZ", business_bot_paused=True
    )
    full = _full(
        settings=settings,
        send_paid_messages_stars=23,
        stars_rating=types.StarsRating(level=2, current_level_stars=20, stars=30, next_level_stars=40),
        disallowed_gifts=types.DisallowedGiftsSettings(disallow_unique_stargifts=True),
        stargifts_count=5,
        saved_music=types.DocumentEmpty(id=9),
        bot_info=types.BotInfo(
            description=" description ",
            commands=[types.BotCommand(command="start", description=" go ")],
            privacy_policy_url="https://example.test/privacy",
        ),
        stories=types.PeerStories(
            peer=types.PeerUser(42),
            stories=[
                types.StoryItemSkipped(
                    id=7,
                    date=datetime(2025, 1, 1, tzinfo=UTC),
                    expire_date=datetime(2025, 1, 2, tzinfo=UTC),
                    close_friends=True,
                )
            ],
            max_read_id=6,
        ),
    )
    snapshot = _snapshot(full=full)
    assert snapshot.complete
    normalized_settings = cast(dict[str, object], snapshot.fields["full_user.settings"])
    assert normalized_settings["charge_paid_message_stars"] == 17
    assert normalized_settings["phone_country"] == "KZ"
    assert snapshot.fields["full_user.send_paid_messages_stars"] == 23
    assert cast(dict[str, object], snapshot.fields["full_user.stars_rating"])["stars"] == 30
    assert cast(dict[str, object], snapshot.fields["full_user.disallowed_gifts"])["disallow_unique_stargifts"] is True
    assert snapshot.fields["full_user.stargifts_count"] == 5
    assert snapshot.fields["full_user.saved_music"] == {"constructor": "DocumentEmpty", "id": 9}
    assert (
        cast(dict[str, object], snapshot.fields["full_user.bot_info"])["privacy_policy_url"]
        == "https://example.test/privacy"
    )
    stories = cast(dict[str, object], snapshot.fields["full_user.stories"])
    assert cast(list[dict[str, object]], stories["stories"])[0]["close_friends"] is True
    assert json.loads(json.dumps(snapshot.fields)) == snapshot.fields
    assert snapshot.coverage["full_user.notify_settings.silent"].state is ProfileFieldState.API_NOT_RETURNED
    assert not snapshot.coverage["full_user.settings"].authoritative
    assert snapshot.coverage["full_user.settings.charge_paid_message_stars"].authoritative


def test_partial_subtrees_protocol_only_and_required_none_cannot_authorize_replacement() -> None:
    full = _full(settings=None)
    snapshot = _snapshot(full=full)
    assert not snapshot.complete
    assert snapshot.coverage["full_user.settings"].state is ProfileFieldState.UNKNOWN
    protocol = types.InputMessageEntityMentionName(
        offset=0, length=1, user_id=types.InputUser(user_id=9, access_hash=987654321)
    )
    note = types.TextWithEntities("text", [protocol])
    snapshot = _snapshot(full=_full(note=note))
    assert not snapshot.complete
    assert snapshot.coverage["full_user.note.entities[0]"].state is ProfileFieldState.INAPPLICABLE
    assert not snapshot.coverage["full_user.note"].authoritative
    assert "987654321" not in json.dumps(snapshot.fields)
    snapshot = _snapshot(types.User(id=42, status=types.UserStatusEmpty()))
    assert snapshot.fields["user.status"] == {"constructor": "UserStatusEmpty"}
    assert snapshot.coverage["user.status"].state is ProfileFieldState.API_NOT_RETURNED
    assert not snapshot.coverage["user.status"].authoritative


def test_malformed_min_flag_cannot_authorize_clears() -> None:
    user = types.User(id=42)
    flag_name = "min"
    setattr(user, flag_name, 1)
    snapshot = _snapshot(user)
    assert not snapshot.complete
    assert "user:min_flag_unknown" in snapshot.diagnostics
    assert not snapshot.coverage["user.photo"].authoritative
    assert not snapshot.coverage["user.username"].authoritative
    assert "user.photo" not in snapshot.fields


def test_partial_user_cannot_replace_legacy_name_membership_or_published_identity(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    user = types.User(id=42, min=True, first_name="Ada", last_name=None)
    result = normalize_full_user_response(
        SimpleNamespace(users=[user], full_user=_full(), chats=[]),
        target_id=42,
        target_kind="user",
        owner_account_id=77,
        observation=BOUNDARY,
    )
    assert result.full_profile.payload is not None
    assert "name" not in result.full_profile.payload
    assert "my_membership" not in result.full_profile.payload
    patch = result.full_profile.identity_patch or {}
    assert "name" not in patch and "username" not in patch
    assert (
        _apply_identity_patch({"id": 42, "name": "Ada Lovelace", "username": "retained"}, patch)["name"]
        == "Ada Lovelace"
    )
    identity = result.full_profile.dialog_identity_observation
    assert identity is not None and identity.name is IDENTITY_OMITTED and identity.username is IDENTITY_OMITTED
    conn = make_synced_db()
    conn.execute(
        "INSERT INTO dialogs(dialog_id,name,username,type,identity_revision,identity_complete) VALUES (42,'Ada Lovelace','retained','user',0,1)"
    )
    assert not publish_dialog_identity(conn, 42, identity, 0)
    assert conn.execute("SELECT name,username FROM dialogs WHERE dialog_id=42").fetchone() == (
        "Ada Lovelace",
        "retained",
    )


def test_nested_story_min_does_not_authorize_absence_or_complete() -> None:
    from mcp_telegram.entity_profile.field_manifest import PARTIAL_PROFILE_CONSTRUCTORS

    story = types.StoryItem(
        id=7,
        date=datetime(2025, 1, 1, tzinfo=UTC),
        expire_date=datetime(2025, 1, 2, tzinfo=UTC),
        media=types.MessageMediaEmpty(),
        min=True,
        caption="known",
        public=False,
    )
    snapshot = _snapshot(full=_full(stories=types.PeerStories(peer=types.PeerUser(42), stories=[story])))
    assert not snapshot.complete
    path = "full_user.stories.stories[0]"
    assert f"{path}:partial_constructor" in snapshot.diagnostics
    stories = cast(dict[str, object], snapshot.fields["full_user.stories"])
    normalized = cast(list[dict[str, object]], stories["stories"])[0]
    assert normalized["id"] == 7 and normalized["caption"] == "known"
    assert "pinned" not in normalized and "public" not in normalized
    assert not snapshot.coverage[f"{path}.public"].authoritative
    assert snapshot.coverage[f"{path}.public"].state is ProfileFieldState.UNKNOWN
    assert {"User", "StoryItem", "Channel", "Community", "PollResults"} == PARTIAL_PROFILE_CONSTRUCTORS
    for constructor in PARTIAL_PROFILE_CONSTRUCTORS:
        assert PROFILE_FIELD_MANIFEST[constructor].fields["min"].classification is ProfileFieldClass.CAPABILITY
