"""Contract tests for the legacy group profile Telethon adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from telethon.tl import types
from telethon.tl.functions.messages import GetFullChatRequest

from mcp_telegram.telegram_gateway import TelethonGroupProfileGateway


class _Client:
    def __init__(self, response: object) -> None:
        self.response = response
        self.requests: list[object] = []

    async def __call__(self, request: object) -> object:
        self.requests.append(request)
        return self.response


def _full_chat(
    *,
    chat_id: int = 123,
    participants: object | None = None,
    photo: object | None = None,
    chat_date: datetime | None = None,
) -> types.messages.ChatFull:
    if participants is None:
        participants = types.ChatParticipants(chat_id=chat_id, participants=[], version=1)
    full = types.ChatFull(
        id=chat_id,
        about="about",
        participants=cast(types.TypeChatParticipants, participants),
        notify_settings=types.PeerNotifySettings(),
        exported_invite=types.ChatInviteExported(link="https://t.me/+group", admin_id=1, date=None),
        chat_photo=cast(types.TypePhoto | None, photo),
    )
    chat = types.Chat(
        id=chat_id,
        title="Group",
        photo=types.ChatPhotoEmpty(),
        participants_count=1,
        date=chat_date,
        version=1,
    )
    return types.messages.ChatFull(full_chat=full, chats=[chat], users=[])


@pytest.mark.asyncio
async def test_group_gateway_builds_request_and_normalizes_all_primitives() -> None:
    participant = types.ChatParticipant(user_id=7, inviter_id=1, date=None)
    client = _Client(
        _full_chat(
            participants=types.ChatParticipants(chat_id=123, participants=[participant], version=1),
            photo=types.PhotoEmpty(id=55),
        )
    )
    gateway = TelethonGroupProfileGateway(client, now_provider=iter((100.2, 101.8)).__next__)

    observation = await gateway.fetch_group_profile(-123)

    assert isinstance(client.requests[0], GetFullChatRequest)
    assert client.requests[0].chat_id == 123
    assert observation.group_id == -123
    assert observation.about == "about"
    assert observation.invite_link == "https://t.me/+group"
    assert observation.participant_ids == (7,)
    assert observation.participants_unavailable_reason is None
    assert observation.current_photo is not None
    assert observation.current_photo.photo_id == 55
    assert observation.observation_started_at == 100
    assert observation.observation_completed_at == 101
    assert observation.identity_patch == {"type": "group", "name": "Group", "username": None}
    assert observation.created is None
    assert not hasattr(observation, "full_chat")


@pytest.mark.asyncio
async def test_group_gateway_captures_legacy_chat_creation_date() -> None:
    created = datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)
    observation = await TelethonGroupProfileGateway(
        _Client(_full_chat(chat_date=created)), now_provider=lambda: 100
    ).fetch_group_profile(-123)

    assert observation.created == int(created.timestamp())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("participants", "reason"),
    (
        (types.ChatParticipantsForbidden(chat_id=123), "participants_forbidden"),
        (None, "participants_missing"),
        (
            types.ChatParticipants(
                chat_id=123,
                participants=cast(list[types.TypeChatParticipant], ()),
                version=1,
            ),
            None,
        ),
        (
            types.ChatParticipants(
                chat_id=123,
                participants=cast(list[types.TypeChatParticipant], [object()]),
                version=1,
            ),
            "participants_malformed",
        ),
    ),
)
async def test_group_gateway_preserves_exact_participant_unavailable_reason(
    participants: object,
    reason: str | None,
) -> None:
    response = _full_chat(participants=participants)
    if participants is None:
        object.__setattr__(response.full_chat, "participants", None)
    client = _Client(response)

    observation = await TelethonGroupProfileGateway(client, now_provider=lambda: 100).fetch_group_profile(-123)

    if reason is None:
        assert observation.participant_ids == ()
        assert observation.participants_unavailable_reason is None
    else:
        assert observation.participant_ids is None
        assert observation.participants_unavailable_reason == reason


@pytest.mark.asyncio
async def test_group_gateway_rejects_nonsequence_participants() -> None:
    response = _full_chat()
    full_chat = cast(object, response.full_chat)
    participants = cast(object, getattr(full_chat, "participants", None))
    object.__setattr__(participants, "participants", object())

    observation = await TelethonGroupProfileGateway(_Client(response), now_provider=lambda: 100).fetch_group_profile(
        -123
    )

    assert observation.participant_ids is None
    assert observation.participants_unavailable_reason == "participants_not_sequence"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    (
        object(),
        types.messages.ChatFull(
            full_chat=types.ChannelFull(
                id=123,
                about="about",
                read_inbox_max_id=0,
                read_outbox_max_id=0,
                unread_count=0,
                chat_photo=types.PhotoEmpty(id=1),
                notify_settings=types.PeerNotifySettings(),
                bot_info=[],
                pts=1,
            ),
            chats=[],
            users=[],
        ),
        _full_chat(chat_id=124),
    ),
)
async def test_group_gateway_rejects_wrong_envelope_nested_channel_or_target(response: object) -> None:
    with pytest.raises(ValueError):
        await TelethonGroupProfileGateway(_Client(response), now_provider=lambda: 100).fetch_group_profile(-123)


@pytest.mark.asyncio
async def test_group_gateway_photo_date_is_iso_primitive() -> None:
    date = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    photo = types.Photo(
        has_stickers=False,
        id=55,
        access_hash=1,
        file_reference=b"ref",
        date=date,
        sizes=[],
        dc_id=1,
    )
    observation = await TelethonGroupProfileGateway(
        _Client(_full_chat(photo=photo)), now_provider=lambda: 100
    ).fetch_group_profile(-123)
    assert observation.current_photo is not None
    assert observation.current_photo.date == date.isoformat()


@pytest.mark.asyncio
async def test_group_gateway_photo_empty_zero_id_means_no_current_photo() -> None:
    observation = await TelethonGroupProfileGateway(
        _Client(_full_chat(photo=types.PhotoEmpty(id=0))), now_provider=lambda: 100
    ).fetch_group_profile(-123)

    assert observation.current_photo is None


@pytest.mark.asyncio
async def test_group_gateway_unknown_photo_subtype_is_an_optional_absence() -> None:
    response = _full_chat(photo=object())
    observation = await TelethonGroupProfileGateway(_Client(response), now_provider=lambda: 100).fetch_group_profile(
        -123
    )

    assert observation.current_photo is None


@pytest.mark.asyncio
async def test_group_gateway_rejects_malformed_known_photo_subtype() -> None:
    malformed = types.Photo(
        id=0,
        access_hash=1,
        file_reference=b"ref",
        date=datetime(2026, 9, 16, tzinfo=UTC),
        sizes=[],
        dc_id=1,
    )
    with pytest.raises(ValueError, match="current photo id"):
        await TelethonGroupProfileGateway(
            _Client(_full_chat(photo=malformed)), now_provider=lambda: 100
        ).fetch_group_profile(-123)
