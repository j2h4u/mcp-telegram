"""Telethon-only gateway helpers for reading adapters."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Protocol, cast

from telethon.tl import types  # type: ignore[import-untyped]
from telethon.tl.functions.messages import (
    GetFullChatRequest,  # type: ignore[import-untyped]
    GetScheduledHistoryRequest,  # type: ignore[import-untyped]
)
from telethon.tl.functions.users import GetFullUserRequest  # type: ignore[import-untyped]
from telethon.tl.types import TypeInputPeer, TypeInputUser  # type: ignore[import-untyped]
from telethon.utils import get_peer_id  # type: ignore[import-untyped]

from .entity_profile.contracts import (
    GroupCurrentPhoto,
    GroupProfileObservation,
    ObservationBoundary,
    PersonalChannelPost,
    PersonalChannelReference,
    TargetKind,
    UserProfileObservation,
)
from .entity_profile.full_user_normalization import normalize_full_user_response
from .entity_profile.ports import GroupProfilePort, UserProfilePort
from .flood import TelegramRpcThrottled
from .telegram_access import ACCESS_LOST_ERRORS
from .telegram_reading import GatewayFailure, GatewayFailureKind
from .telegram_rpc_scheduler import RpcAdmissionClosedError, RpcAdmissionError, UnclassifiedTelegramRpcError

CATCHABLE_GATEWAY_FAILURES = (Exception,)


class ScheduledHistoryClient(Protocol):
    async def get_input_entity(self, _dialog_id: int) -> object: ...

    async def __call__(self, _request: object, **_kwargs: object) -> object: ...


class GroupProfileClient(Protocol):
    async def __call__(self, _request: object, **_kwargs: object) -> object: ...


class UserProfileClient(Protocol):
    async def __call__(self, _request: object, **_kwargs: object) -> object: ...

    async def get_messages(self, _entity: object, ids: list[int]) -> object: ...


class TelethonGroupProfileGateway(GroupProfilePort):
    """Normalize ``messages.GetFullChat`` at the Telethon boundary."""

    def __init__(self, client: object, *, now_provider: Callable[[], float] | None = None) -> None:
        self._client = cast(GroupProfileClient, client)
        self._now_provider = now_provider or time.time

    async def fetch_group_profile(self, group_id: int) -> GroupProfileObservation:
        raw_group_id = _raw_group_id(group_id)
        started_at = int(self._now_provider())
        result = await self._client(GetFullChatRequest(chat_id=raw_group_id))
        completed_at = int(self._now_provider())
        full_chat = _validated_full_chat(result, raw_group_id)
        participant_ids, participants_reason = _normalize_participants(
            getattr(full_chat, "participants", None), raw_group_id
        )
        current_photo = _normalize_current_photo(getattr(full_chat, "chat_photo", None))
        return GroupProfileObservation(
            group_id=group_id,
            about=_optional_string(getattr(full_chat, "about", None)),
            invite_link=_normalize_invite_link(getattr(full_chat, "exported_invite", None)),
            participant_ids=participant_ids,
            participants_unavailable_reason=participants_reason,
            current_photo=current_photo,
            observation_started_at=started_at,
            observation_completed_at=completed_at,
        )


class TelethonUserProfileGateway(UserProfilePort):
    """Normalize ``users.GetFullUser`` at the Telethon boundary."""

    def __init__(self, client: object, *, now_provider: Callable[[], float] | None = None) -> None:
        self._client = cast(UserProfileClient, client)
        self._now_provider = now_provider or time.time

    async def fetch_user_profile(self, user_id: int, target_kind: TargetKind) -> UserProfileObservation:
        if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
            raise ValueError("user target id must be a positive integer")
        try:
            normalized_kind = TargetKind(target_kind)
        except (TypeError, ValueError) as exc:
            raise ValueError("user target kind must be 'user' or 'bot'") from exc
        started_at = self._now_provider()
        result = await self._client(GetFullUserRequest(id=cast(TypeInputUser, user_id)))
        completed_at = self._now_provider()
        observation = normalize_full_user_response(
            result,
            target_id=user_id,
            target_kind=normalized_kind,
            observation=ObservationBoundary(started_at=started_at, completed_at=completed_at),
        )
        reference = _personal_channel_reference(result, observation.personal_channel.payload)
        return replace(observation, personal_channel_reference=reference)

    async def fetch_personal_channel_post(
        self, reference: PersonalChannelReference, message_id: int
    ) -> PersonalChannelPost | None:
        message_id = _validated_personal_channel_message_id(message_id)
        peer = _personal_channel_peer(reference)
        fetched = await self._client.get_messages(peer, ids=[message_id])
        return _normalize_personal_channel_post(_first_message(fetched), message_id)


def _raw_group_id(group_id: int) -> int:
    if not isinstance(group_id, int) or isinstance(group_id, bool) or group_id == 0:
        raise ValueError("legacy group target id is invalid")
    return -group_id if group_id < 0 else group_id


def _validated_full_chat(result: object, raw_group_id: int) -> types.ChatFull:
    if not isinstance(result, types.messages.ChatFull):
        raise ValueError("full chat envelope is invalid")
    full_chat = getattr(result, "full_chat", None)
    if not isinstance(full_chat, types.ChatFull):
        raise ValueError("legacy chat full payload is invalid")
    if _positive_id(getattr(full_chat, "id", None)) != raw_group_id:
        raise ValueError("legacy chat full target does not match")
    return full_chat


def _normalize_invite_link(invite: object) -> str | None:
    link = getattr(cast(object, invite), "link", None) if invite is not None else None
    return link if isinstance(link, str) else None


def _positive_id(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _signed_64(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or not -(2**63) <= value <= (2**63 - 1):
        return None
    return value


def _first_message(value: object) -> object | None:
    if value is None:
        return None
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return value[0] if value else None
    return value


def _validated_personal_channel_message_id(message_id: object) -> int:
    if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
        raise ValueError("personal channel message id must be positive")
    return message_id


def _personal_channel_peer(reference: object) -> types.InputPeerChannel:
    if not isinstance(reference, PersonalChannelReference):
        raise TypeError("personal channel reference is invalid")
    return types.InputPeerChannel(channel_id=reference.channel_id, access_hash=reference.access_hash)


def _normalize_personal_channel_post(message: object | None, message_id: int) -> PersonalChannelPost | None:
    if message is None:
        return None
    raw_message_id = getattr(message, "id", message_id)
    if not isinstance(raw_message_id, int) or isinstance(raw_message_id, bool) or raw_message_id != message_id:
        return None
    raw_date = getattr(message, "date", None)
    sent_at = int(raw_date.timestamp()) if isinstance(raw_date, datetime) else None
    raw_text = getattr(message, "message", None)
    return PersonalChannelPost(
        message_id=message_id, sent_at=sent_at, text=raw_text if isinstance(raw_text, str) else None
    )


def _personal_channel_reference(
    result: object, personal_channel: Mapping[str, object] | None
) -> PersonalChannelReference | None:
    if personal_channel is None:
        return None
    channel_id = _positive_id(personal_channel.get("personal_channel_id"))
    if channel_id is None:
        return None
    chats = getattr(result, "chats", None)
    if not isinstance(chats, Sequence) or isinstance(chats, str | bytes | bytearray):
        return None
    for chat in chats:
        if not isinstance(chat, types.Channel) or bool(getattr(chat, "min", False)):
            continue
        if _positive_id(getattr(chat, "id", None)) != channel_id:
            continue
        access_hash = _signed_64(getattr(chat, "access_hash", None))
        if access_hash is None:
            return None
        return PersonalChannelReference(channel_id=channel_id, access_hash=access_hash)
    return None


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _normalize_participants(  # noqa: PLR0911
    participants: object,
    raw_group_id: int,
) -> tuple[tuple[int, ...] | None, str | None]:
    if isinstance(participants, types.ChatParticipantsForbidden):
        return None, "participants_forbidden"
    if participants is None:
        return None, "participants_missing"
    if not isinstance(participants, types.ChatParticipants):
        return None, "participants_malformed"
    if _positive_id(getattr(participants, "chat_id", None)) != raw_group_id:
        return None, "participants_target_mismatch"
    raw_items = getattr(participants, "participants", None)
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, str | bytes | bytearray):
        return None, "participants_not_sequence"
    participant_types = (types.ChatParticipant, types.ChatParticipantAdmin, types.ChatParticipantCreator)
    ids: list[int] = []
    for item in raw_items:
        if not isinstance(item, participant_types):
            return None, "participants_malformed"
        user_id = _positive_id(getattr(item, "user_id", None))
        if user_id is None:
            return None, "participants_malformed"
        ids.append(user_id)
    return tuple(ids), None


def _normalize_current_photo(photo: object) -> GroupCurrentPhoto | None:
    if photo is None:
        return None
    if not isinstance(photo, (types.Photo, types.PhotoEmpty)):
        raise ValueError("legacy group current photo is invalid")
    if isinstance(photo, types.PhotoEmpty) and getattr(photo, "id", None) == 0:
        return None
    photo_id = _positive_id(getattr(photo, "id", None))
    if photo_id is None:
        raise ValueError("legacy group current photo id is invalid")
    raw_date = getattr(photo, "date", None)
    date = raw_date.isoformat() if isinstance(raw_date, datetime) else None
    return GroupCurrentPhoto(photo_id=photo_id, date=date)


def translate_gateway_failure(exc: BaseException) -> GatewayFailure:
    """Translate Telegram exceptions at the integration boundary."""
    if isinstance(exc, RpcAdmissionClosedError):
        raise exc
    if isinstance(exc, (RpcAdmissionError, UnclassifiedTelegramRpcError)):
        return GatewayFailure(
            GatewayFailureKind.TRANSIENT,
            "TelegramUnavailable",
            "Telegram is temporarily unavailable; retry later",
            True,
        )
    if isinstance(exc, TelegramRpcThrottled):
        return GatewayFailure(
            GatewayFailureKind.FLOOD_WAIT,
            TelegramRpcThrottled.__name__,
            "Telegram RPC throttled",
            not exc.latched,
            exc.retry_after_seconds,
        )
    message = str(exc).replace("\n", "\\n") or type(exc).__name__
    if isinstance(exc, ACCESS_LOST_ERRORS):
        return GatewayFailure(GatewayFailureKind.ACCESS_LOST, type(exc).__name__, message, False)
    if isinstance(exc, ValueError):
        return GatewayFailure(GatewayFailureKind.INVALID_TARGET, type(exc).__name__, message, False)
    return GatewayFailure(GatewayFailureKind.TRANSIENT, type(exc).__name__, message, True)


async def fetch_scheduled_history_snapshot(
    client: ScheduledHistoryClient,
    dialog_id: int,
) -> list[object]:
    """Fetch one scheduled queue snapshot through Telethon.

    The daemon-owned TelegramRpcGate configures Telethon's threshold to zero,
    so the gate owns account-wide flood admission and observation.
    The scheduled reconciliation caller owns the ``SCHEDULED_MESSAGES`` scope;
    this shared adapter deliberately inherits it.
    """
    input_entity = cast(TypeInputPeer, await client.get_input_entity(dialog_id))
    result = await client(GetScheduledHistoryRequest(peer=input_entity, hash=0))
    messages = list(cast(Sequence[object], getattr(result, "messages", ()) or ()))
    entities = {
        get_peer_id(entity): entity
        for entity in [
            *cast(Sequence[object], getattr(result, "users", ()) or ()),
            *cast(Sequence[object], getattr(result, "chats", ()) or ()),
        ]
    }
    for message in messages:
        finish_init = getattr(message, "_finish_init", None)
        if callable(finish_init):
            finish_init(client, entities, input_entity)
    return messages
