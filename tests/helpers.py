"""Shared test helpers for sync/event/delta tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TypedDict, Unpack

from mcp_telegram.entity_profile.contracts import (
    ChannelContactOverlapObservation,
    ChannelProfileObservation,
    ChannelReference,
    GroupProfileObservation,
    PersonalChannelPost,
    PersonalChannelReference,
    ProjectionOutcome,
    ProjectionStatus,
    TargetKind,
    UserProfileObservation,
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
    ) -> None:
        self.profile = profile
        self.overlap = overlap
        self.profile_calls: list[int] = []
        self.overlap_calls: list[int] = []
        self.references: list[ChannelReference] = []

    def get_channel_reference(self, channel_id: int) -> ChannelReference | None:
        canonical_id = channel_id if channel_id <= -1_000_000_000_001 else -1_000_000_000_000 - abs(channel_id)
        return ChannelReference(channel_id=canonical_id, access_hash=0)

    async def fetch_channel_profile(self, reference: ChannelReference) -> ChannelProfileObservation:
        self.references.append(reference)
        self.profile_calls.append(reference.channel_id)
        return replace(self.profile, channel_id=reference.channel_id)

    async def fetch_channel_contact_overlap(self, reference: ChannelReference) -> ChannelContactOverlapObservation:
        self.overlap_calls.append(reference.channel_id)
        return replace(self.overlap, channel_id=reference.channel_id)


class LoudUserProfilePort:
    """Test-only port that fails if a user profile request is unexpected."""

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
