"""Shared test helpers for sync/event/delta tests."""

# Telethon-shaped MagicMock fixtures intentionally expose dynamic attributes.
# pyright: reportAny=false

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TypedDict, Unpack

from mcp_telegram.entity_profile.contracts import (
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
    PersonalChannelPost,
    PersonalChannelReference,
    ProjectionOutcome,
    ProjectionStatus,
    TargetKind,
    UserAvatarHistoryObservation,
    UserProfileObservation,
    UserReference,
)


class _BuildMockMessageKwargs(TypedDict, total=False):
    text: str | None
    sender_id: int | None
    sender_first_name: str | None
    media: object | None
    reply_to_msg_id: int | None
    forum_topic: bool
    reply_to_top_id: int | None
    message_thread_id: int | None
    is_topic_message: bool
    reply_count: int
    reactions: object | None
    edit_date: datetime | None


def build_mock_message(
    id: int,
    **kwargs: Unpack[_BuildMockMessageKwargs],
) -> SimpleNamespace:
    """Build a minimal Telethon-like message object."""
    text = kwargs.get("text", "hello")
    sender_id = kwargs.get("sender_id", 42)
    sender_first_name = kwargs.get("sender_first_name", "Alice")
    media = kwargs.get("media")
    reply_to_msg_id = kwargs.get("reply_to_msg_id")
    forum_topic = kwargs.get("forum_topic", False)
    reply_to_top_id = kwargs.get("reply_to_top_id")
    message_thread_id = kwargs.get("message_thread_id")
    is_topic_message = kwargs.get("is_topic_message", False)
    reply_count = kwargs.get("reply_count", 0)
    reactions = kwargs.get("reactions")
    edit_date = kwargs.get("edit_date")
    sender = SimpleNamespace(first_name=sender_first_name) if sender_first_name is not None else None

    reply_to_obj: SimpleNamespace | None = None
    if reply_to_msg_id is not None or forum_topic:
        reply_to_obj = SimpleNamespace(
            reply_to_msg_id=reply_to_msg_id,
            forum_topic=forum_topic,
            reply_to_top_id=reply_to_top_id,
        )

    return SimpleNamespace(
        id=id,
        date=datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
        message=text,
        sender_id=sender_id,
        sender=sender,
        media=media,
        reply_to=reply_to_obj,
        replies=SimpleNamespace(replies=reply_count) if reply_count else None,
        reactions=reactions,
        edit_date=edit_date,
        message_thread_id=message_thread_id,
        is_topic_message=is_topic_message,
    )


def build_mock_reactions(counts: dict[str, int]) -> SimpleNamespace:
    """Build a mock MessageReactions object."""
    results = [
        SimpleNamespace(reaction=SimpleNamespace(emoticon=emoji), count=count) for emoji, count in counts.items()
    ]
    return SimpleNamespace(results=results)


class MockTotalList(list):
    """List subclass with .total attribute, mimicking Telethon TotalList.

    Use in tests that switch from iter_messages to get_messages:
        mock_client.get_messages = AsyncMock(
            return_value=MockTotalList([msg1, msg2], total=500)
        )
    """

    def __init__(self, items: list, total: int | None = None) -> None:
        super().__init__(items)
        self.total = total if total is not None else len(items)


class LoudGroupProfilePort:
    """Test-only port that fails if an unrelated composition invokes it."""

    async def fetch_group_profile(self, group_id: int) -> GroupProfileObservation:
        raise AssertionError(f"unexpected group profile request for {group_id}")


class LoudCommonChatsPort:
    async def fetch_common_chats(self, reference: UserReference) -> CommonChatsObservation:
        raise AssertionError(f"unexpected common-chat request for {reference.user_id}")


class LoudUserAvatarHistoryPort:
    async def fetch_user_avatar_history(self, reference: UserReference) -> UserAvatarHistoryObservation:
        raise AssertionError(f"unexpected user-avatar request for {reference.user_id}")


class LoudChatAvatarHistoryPort:
    def get_chat_avatar_reference(self, entity_id: int) -> None:
        raise AssertionError(f"unexpected chat-avatar reference request for {entity_id}")

    async def fetch_chat_avatar_history(self, reference: ChatAvatarReference) -> ChatAvatarHistoryObservation:
        raise AssertionError(f"unexpected chat-avatar request for {reference}")


class ClientCommonChatsPort:
    """Neutral common-chat port backed by a test client's response queue."""

    def __init__(self, client: object) -> None:
        self.client = client

    async def fetch_common_chats(self, reference: UserReference) -> CommonChatsObservation:
        result = await self.client(("common_chats", {"user_id": reference.user_id, "limit": 100}))  # type: ignore[operator]
        chats: list[CommonChatSummary] = []
        for chat in getattr(result, "chats", ()) or ():
            chat_id = getattr(chat, "id", None)
            if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id == 0:
                raise ValueError("test common-chat id is invalid")
            kind = "supergroup" if bool(getattr(chat, "megagroup", False)) else "channel"
            if type(chat).__name__ == "Chat":
                kind = "group"
            chats.append(CommonChatSummary(chat_id, getattr(chat, "title", None), kind))
        return CommonChatsObservation(
            reference.user_id, tuple(chats), len(chats), ProjectionStatus.USABLE, None, 100.0, 100.0
        )


class ClientUserAvatarHistoryPort:
    """Neutral user-avatar port backed by a test client's response queue."""

    def __init__(self, client: object) -> None:
        self.client = client

    async def fetch_user_avatar_history(self, reference: UserReference) -> UserAvatarHistoryObservation:
        result = await self.client(("user_photos", {"user_id": reference.user_id, "limit": 100}))  # type: ignore[operator]
        photos: list[ChatCurrentPhoto] = []
        for photo in getattr(result, "photos", ()) or ():
            photo_id = getattr(photo, "id", None)
            if not isinstance(photo_id, int) or isinstance(photo_id, bool) or photo_id <= 0:
                raise ValueError("test user-photo id is invalid")
            date = getattr(photo, "date", None)
            photos.append(ChatCurrentPhoto(photo_id, date.isoformat() if date is not None else None))
        count = getattr(result, "count", len(photos))
        if not isinstance(count, int):
            count = len(photos)
        return UserAvatarHistoryObservation(
            reference.user_id, tuple(photos), max(count, len(photos)), ProjectionStatus.USABLE, None, 100.0, 100.0
        )


class ClientChatAvatarHistoryPort:
    """Neutral chat-avatar port backed by a test client's response queue."""

    def __init__(self, client: object) -> None:
        self.client = client

    def get_chat_avatar_reference(self, entity_id: int) -> ChatAvatarReference | None:
        return GroupReference(entity_id) if isinstance(entity_id, int) and entity_id < 0 else None

    async def fetch_chat_avatar_history(self, reference: ChatAvatarReference) -> ChatAvatarHistoryObservation:
        result = await self.client(("search", {"limit": 100}))  # type: ignore[operator]
        photos: list[ChatCurrentPhoto] = []
        for message in getattr(result, "messages", ()) or ():
            action = getattr(message, "action", None)
            photo = getattr(action, "photo", None)
            photo_id = getattr(photo, "id", None)
            if not isinstance(photo_id, int) or isinstance(photo_id, bool) or photo_id <= 0:
                continue
            date = getattr(message, "date", None)
            photos.append(ChatCurrentPhoto(photo_id, date.isoformat() if date is not None else None))
        count = getattr(result, "count", len(photos))
        if not isinstance(count, int):
            count = len(photos)
        return ChatAvatarHistoryObservation(
            reference, tuple(photos), max(count, len(photos)), ProjectionStatus.USABLE, None, 100.0, 100.0
        )


class LoudChannelProfilePort:
    """Test-only port that fails if a channel composition unexpectedly calls it."""

    def get_channel_reference(self, channel_id: int) -> ChannelReference | None:
        raise AssertionError(f"unexpected channel reference request for {channel_id}")

    async def fetch_channel_profile(self, reference: ChannelReference) -> ChannelProfileObservation:
        raise AssertionError(f"unexpected channel profile request for {reference.channel_id}")

    async def fetch_channel_contact_overlap(self, reference: ChannelReference) -> ChannelContactOverlapObservation:
        raise AssertionError(f"unexpected channel contact overlap request for {reference.channel_id}")


class FakeChannelProfilePort:
    """Deterministic channel profile port double with independent call accounting."""

    def __init__(
        self,
        profile: ChannelProfileObservation,
        overlap: ChannelContactOverlapObservation,
        *,
        access_hash: int = 0,
    ) -> None:
        self.profile = profile
        self.overlap = overlap
        self.profile_calls: list[int] = []
        self.overlap_calls: list[int] = []
        self.references: list[ChannelReference] = []
        self.access_hash = access_hash

    def get_channel_reference(self, channel_id: int) -> ChannelReference | None:
        canonical_id = channel_id if channel_id <= -1_000_000_000_001 else -1_000_000_000_000 - abs(channel_id)
        return ChannelReference(channel_id=canonical_id, access_hash=self.access_hash)

    async def fetch_channel_profile(self, reference: ChannelReference) -> ChannelProfileObservation:
        self.references.append(reference)
        self.profile_calls.append(reference.channel_id)
        return replace(self.profile, channel_id=reference.channel_id)

    async def fetch_channel_contact_overlap(self, reference: ChannelReference) -> ChannelContactOverlapObservation:
        self.overlap_calls.append(reference.channel_id)
        return replace(self.overlap, channel_id=reference.channel_id)


class LoudUserProfilePort:
    """Test-only port that fails if a user profile request is unexpected."""

    def get_user_reference(self, user_id: int, *, is_self: bool = False) -> None:
        raise AssertionError(f"unexpected user reference request for {user_id}/{is_self}")

    async def fetch_user_profile(self, user_id: int, target_kind: TargetKind) -> UserProfileObservation:
        raise AssertionError(f"unexpected user profile request for {user_id} ({target_kind})")

    async def fetch_personal_channel_post(
        self, reference: PersonalChannelReference, message_id: int
    ) -> PersonalChannelPost | None:
        raise AssertionError(f"unexpected personal channel post request for {reference.channel_id}/{message_id}")


class FakeUserProfilePort:
    """Deterministic user profile port double with explicit call accounting."""

    def __init__(
        self,
        observation: UserProfileObservation | None = None,
        error: BaseException | None = None,
        post: PersonalChannelPost | None = None,
        post_error: BaseException | None = None,
    ) -> None:
        self.observation = observation
        self.error = error
        self.post = post
        self.post_error = post_error
        self.calls: list[tuple[int, TargetKind]] = []

    def get_user_reference(self, user_id: int, *, is_self: bool = False) -> UserReference:
        return UserReference(user_id, 0, is_self=is_self)

    async def fetch_user_profile(self, user_id: int, target_kind: TargetKind) -> UserProfileObservation:
        self.calls.append((user_id, target_kind))
        if self.error is not None:
            raise self.error
        if self.observation is None:
            unavailable = ProjectionOutcome(ProjectionStatus.UNAVAILABLE, None, "unset", None)
            return UserProfileObservation(user_id, target_kind, unavailable, unavailable)
        return self.observation

    async def fetch_personal_channel_post(
        self, reference: PersonalChannelReference, message_id: int
    ) -> PersonalChannelPost | None:
        if self.post_error is not None:
            raise self.post_error
        if self.post is not None and self.post.message_id != message_id:
            return None
        return self.post
