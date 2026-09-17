"""Contract tests for the user profile Telethon adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telethon.tl import types  # type: ignore[import-untyped]
from telethon.tl.functions.users import GetFullUserRequest

from mcp_telegram.entity_profile.contracts import (
    PersonalChannelPost,
    PersonalChannelReference,
    ProjectionStatus,
    TargetKind,
)
from mcp_telegram.models import DialogType
from mcp_telegram.telegram_gateway import TelethonUserProfileGateway
from mcp_telegram.telethon_dialog import classify_dialog_type


class _Client:
    def __init__(self, response: object) -> None:
        self.response = response
        self.requests: list[object] = []
        self.message_entity: object | None = None
        self.message_ids: list[int] = []
        self.message: object | None = None

    async def __call__(self, request: object) -> object:
        self.requests.append(request)
        return self.response

    async def get_messages(self, entity: object, ids: list[int]) -> object:
        self.message_entity = entity
        self.message_ids = ids
        return self.message


def _response(*, user_id: int = 42, bot: bool = False) -> object:
    return SimpleNamespace(
        full_user=SimpleNamespace(about="about", personal_channel_id=777, personal_channel_message=9),
        users=[SimpleNamespace(id=user_id, bot=bot, first_name="Target")],
        chats=[SimpleNamespace(id=777, title="Channel", username="channel")],
    )


@pytest.mark.asyncio
async def test_gateway_fetches_one_request_and_returns_both_projections() -> None:
    client = _Client(_response())
    gateway = TelethonUserProfileGateway(client, now_provider=iter((100.25, 101.5)).__next__)

    observation = await gateway.fetch_user_profile(42, TargetKind.USER)

    assert len(client.requests) == 1
    assert isinstance(client.requests[0], GetFullUserRequest)
    assert client.requests[0].id == 42
    assert observation.target_id == 42
    assert observation.target_kind is TargetKind.USER
    assert observation.full_profile.status is ProjectionStatus.USABLE
    assert observation.personal_channel.status is ProjectionStatus.USABLE
    assert observation.full_profile.provenance is not None
    boundary = observation.full_profile.provenance.observation
    assert (boundary.started_at, boundary.completed_at) == (100.25, 101.5)
    assert observation.personal_channel.payload == {
        "personal_channel_id": 777,
        "personal_channel_message": 9,
        "title": "Channel",
        "username": "channel",
    }


@pytest.mark.asyncio
async def test_gateway_accepts_replies_bot_as_service_full_profile() -> None:
    user = types.User(id=42, bot=True, first_name="Replies", username="replies")
    response = SimpleNamespace(
        full_user=SimpleNamespace(about="service profile", personal_channel_id=None),
        users=[user],
        chats=[],
    )

    observation = await TelethonUserProfileGateway(_Client(response), now_provider=lambda: 100.0).fetch_user_profile(
        42, TargetKind.BOT
    )

    assert classify_dialog_type(user) is DialogType.SERVICE
    assert observation.full_profile.status is ProjectionStatus.USABLE
    assert observation.full_profile.payload is not None
    assert observation.full_profile.payload["about"] == "service profile"
    assert observation.full_profile.payload["bot"] is True
    assert observation.personal_channel.status is ProjectionStatus.ABSENT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    (
        object(),
        SimpleNamespace(full_user=SimpleNamespace(about="about"), users=None),
        _response(user_id=43),
        _response(bot=True),
    ),
)
async def test_gateway_invalid_envelope_has_no_reusable_provenance(response: object) -> None:
    gateway = TelethonUserProfileGateway(_Client(response), now_provider=lambda: 100.0)

    observation = await gateway.fetch_user_profile(42, TargetKind.USER)

    assert observation.full_profile.status is ProjectionStatus.UNAVAILABLE
    assert observation.personal_channel.status is ProjectionStatus.UNAVAILABLE
    assert observation.full_profile.provenance is None
    assert observation.personal_channel.provenance is None


@pytest.mark.asyncio
async def test_gateway_rejects_invalid_target_before_rpc() -> None:
    client = _Client(_response())
    gateway = TelethonUserProfileGateway(client)

    with pytest.raises(ValueError, match="positive integer"):
        await gateway.fetch_user_profile(0, TargetKind.USER)
    with pytest.raises(ValueError, match="target kind"):
        await gateway.fetch_user_profile(42, "channel")  # type: ignore[arg-type]
    assert client.requests == []


def _channel(*, channel_id: int = 777, access_hash: object = -123, min: bool | None = None) -> types.Channel:
    return types.Channel(
        id=channel_id,
        title="Channel",
        photo=types.ChatPhotoEmpty(),
        date=datetime(2026, 1, 1, tzinfo=UTC),
        access_hash=access_hash if isinstance(access_hash, int) else None,
        username="channel",
        min=min,
    )


@pytest.mark.asyncio
async def test_gateway_keeps_matching_negative_access_hash_as_private_capability() -> None:
    response = _response()
    response.chats = [_channel(access_hash=-123)]  # type: ignore[attr-defined]
    client = _Client(response)
    client.message = types.Message(
        id=9,
        peer_id=types.PeerChannel(777),
        date=datetime(2026, 1, 2, tzinfo=UTC),
        message="attached post",
    )
    gateway = TelethonUserProfileGateway(client, now_provider=lambda: 100.0)

    observation = await gateway.fetch_user_profile(42, TargetKind.USER)
    assert observation.personal_channel_reference == PersonalChannelReference(777, -123)

    post = await gateway.fetch_personal_channel_post(observation.personal_channel_reference, 9)  # type: ignore[arg-type]
    assert post == PersonalChannelPost(9, 1767312000, "attached post")
    assert isinstance(client.message_entity, types.InputPeerChannel)
    assert client.message_entity.channel_id == 777
    assert client.message_entity.access_hash == -123
    assert client.message_ids == [9]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chat",
    (
        None,
        _channel(channel_id=778),
        _channel(access_hash="bad"),
        _channel(access_hash=True),
        _channel(access_hash=2**63),
        _channel(min=True),
    ),
)
async def test_gateway_drops_absent_malformed_mismatched_or_min_channel_capability(
    chat: object | None,
) -> None:
    response = _response()
    response.chats = [] if chat is None else [chat]  # type: ignore[attr-defined]

    observation = await TelethonUserProfileGateway(_Client(response), now_provider=lambda: 100.0).fetch_user_profile(
        42, TargetKind.USER
    )

    assert observation.personal_channel_reference is None
