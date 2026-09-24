"""Telethon-only gateway helpers for reading adapters."""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Protocol, cast

from telethon.errors import ChatAdminRequiredError  # type: ignore[import-untyped]
from telethon.tl import types  # type: ignore[import-untyped]
from telethon.tl.functions.channels import (  # type: ignore[import-untyped]
    GetFullChannelRequest,
    GetParticipantsRequest,
)
from telethon.tl.functions.messages import (
    GetCommonChatsRequest,
    GetFullChatRequest,  # type: ignore[import-untyped]
    GetScheduledHistoryRequest,  # type: ignore[import-untyped]
    SearchRequest,
)
from telethon.tl.functions.photos import GetUserPhotosRequest  # type: ignore[import-untyped]
from telethon.tl.functions.users import GetFullUserRequest  # type: ignore[import-untyped]
from telethon.tl.types import (  # type: ignore[import-untyped]
    ChannelParticipantsContacts,
    TypeInputPeer,
    TypeInputUser,
)
from telethon.utils import get_peer_id  # type: ignore[import-untyped]

from .entity_profile.contracts import (
    CHANNEL_ID_MARKER,
    ChannelContactOverlapObservation,
    ChannelProfileObservation,
    ChannelReference,
    ChatAvatarHistoryObservation,
    ChatAvatarReference,
    ChatCurrentPhoto,
    CommonChatsObservation,
    CommonChatSummary,
    GroupProfileObservation,
    GroupReference,
    ObservationBoundary,
    PersonalChannelPost,
    PersonalChannelReference,
    ProjectionStatus,
    TargetKind,
    UserAvatarHistoryObservation,
    UserProfileObservation,
    UserReference,
)
from .entity_profile.full_user_normalization import normalize_full_user_response
from .entity_profile.ports import (
    ChannelProfilePort,
    ChatAvatarHistoryPort,
    CommonChatsPort,
    GroupProfilePort,
    UserAvatarHistoryPort,
    UserProfilePort,
)
from .flood import TelegramRpcThrottled
from .telegram_access import ACCESS_LOST_ERRORS
from .telegram_reading import GatewayFailure, GatewayFailureKind
from .telegram_rpc_error import is_reaction_detail_terminal_rpc_error
from .telegram_rpc_scheduler import RpcAdmissionClosedError, RpcAdmissionError, UnclassifiedTelegramRpcError

CATCHABLE_GATEWAY_FAILURES = (Exception,)


class ScheduledHistoryClient(Protocol):
    async def get_input_entity(self, _dialog_id: int, /) -> object: ...

    async def __call__(self, _request: object, **_kwargs: object) -> object: ...


class GroupProfileClient(Protocol):
    async def __call__(self, _request: object, **_kwargs: object) -> object: ...


class ChannelProfileClient(Protocol):
    async def __call__(self, _request: object, **_kwargs: object) -> object: ...


class UserProfileClient(Protocol):
    async def __call__(self, _request: object, **_kwargs: object) -> object: ...

    async def get_messages(self, _entity: object, ids: list[int]) -> object: ...


class TelethonChannelProfileGateway(ChannelProfilePort):
    """Normalize channel profile capabilities at the Telethon boundary."""

    def __init__(self, client: object, *, now_provider: Callable[[], float] | None = None) -> None:
        self._client = cast(ChannelProfileClient, client)
        self._now_provider = now_provider or time.time

    def get_channel_reference(self, channel_id: int) -> ChannelReference | None:
        if not _is_canonical_channel_id(channel_id):
            return None
        session = getattr(self._client, "session", None)
        getter = getattr(session, "get_input_entity", None)
        if not callable(getter):
            return None
        if inspect.iscoroutinefunction(getter):
            return None
        try:
            input_entity = getter(channel_id)
        except AttributeError, KeyError, TypeError, ValueError:
            return None
        return _channel_reference_from_session_entity(input_entity, channel_id)

    async def fetch_channel_profile(self, reference: ChannelReference) -> ChannelProfileObservation:
        _validate_channel_reference(reference)
        channel_id = reference.channel_id
        started_at = self._now_provider()
        try:
            result = await self._client(GetFullChannelRequest(channel=_input_channel(reference)))
            full_chat = _validated_full_channel(result, channel_id)
            created = _channel_created(result, channel_id)
            completed_at = self._now_provider()
        except ACCESS_LOST_ERRORS:
            completed_at = self._now_provider()
            return _unavailable_channel_profile(channel_id, started_at, completed_at, "access_lost")
        except ChatAdminRequiredError:
            completed_at = self._now_provider()
            return _unavailable_channel_profile(channel_id, started_at, completed_at, "not_an_admin")
        return ChannelProfileObservation(
            channel_id=channel_id,
            about=_optional_string(getattr(full_chat, "about", None)),
            participants_count=_nonnegative_int(getattr(full_chat, "participants_count", None)),
            linked_chat_id=_normalize_linked_channel_id(getattr(full_chat, "linked_chat_id", None)),
            pinned_msg_id=_nonnegative_int(getattr(full_chat, "pinned_msg_id", None)),
            slow_mode_seconds=_nonnegative_int(getattr(full_chat, "slowmode_seconds", None)),
            available_reactions=_normalize_reactions(getattr(full_chat, "available_reactions", None)),
            current_photo=_normalize_channel_photo(getattr(full_chat, "chat_photo", None)),
            observation_started_at=started_at,
            observation_completed_at=completed_at,
            created=created,
        )

    async def fetch_channel_contact_overlap(self, reference: ChannelReference) -> ChannelContactOverlapObservation:
        _validate_channel_reference(reference)
        channel_id = reference.channel_id
        started_at = self._now_provider()
        try:
            result = await self._client(
                GetParticipantsRequest(
                    channel=_input_channel(reference),
                    filter=ChannelParticipantsContacts(q=""),
                    offset=0,
                    limit=200,
                    hash=0,
                )
            )
            contact_ids = _validated_contact_ids(result)
            completed_at = self._now_provider()
        except ACCESS_LOST_ERRORS:
            completed_at = self._now_provider()
            return ChannelContactOverlapObservation(
                channel_id=channel_id,
                contact_ids=None,
                status=ProjectionStatus.UNAVAILABLE,
                reason="access_lost",
                observation_started_at=started_at,
                observation_completed_at=completed_at,
            )
        except ChatAdminRequiredError:
            completed_at = self._now_provider()
            return ChannelContactOverlapObservation(
                channel_id=channel_id,
                contact_ids=None,
                status=ProjectionStatus.UNAVAILABLE,
                reason="not_an_admin",
                observation_started_at=started_at,
                observation_completed_at=completed_at,
            )
        return ChannelContactOverlapObservation(
            channel_id=channel_id,
            contact_ids=contact_ids,
            status=ProjectionStatus.PARTIAL,
            reason="bounded_contacts_page",
            observation_started_at=started_at,
            observation_completed_at=completed_at,
        )


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
        created = _group_created(result, raw_group_id)
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
            created=created,
            observation_started_at=started_at,
            observation_completed_at=completed_at,
        )


class TelethonCommonChatsGateway(CommonChatsPort):
    """Normalize one bounded ``messages.GetCommonChats`` page."""

    def __init__(self, client: object, *, now_provider: Callable[[], float] | None = None) -> None:
        self._client = cast(GroupProfileClient, client)
        self._now_provider = now_provider or time.time

    async def fetch_common_chats(self, reference: UserReference) -> CommonChatsObservation:
        _validate_user_reference(reference)
        started_at = self._now_provider()
        try:
            result = await self._client(GetCommonChatsRequest(user_id=_input_user(reference), max_id=0, limit=100))
        except ACCESS_LOST_ERRORS:
            completed_at = self._now_provider()
            return CommonChatsObservation(
                reference.user_id, (), 0, ProjectionStatus.UNAVAILABLE, "access_lost", started_at, completed_at
            )
        except ChatAdminRequiredError:
            completed_at = self._now_provider()
            return CommonChatsObservation(
                reference.user_id, (), 0, ProjectionStatus.UNAVAILABLE, "not_an_admin", started_at, completed_at
            )
        chats, reported_count, status = _normalize_common_chats(result)
        completed_at = self._now_provider()
        return CommonChatsObservation(
            user_id=reference.user_id,
            chats=chats,
            reported_count=reported_count,
            status=status,
            reason="bounded_page" if status is ProjectionStatus.PARTIAL else None,
            observation_started_at=started_at,
            observation_completed_at=completed_at,
        )


class TelethonUserAvatarHistoryGateway(UserAvatarHistoryPort):
    """Normalize one bounded ``photos.GetUserPhotos`` page."""

    def __init__(self, client: object, *, now_provider: Callable[[], float] | None = None) -> None:
        self._client = cast(GroupProfileClient, client)
        self._now_provider = now_provider or time.time

    async def fetch_user_avatar_history(self, reference: UserReference) -> UserAvatarHistoryObservation:
        _validate_user_reference(reference)
        started_at = self._now_provider()
        try:
            result = await self._client(
                GetUserPhotosRequest(
                    user_id=_input_user(reference),
                    offset=0,
                    max_id=0,
                    limit=100,
                )
            )
        except ACCESS_LOST_ERRORS:
            completed_at = self._now_provider()
            return UserAvatarHistoryObservation(
                reference.user_id, (), 0, ProjectionStatus.UNAVAILABLE, "access_lost", started_at, completed_at
            )
        except ChatAdminRequiredError:
            completed_at = self._now_provider()
            return UserAvatarHistoryObservation(
                reference.user_id, (), 0, ProjectionStatus.UNAVAILABLE, "not_an_admin", started_at, completed_at
            )
        photos, reported_count, status = _normalize_user_photos(result, reference.user_id)
        completed_at = self._now_provider()
        return UserAvatarHistoryObservation(
            user_id=reference.user_id,
            photos=photos,
            reported_count=reported_count,
            status=status,
            reason="bounded_page" if status is ProjectionStatus.PARTIAL else None,
            observation_started_at=started_at,
            observation_completed_at=completed_at,
        )


class TelethonChatAvatarHistoryGateway(ChatAvatarHistoryPort):
    """Normalize one bounded chat-photo message search page."""

    def __init__(self, client: object, *, now_provider: Callable[[], float] | None = None) -> None:
        self._client = cast(GroupProfileClient, client)
        self._now_provider = now_provider or time.time

    def get_chat_avatar_reference(self, entity_id: int) -> ChatAvatarReference | None:
        return _chat_avatar_reference_from_peer(_session_entity(self._client, entity_id), entity_id)

    async def fetch_chat_avatar_history(self, reference: ChatAvatarReference) -> ChatAvatarHistoryObservation:
        if not isinstance(reference, (ChannelReference, GroupReference)):
            raise TypeError("chat avatar reference is invalid")
        started_at = self._now_provider()
        peer: object = (
            _input_channel(reference)
            if isinstance(reference, ChannelReference)
            else types.InputPeerChat(chat_id=abs(reference.group_id))
        )
        try:
            result = await self._client(
                SearchRequest(
                    peer=cast(TypeInputPeer, peer),
                    q="",
                    filter=types.InputMessagesFilterChatPhotos(),
                    min_date=None,
                    max_date=None,
                    offset_id=0,
                    add_offset=0,
                    limit=100,
                    max_id=0,
                    min_id=0,
                    hash=0,
                    from_id=None,
                )
            )
            photos, reported_count, status = _normalize_chat_photos(result, reference)
            completed_at = self._now_provider()
        except ACCESS_LOST_ERRORS:
            completed_at = self._now_provider()
            return ChatAvatarHistoryObservation(
                reference, (), 0, ProjectionStatus.UNAVAILABLE, "access_lost", started_at, completed_at
            )
        except ChatAdminRequiredError:
            completed_at = self._now_provider()
            return ChatAvatarHistoryObservation(
                reference, (), 0, ProjectionStatus.UNAVAILABLE, "not_an_admin", started_at, completed_at
            )
        return ChatAvatarHistoryObservation(
            reference,
            photos,
            reported_count,
            status,
            "bounded_page" if status is ProjectionStatus.PARTIAL else None,
            started_at,
            completed_at,
        )


class TelethonUserProfileGateway(UserProfilePort):
    """Normalize ``users.GetFullUser`` at the Telethon boundary."""

    def __init__(self, client: object, *, now_provider: Callable[[], float] | None = None) -> None:
        self._client = cast(UserProfileClient, client)
        self._now_provider = now_provider or time.time

    def get_user_reference(self, user_id: int, *, is_self: bool = False) -> UserReference | None:
        if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
            return None
        return _user_reference_from_peer(_session_entity(self._client, user_id), user_id, is_self=is_self)

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
        current_photo = _normalize_user_current_photo(result, user_id)
        return replace(observation, personal_channel_reference=reference, current_photo=current_photo)

    async def fetch_personal_channel_post(
        self, reference: PersonalChannelReference, message_id: int
    ) -> PersonalChannelPost | None:
        message_id = _validated_personal_channel_message_id(message_id)
        peer = _personal_channel_peer(reference)
        fetched = await self._client.get_messages(peer, ids=[message_id])
        return _normalize_personal_channel_post(_first_message(fetched), message_id)


def _is_canonical_channel_id(channel_id: object) -> bool:
    return isinstance(channel_id, int) and not isinstance(channel_id, bool) and channel_id <= -CHANNEL_ID_MARKER - 1


def _canonical_channel_id(raw_channel_id: int) -> int:
    return -CHANNEL_ID_MARKER - raw_channel_id


def _channel_reference_from_session_entity(  # noqa: PLR0911
    input_entity: object, channel_id: int
) -> ChannelReference | None:
    if inspect.isawaitable(input_entity):
        close = getattr(input_entity, "close", None)
        if callable(close):
            close()
        return None
    if not isinstance(input_entity, types.InputPeerChannel):
        return None
    raw_channel_id = getattr(input_entity, "channel_id", None)
    if not isinstance(raw_channel_id, int) or isinstance(raw_channel_id, bool):
        return None
    if _canonical_channel_id(raw_channel_id) != channel_id:
        return None
    normalized_hash = _signed_64(getattr(input_entity, "access_hash", None))
    if normalized_hash is None:
        return None
    try:
        return ChannelReference(channel_id=channel_id, access_hash=normalized_hash)
    except ValueError:
        return None


def _validate_channel_reference(reference: object) -> ChannelReference:
    if not isinstance(reference, ChannelReference):
        raise TypeError("channel reference is invalid")
    return reference


def _input_channel(reference: ChannelReference) -> types.InputChannel:
    raw_channel_id = -CHANNEL_ID_MARKER - reference.channel_id
    return types.InputChannel(channel_id=raw_channel_id, access_hash=reference.access_hash)


def _channel_target_matches(channel_id: int, raw_id: object) -> bool:
    if not isinstance(raw_id, int) or isinstance(raw_id, bool) or raw_id <= 0:
        return False
    if channel_id == raw_id or abs(channel_id) == raw_id:
        return True
    return channel_id == -1000000000000 - raw_id


def _validated_full_channel(result: object, channel_id: int) -> types.ChannelFull:
    if not isinstance(result, types.messages.ChatFull):
        raise ValueError("full channel envelope is invalid")
    full_chat = getattr(result, "full_chat", None)
    if not isinstance(full_chat, types.ChannelFull):
        raise ValueError("channel full payload is invalid")
    if not _channel_target_matches(channel_id, getattr(full_chat, "id", None)):
        raise ValueError("channel full target does not match")
    return full_chat


def _channel_created(result: object, channel_id: int) -> int | None:
    chats = getattr(result, "chats", None)
    if not isinstance(chats, Sequence):
        return None
    for chat in chats:
        if not _channel_target_matches(channel_id, getattr(chat, "id", None)):
            continue
        date = getattr(chat, "date", None)
        if isinstance(date, datetime):
            return int(date.timestamp())
    return None


def _unavailable_channel_profile(
    channel_id: int,
    started_at: float,
    completed_at: float,
    reason: str,
) -> ChannelProfileObservation:
    return ChannelProfileObservation(
        channel_id=channel_id,
        about=None,
        participants_count=None,
        linked_chat_id=None,
        pinned_msg_id=None,
        slow_mode_seconds=None,
        available_reactions={"kind": "none", "emojis": []},
        current_photo=None,
        observation_started_at=started_at,
        observation_completed_at=completed_at,
        status=ProjectionStatus.UNAVAILABLE,
        reason=reason,
    )


def _nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _normalize_linked_channel_id(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("linked channel id is invalid")
    if value == 0:
        return None
    if value < 0:
        raise ValueError("linked channel id must be a positive raw channel id")
    raw_id = value
    return int(get_peer_id(types.PeerChannel(raw_id)))


def _normalize_channel_photo(photo: object) -> ChatCurrentPhoto | None:
    if photo is None:
        return None
    if not isinstance(photo, (types.Photo, types.PhotoEmpty)):
        return None
    photo_id = _positive_id(getattr(photo, "id", None))
    if isinstance(photo, types.PhotoEmpty) and getattr(photo, "id", None) == 0:
        return None
    if photo_id is None:
        raise ValueError("channel current photo id is invalid")
    raw_date = getattr(photo, "date", None)
    date = raw_date.isoformat() if isinstance(raw_date, datetime) else None
    return ChatCurrentPhoto(photo_id=photo_id, date=date)


def _normalize_user_current_photo(result: object, user_id: int) -> ChatCurrentPhoto | None:
    users = getattr(result, "users", None)
    if not isinstance(users, Sequence) or isinstance(users, str | bytes | bytearray):
        return None
    for user in users:
        if _positive_id(getattr(user, "id", None)) != user_id:
            continue
        photo = getattr(user, "photo", None)
        photo_id = _positive_id(getattr(photo, "photo_id", None))
        return None if photo_id is None else ChatCurrentPhoto(photo_id)
    return None


def _normalize_reactions(raw_reactions: object) -> dict[str, object]:
    if isinstance(raw_reactions, types.ChatReactionsAll):
        return {"kind": "all", "emojis": []}
    if isinstance(raw_reactions, types.ChatReactionsSome):
        emojis = [
            value
            for reaction in cast(Sequence[object], getattr(raw_reactions, "reactions", ()) or ())
            if (value := getattr(cast(object, reaction), "emoticon", None)) is not None and isinstance(value, str)
        ]
        return {"kind": "some", "emojis": emojis}
    if raw_reactions is None or isinstance(raw_reactions, types.ChatReactionsNone):
        return {"kind": "none", "emojis": []}
    return {"kind": "unknown", "emojis": []}


def _validated_contact_ids(result: object) -> tuple[int, ...]:
    if not isinstance(result, types.channels.ChannelParticipants):
        raise ValueError("channel participants envelope is invalid")
    users = getattr(result, "users", None)
    if not isinstance(users, Sequence) or isinstance(users, str | bytes | bytearray):
        raise ValueError("channel participants users are invalid")
    ids = {user_id for user in users if (user_id := _positive_id(getattr(user, "id", None))) is not None}
    return tuple(sorted(ids))


def _close_awaitable(value: object) -> None:
    close = getattr(value, "close", None)
    if callable(close):
        close()


def _validate_user_reference(reference: object) -> UserReference:
    if not isinstance(reference, UserReference):
        raise TypeError("user reference is invalid")
    return reference


def _input_user(reference: UserReference) -> TypeInputUser:
    return types.InputUserSelf() if reference.is_self else types.InputUser(reference.user_id, reference.access_hash)


def _session_entity(client: object, entity_id: int) -> object | None:
    session = getattr(client, "session", None)
    getter = getattr(session, "get_input_entity", None)
    if not callable(getter) or inspect.iscoroutinefunction(getter):
        return None
    try:
        peer = getter(entity_id)
    except AttributeError, KeyError, TypeError, ValueError:
        return None
    if inspect.isawaitable(peer):
        _close_awaitable(peer)
        return None
    return peer


def _chat_avatar_reference_from_peer(peer: object | None, entity_id: int) -> ChatAvatarReference | None:
    if isinstance(peer, types.InputPeerChannel):
        raw_id = getattr(peer, "channel_id", None)
        access_hash = _signed_64(getattr(peer, "access_hash", None))
        if not isinstance(raw_id, int) or access_hash is None:
            return None
        canonical_id = _canonical_channel_id(raw_id)
        return ChannelReference(canonical_id, access_hash) if canonical_id == entity_id else None
    if isinstance(peer, types.InputPeerChat):
        raw_id = getattr(peer, "chat_id", None)
        if isinstance(raw_id, int) and -raw_id == entity_id:
            return GroupReference(-raw_id)
    return None


def _user_reference_from_peer(peer: object | None, user_id: int, *, is_self: bool) -> UserReference | None:
    if isinstance(peer, types.InputPeerSelf):
        return UserReference(user_id, 0, is_self=True) if is_self else None
    if not isinstance(peer, types.InputPeerUser):
        return None
    raw_id = getattr(peer, "user_id", None)
    access_hash = _signed_64(getattr(peer, "access_hash", None))
    if raw_id != user_id or access_hash is None:
        return None
    return UserReference(user_id, access_hash, is_self=is_self)


def _normalize_common_chats(result: object) -> tuple[tuple[CommonChatSummary, ...], int, ProjectionStatus]:
    if not isinstance(result, (types.messages.Chats, types.messages.ChatsSlice)):
        raise ValueError("common chats envelope is invalid")
    raw_chats = getattr(result, "chats", None)
    if not isinstance(raw_chats, Sequence) or isinstance(raw_chats, str | bytes | bytearray):
        raise ValueError("common chats rows are invalid")
    output: list[CommonChatSummary] = []
    seen: set[int] = set()
    for chat in raw_chats:
        summary = _normalize_common_chat(chat, seen)
        if summary is not None:
            output.append(summary)
    reported = _count_or_length(result, len(output))
    return (
        tuple(output),
        max(reported, len(output)),
        ProjectionStatus.PARTIAL if isinstance(result, types.messages.ChatsSlice) else ProjectionStatus.USABLE,
    )


def _normalize_common_chat(chat: object, seen: set[int]) -> CommonChatSummary | None:
    identity = _common_chat_identity(chat, seen)
    if identity is None:
        return None
    chat_id, raw_id, kind = identity
    title = getattr(chat, "title", None)
    if title is None:
        title = str(raw_id)
    elif not isinstance(title, str):
        raise ValueError("common chat title is invalid")
    elif not title:
        title = str(raw_id)
    return CommonChatSummary(chat_id, title, kind)


def _common_chat_identity(chat: object, seen: set[int]) -> tuple[int, int, str] | None:
    if isinstance(chat, types.ChatEmpty):
        return None
    if not isinstance(chat, (types.Chat, types.ChatForbidden, types.Channel, types.ChannelForbidden)):
        raise ValueError("common chat row is invalid")
    raw_id = _positive_id(getattr(chat, "id", None))
    if raw_id is None:
        raise ValueError("common chat id is invalid")
    chat_id = -raw_id if isinstance(chat, (types.Chat, types.ChatForbidden)) else _canonical_channel_id(raw_id)
    if chat_id in seen:
        return None
    seen.add(chat_id)
    kind = "group"
    if isinstance(chat, (types.Channel, types.ChannelForbidden)):
        kind = "supergroup" if bool(getattr(chat, "megagroup", False)) else "channel"
    return chat_id, raw_id, kind


def _normalize_user_photos(result: object, user_id: int) -> tuple[tuple[ChatCurrentPhoto, ...], int, ProjectionStatus]:
    if not isinstance(result, (types.photos.Photos, types.photos.PhotosSlice)):
        raise ValueError("user photos envelope is invalid")
    raw_photos = getattr(result, "photos", None)
    if not isinstance(raw_photos, Sequence) or isinstance(raw_photos, str | bytes | bytearray):
        raise ValueError("user photos rows are invalid")
    users = getattr(result, "users", None)
    if not isinstance(users, Sequence) or isinstance(users, str | bytes | bytearray):
        raise ValueError("user photos users are invalid")
    user_ids = _normalize_photo_user_ids(users)
    if users and user_id not in user_ids:
        raise ValueError("user photos target does not match")
    photos = _normalize_photo_rows(raw_photos, context="user avatar")
    reported = _count_or_length(result, len(photos))
    status = ProjectionStatus.PARTIAL if isinstance(result, types.photos.PhotosSlice) else ProjectionStatus.USABLE
    return photos, max(reported, len(photos)), status


def _normalize_photo_user_ids(users: Sequence[object]) -> set[int]:
    user_ids: set[int] = set()
    for user in users:
        if not isinstance(user, (types.User, types.UserEmpty)):
            raise ValueError("user photos user row is invalid")
        normalized_id = _positive_id(getattr(user, "id", None))
        if normalized_id is None:
            raise ValueError("user photos user id is invalid")
        user_ids.add(normalized_id)
    return user_ids


def _normalize_chat_photos(
    result: object, reference: ChatAvatarReference
) -> tuple[tuple[ChatCurrentPhoto, ...], int, ProjectionStatus]:
    accepted = (types.messages.Messages, types.messages.MessagesSlice, types.messages.ChannelMessages)
    if not isinstance(result, accepted):
        raise ValueError("chat photos envelope is invalid")
    raw_messages = getattr(result, "messages", None)
    if not isinstance(raw_messages, Sequence) or isinstance(raw_messages, str | bytes | bytearray):
        raise ValueError("chat photo messages are invalid")
    photos: list[ChatCurrentPhoto] = []
    seen: set[int] = set()
    for message in raw_messages:
        photo = _normalize_chat_photo(message, reference, seen)
        if photo is not None:
            photos.append(photo)
    reported = _count_or_length(result, len(photos))
    status = (
        ProjectionStatus.PARTIAL
        if isinstance(result, (types.messages.MessagesSlice, types.messages.ChannelMessages))
        else ProjectionStatus.USABLE
    )
    return tuple(photos), max(reported, len(photos)), status


def _normalize_chat_photo(message: object, reference: ChatAvatarReference, seen: set[int]) -> ChatCurrentPhoto | None:
    if isinstance(message, types.MessageEmpty):
        return None
    if not isinstance(message, (types.Message, types.MessageService)):
        raise ValueError("chat photo message is invalid")
    if not _chat_message_matches_reference(message, reference):
        raise ValueError("chat photo message target does not match")
    action = getattr(message, "action", None)
    if not isinstance(action, types.MessageActionChatEditPhoto):
        return None
    return _normalize_chat_photo_action(action, message, seen)


def _normalize_chat_photo_action(action: object, message: object, seen: set[int]) -> ChatCurrentPhoto | None:
    photo = getattr(action, "photo", None)
    if not isinstance(photo, (types.Photo, types.PhotoEmpty)):
        raise ValueError("chat photo action payload is invalid")
    photo_id = _positive_id(getattr(photo, "id", None))
    if photo_id is None and isinstance(photo, types.PhotoEmpty):
        return None
    if photo_id is None:
        raise ValueError("chat avatar photo id is invalid")
    if photo_id in seen:
        return None
    seen.add(photo_id)
    raw_date = getattr(message, "date", None)
    if raw_date is not None and not isinstance(raw_date, datetime):
        raise ValueError("chat avatar date is invalid")
    return ChatCurrentPhoto(photo_id, raw_date.isoformat() if raw_date is not None else None)


def _chat_message_matches_reference(message: object, reference: ChatAvatarReference) -> bool:
    peer = getattr(message, "peer_id", None)
    if isinstance(reference, ChannelReference):
        return isinstance(peer, types.PeerChannel) and peer.channel_id == -CHANNEL_ID_MARKER - reference.channel_id
    return isinstance(peer, types.PeerChat) and peer.chat_id == abs(reference.group_id)


def _normalize_photo_rows(raw_photos: Sequence[object], *, context: str) -> tuple[ChatCurrentPhoto, ...]:
    output: list[ChatCurrentPhoto] = []
    seen: set[int] = set()
    for photo in raw_photos:
        if not isinstance(photo, (types.Photo, types.PhotoEmpty)):
            raise ValueError(f"{context} photo row is invalid")
        photo_id = _positive_id(getattr(photo, "id", None))
        if photo_id is None and isinstance(photo, types.PhotoEmpty):
            continue
        if photo_id is None:
            raise ValueError(f"{context} photo id is invalid")
        if photo_id in seen:
            continue
        seen.add(photo_id)
        raw_date = getattr(photo, "date", None)
        if raw_date is not None and not isinstance(raw_date, datetime):
            raise ValueError(f"{context} photo date is invalid")
        output.append(ChatCurrentPhoto(photo_id, raw_date.isoformat() if raw_date is not None else None))
    return tuple(output)


def _count_or_length(result: object, fallback: int) -> int:
    count = getattr(result, "count", fallback)
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValueError("reported count is invalid")
    return count


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


def _group_created(result: object, raw_group_id: int) -> int | None:
    chats = getattr(result, "chats", None)
    if not isinstance(chats, Sequence):
        return None
    for chat in chats:
        if _positive_id(getattr(chat, "id", None)) != raw_group_id:
            continue
        date = getattr(chat, "date", None)
        if isinstance(date, datetime):
            return int(date.timestamp())
    return None


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


def _normalize_current_photo(photo: object) -> ChatCurrentPhoto | None:
    if photo is None:
        return None
    if not isinstance(photo, (types.Photo, types.PhotoEmpty)):
        return None
    if isinstance(photo, types.PhotoEmpty) and getattr(photo, "id", None) == 0:
        return None
    photo_id = _positive_id(getattr(photo, "id", None))
    if photo_id is None:
        raise ValueError("legacy group current photo id is invalid")
    raw_date = getattr(photo, "date", None)
    date = raw_date.isoformat() if isinstance(raw_date, datetime) else None
    return ChatCurrentPhoto(photo_id=photo_id, date=date)


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


def translate_reaction_detail_failure(exc: BaseException) -> GatewayFailure:
    """Translate one reaction-detail error with its bounded terminal RPC set."""
    failure = translate_gateway_failure(exc)
    if not is_reaction_detail_terminal_rpc_error(exc):
        return failure
    return GatewayFailure(GatewayFailureKind.INVALID_TARGET, failure.error_type, failure.error_message, False)


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
