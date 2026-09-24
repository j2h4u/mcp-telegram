"""Contract tests for the channel profile Telethon adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from telethon.errors import ChatAdminRequiredError
from telethon.tl import types
from telethon.tl.functions.channels import GetFullChannelRequest, GetParticipantsRequest

from mcp_telegram.entity_profile.contracts import ChannelReference, ProjectionStatus
from mcp_telegram.telegram_gateway import TelethonChannelProfileGateway


class _Client:
    def __init__(self, response: object) -> None:
        self.response = response
        self.requests: list[object] = []

    async def __call__(self, request: object) -> object:
        self.requests.append(request)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class _SyncSession:
    def __init__(self, input_entity: object) -> None:
        self.input_entity = input_entity
        self.calls: list[int] = []

    def get_input_entity(self, channel_id: int) -> object:
        self.calls.append(channel_id)
        return self.input_entity


class _AsyncSession:
    def __init__(self, input_entity: object) -> None:
        self.input_entity = input_entity
        self.calls = 0

    async def get_input_entity(self, _channel_id: int) -> object:
        self.calls += 1
        return self.input_entity


class _AsyncResolverProbe:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *_args: object, **_kwargs: object) -> object:
        self.calls += 1
        return None


class _ReferenceClient(_Client):
    def __init__(self, session: object) -> None:
        super().__init__(object())
        self.session = session
        self.get_input_entity = _AsyncResolverProbe()
        self.get_entity = _AsyncResolverProbe()


def test_channel_reference_provider_uses_only_sync_session_cache() -> None:
    session = _SyncSession(types.InputPeerChannel(channel_id=123, access_hash=0))
    client = _ReferenceClient(session)

    reference = TelethonChannelProfileGateway(client).get_channel_reference(-1000000000123)

    assert reference == ChannelReference(-1000000000123, 0)
    assert session.calls == [-1000000000123]
    assert client.get_input_entity.calls == 0
    assert client.get_entity.calls == 0


@pytest.mark.parametrize(
    "input_entity",
    (
        None,
        types.InputPeerUser(user_id=123, access_hash=0),
        types.InputPeerChannel(channel_id=124, access_hash=0),
    ),
)
def test_channel_reference_provider_returns_none_for_unusable_or_mismatched_cache_entry(
    input_entity: object,
) -> None:
    session = _SyncSession(input_entity)
    client = _ReferenceClient(session)
    assert TelethonChannelProfileGateway(client).get_channel_reference(-1000000000123) is None


def test_channel_reference_provider_rejects_noncanonical_id_and_async_session_getter() -> None:
    session = _AsyncSession(types.InputPeerChannel(channel_id=123, access_hash=0))
    client = _ReferenceClient(session)
    assert TelethonChannelProfileGateway(client).get_channel_reference(-123) is None
    assert TelethonChannelProfileGateway(client).get_channel_reference(-1000000000123) is None
    assert session.calls == 0


def _full_channel(*, channel_id: int = 123, channel_date: datetime | None = None) -> types.messages.ChatFull:
    full = types.ChannelFull(
        id=channel_id,
        about="channel about",
        read_inbox_max_id=0,
        read_outbox_max_id=0,
        unread_count=0,
        chat_photo=types.PhotoEmpty(id=55),
        notify_settings=types.PeerNotifySettings(),
        bot_info=[],
        pts=1,
        participants_count=42,
        linked_chat_id=200,
        pinned_msg_id=9,
        slowmode_seconds=60,
        available_reactions=types.ChatReactionsSome([types.ReactionEmoji("👍")]),
    )
    channel = types.Channel(
        id=channel_id,
        title="Channel",
        photo=types.ChatPhotoEmpty(),
        date=channel_date,
        megagroup=True,
    )
    return types.messages.ChatFull(full_chat=full, chats=[channel], users=[])


def _reference(*, channel_id: int = -1000000000123, access_hash: int = 0) -> ChannelReference:
    return ChannelReference(channel_id=channel_id, access_hash=access_hash)


@pytest.mark.parametrize(
    "channel_id",
    (0, -123, -1000000000000),
)
def test_channel_reference_requires_marked_channel_id(channel_id: int) -> None:
    with pytest.raises(ValueError, match="canonical marked"):
        ChannelReference(channel_id, 0)


@pytest.mark.parametrize("access_hash", (-(2**63) - 1, 2**63))
def test_channel_reference_requires_signed_64_bit_access_hash(access_hash: int) -> None:
    with pytest.raises(ValueError, match="signed 64-bit"):
        ChannelReference(-1000000000123, access_hash)


@pytest.mark.asyncio
async def test_profile_gateway_normalizes_one_full_channel_rpc() -> None:
    client = _Client(_full_channel())
    gateway = TelethonChannelProfileGateway(client, now_provider=iter((100.25, 101.5)).__next__)

    observation = await gateway.fetch_channel_profile(_reference(access_hash=7))

    assert len(client.requests) == 1
    assert isinstance(client.requests[0], GetFullChannelRequest)
    assert isinstance(client.requests[0].channel, types.InputChannel)
    assert client.requests[0].channel.channel_id == 123
    assert client.requests[0].channel.access_hash == 7
    assert observation.channel_id == -1000000000123
    assert observation.about == "channel about"
    assert observation.participants_count == 42
    assert observation.linked_chat_id == -1000000000200
    assert observation.pinned_msg_id == 9
    assert observation.slow_mode_seconds == 60
    assert observation.available_reactions == {"kind": "some", "emojis": ["👍"]}
    assert observation.current_photo is not None
    assert observation.current_photo.photo_id == 55
    assert (observation.observation_started_at, observation.observation_completed_at) == (100.25, 101.5)
    assert observation.created is None
    assert not hasattr(observation, "full_chat")


@pytest.mark.asyncio
async def test_profile_gateway_captures_channel_creation_date() -> None:
    created = datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)
    observation = await TelethonChannelProfileGateway(
        _Client(_full_channel(channel_date=created))
    ).fetch_channel_profile(_reference(access_hash=7))

    assert observation.created == int(created.timestamp())


@pytest.mark.asyncio
async def test_contact_gateway_uses_one_bounded_contacts_request_and_deduplicates_ids() -> None:
    result = types.channels.ChannelParticipants(
        count=3,
        participants=[],
        chats=[],
        users=cast(
            list[types.TypeUser],
            [SimpleNamespace(id=7), SimpleNamespace(id=7), SimpleNamespace(id=9), SimpleNamespace(id=-1)],
        ),
    )
    client = _Client(result)
    gateway = TelethonChannelProfileGateway(client, now_provider=iter((100.0, 100.5)).__next__)

    observation = await gateway.fetch_channel_contact_overlap(_reference())

    assert len(client.requests) == 1
    request = client.requests[0]
    assert isinstance(request, GetParticipantsRequest)
    assert isinstance(request.channel, types.InputChannel)
    assert request.channel.channel_id == 123
    assert request.channel.access_hash == 0
    assert isinstance(request.filter, types.ChannelParticipantsContacts)
    assert request.filter.q == ""
    assert observation.contact_ids == (7, 9)
    assert observation.status is ProjectionStatus.PARTIAL
    assert observation.reason == "bounded_contacts_page"
    assert (observation.observation_started_at, observation.observation_completed_at) == (100.0, 100.5)


@pytest.mark.asyncio
async def test_contact_permission_error_is_unavailable_and_other_errors_propagate() -> None:
    permission = TelethonChannelProfileGateway(
        _Client(ChatAdminRequiredError(request=None)), now_provider=lambda: 100.0
    )
    observation = await permission.fetch_channel_contact_overlap(_reference())
    assert observation.status is ProjectionStatus.UNAVAILABLE
    assert observation.contact_ids is None
    assert observation.reason == "not_an_admin"

    with pytest.raises(RuntimeError, match="temporary"):
        await TelethonChannelProfileGateway(_Client(RuntimeError("temporary"))).fetch_channel_contact_overlap(
            _reference()
        )


@pytest.mark.asyncio
async def test_profile_gateway_rejects_invalid_target_or_envelope_before_reusable_result() -> None:
    client = _Client(object())
    gateway = TelethonChannelProfileGateway(client, now_provider=lambda: 100.0)
    with pytest.raises(TypeError):
        await gateway.fetch_channel_profile(0)  # type: ignore[arg-type]
    assert client.requests == []
    with pytest.raises(ValueError, match="envelope"):
        await gateway.fetch_channel_profile(_reference())


@pytest.mark.asyncio
async def test_profile_gateway_normalizes_photo_date() -> None:
    photo = types.Photo(
        id=55,
        access_hash=1,
        file_reference=b"ref",
        date=datetime(2026, 9, 16, 12, 0, tzinfo=UTC),
        sizes=[],
        dc_id=1,
    )
    response = _full_channel()
    object.__setattr__(response.full_chat, "chat_photo", photo)
    observation = await TelethonChannelProfileGateway(
        _Client(response), now_provider=lambda: 100.0
    ).fetch_channel_profile(_reference())
    assert observation.current_photo is not None
    assert observation.current_photo.date == "2026-09-16T12:00:00+00:00"


@pytest.mark.asyncio
async def test_profile_gateway_unknown_optional_subtypes_are_safe() -> None:
    response = _full_channel()
    object.__setattr__(response.full_chat, "available_reactions", object())
    object.__setattr__(response.full_chat, "chat_photo", object())

    observation = await TelethonChannelProfileGateway(
        _Client(response), now_provider=lambda: 100.0
    ).fetch_channel_profile(_reference())

    assert observation.available_reactions == {"kind": "unknown", "emojis": []}
    assert observation.current_photo is None


@pytest.mark.parametrize("linked_chat_id", (-1, "200"))
async def test_profile_gateway_rejects_malformed_linked_channel_id(linked_chat_id: object) -> None:
    response = _full_channel()
    object.__setattr__(response.full_chat, "linked_chat_id", linked_chat_id)
    with pytest.raises(ValueError, match="linked channel id"):
        await TelethonChannelProfileGateway(_Client(response), now_provider=lambda: 100.0).fetch_channel_profile(
            _reference()
        )


@pytest.mark.asyncio
async def test_profile_gateway_rejects_malformed_known_photo_subtype() -> None:
    response = _full_channel()
    malformed = types.Photo(
        id=0,
        access_hash=1,
        file_reference=b"ref",
        date=datetime(2026, 9, 16, tzinfo=UTC),
        sizes=[],
        dc_id=1,
    )
    object.__setattr__(response.full_chat, "chat_photo", malformed)

    with pytest.raises(ValueError, match="current photo id"):
        await TelethonChannelProfileGateway(_Client(response), now_provider=lambda: 100.0).fetch_channel_profile(
            _reference()
        )
