from __future__ import annotations

from telethon.tl import types

from mcp_telegram.dialog_identity_contracts import IDENTITY_OMITTED, DialogIdentityObservation
from mcp_telegram.models import DialogType
from mcp_telegram.telethon_dialog import observe_dialog_identity


def _observe(entity: object) -> DialogIdentityObservation | None:
    return observe_dialog_identity(entity, dialog_id=42, source="test", observed_at=123)


def test_full_user_bot_and_service_are_complete_authoritative() -> None:
    user = _observe(types.User(id=42, first_name="Ada", last_name="Lovelace", username="ada"))
    bot = _observe(types.User(id=42, first_name="Helper", bot=True, username=None, usernames=[]))
    service = _observe(types.User(id=42, first_name="Replies", username="replies"))

    assert user is not None and user.complete is True
    assert (user.name, user.username, user.dialog_type) == ("Ada Lovelace", "ada", DialogType.USER)
    assert bot is not None and (bot.username, bot.dialog_type) == (None, DialogType.BOT)
    assert service is not None and service.dialog_type is DialogType.SERVICE


def test_full_channels_and_chat_are_complete_with_authoritative_absence() -> None:
    photo = types.ChatPhotoEmpty()
    channel = types.Channel(id=42, title="News", photo=photo, date=None, broadcast=True, username="news")
    supergroup = types.Channel(id=42, title="Group", photo=photo, date=None, megagroup=True)
    forum = types.Channel(id=42, title="Forum", photo=photo, date=None, megagroup=True, forum=True)
    chat = types.Chat(id=42, title="Legacy", photo=photo, participants_count=1, date=None, version=1)

    channel_observation = _observe(channel)
    supergroup_observation = _observe(supergroup)
    forum_observation = _observe(forum)
    assert channel_observation is not None and channel_observation.dialog_type is DialogType.CHANNEL
    assert supergroup_observation is not None and supergroup_observation.dialog_type is DialogType.SUPERGROUP
    assert forum_observation is not None and forum_observation.dialog_type is DialogType.FORUM
    legacy = _observe(chat)
    assert legacy is not None and legacy.complete is True
    assert (legacy.name, legacy.username, legacy.dialog_type) == ("Legacy", None, DialogType.GROUP)


def test_partial_objects_keep_only_positive_identity_facts() -> None:
    min_user = types.User(id=42, min=True, first_name="Ada", username="", usernames=[])
    min_channel = types.Channel(
        id=42,
        title="Partial",
        photo=types.ChatPhotoEmpty(),
        date=None,
        min=True,
        username=None,
        usernames=[types.Username("active", active=True)],
    )
    forbidden_channel = types.ChannelForbidden(id=42, access_hash=1, title="Hidden")
    forbidden_chat = types.ChatForbidden(id=42, title="Unavailable")

    for observation, name, username in (
        (_observe(min_user), "Ada", IDENTITY_OMITTED),
        (_observe(min_channel), "Partial", "active"),
        (_observe(forbidden_channel), "Hidden", IDENTITY_OMITTED),
        (_observe(forbidden_chat), "Unavailable", IDENTITY_OMITTED),
    ):
        assert observation is not None
        assert observation.complete is False
        assert observation.dialog_type is IDENTITY_OMITTED
        assert observation.name == name
        assert observation.username == username


def test_partial_empty_identity_and_unsupported_objects_are_omitted() -> None:
    empty_min_user = types.User(id=42, min=True)

    class ShapeOnly:
        first_name = "Looks like a user"

    assert _observe(empty_min_user) is None
    assert _observe(ShapeOnly()) is None
    assert _observe(None) is None


def test_partial_blank_values_do_not_become_deletions() -> None:
    min_user = types.User(id=42, min=True, first_name=" ", username=" ")
    observation = _observe(min_user)
    assert observation is None


def test_observation_carries_requested_metadata() -> None:
    observation = observe_dialog_identity(
        types.ChatForbidden(id=42, title="Restricted"),
        dialog_id=42,
        source="entity_profile",
        observed_at=456,
    )
    assert observation is not None
    assert (observation.dialog_id, observation.source, observation.observed_at) == (42, "entity_profile", 456)
