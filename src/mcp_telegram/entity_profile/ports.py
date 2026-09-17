"""Stable ports for entity profile application consumers."""

from __future__ import annotations

from typing import Protocol

from .contracts import (
    ChannelContactOverlapObservation,
    ChannelProfileObservation,
    ChannelReference,
    ChatAvatarHistoryObservation,
    ChatAvatarReference,
    CommonChatsObservation,
    GroupProfileObservation,
    PersonalChannelPost,
    PersonalChannelReference,
    TargetKind,
    UserAvatarHistoryObservation,
    UserProfileObservation,
    UserReference,
)


class ChannelReferenceProvider(Protocol):
    """Resolve one canonical channel through the local Telethon session cache."""

    def get_channel_reference(self, channel_id: int) -> ChannelReference | None: ...


class GroupProfilePort(Protocol):
    """Fetch one normalized legacy group profile observation."""

    async def fetch_group_profile(self, group_id: int) -> GroupProfileObservation: ...


class CommonChatsPort(Protocol):
    """Fetch one bounded common-chat page."""

    async def fetch_common_chats(self, reference: UserReference) -> CommonChatsObservation: ...


class UserAvatarHistoryPort(Protocol):
    """Fetch one bounded user avatar-history page."""

    async def fetch_user_avatar_history(self, reference: UserReference) -> UserAvatarHistoryObservation: ...


class ChatAvatarHistoryPort(Protocol):
    """Fetch one bounded chat avatar-history page."""

    def get_chat_avatar_reference(self, entity_id: int) -> ChatAvatarReference | None: ...

    async def fetch_chat_avatar_history(self, reference: ChatAvatarReference) -> ChatAvatarHistoryObservation: ...


class ChannelProfilePort(ChannelReferenceProvider, Protocol):
    """Fetch channel profile and bounded contact overlap independently."""

    async def fetch_channel_profile(self, reference: ChannelReference) -> ChannelProfileObservation: ...

    async def fetch_channel_contact_overlap(self, reference: ChannelReference) -> ChannelContactOverlapObservation: ...


class UserProfilePort(Protocol):
    """Fetch one normalized user or bot profile observation."""

    def get_user_reference(self, user_id: int, *, is_self: bool = False) -> UserReference | None: ...

    async def fetch_user_profile(self, user_id: int, target_kind: TargetKind) -> UserProfileObservation: ...

    async def fetch_personal_channel_post(
        self, reference: PersonalChannelReference, message_id: int
    ) -> PersonalChannelPost | None: ...


class ProfilePairObservationHook(Protocol):
    """Best-effort aggregate observer for Entity Profile pair lifecycles."""

    def observe_profile_pair(  # noqa: PLR0913 - this is the privacy-safe boundary
        self,
        *,
        mode: str,
        eligible_pair: bool,
        outcome: str,
        actual_attempts: int = 0,
        retries: int = 0,
        full_profile_outcome: str | None = None,
        personal_channel_outcome: str | None = None,
        pair_ready: bool = False,
        pair_readiness_latency_ms: float | None = None,
        local_satisfaction: bool = False,
        prevented_request: bool = False,
        reuse_rejection_reason: str | None = None,
        reused_age_ms: float | None = None,
        stale_writer_rejected: bool = False,
        measurement_complete: bool = True,
    ) -> None: ...


__all__ = [
    "ChannelProfilePort",
    "ChannelReferenceProvider",
    "ChatAvatarHistoryPort",
    "CommonChatsPort",
    "GroupProfilePort",
    "ProfilePairObservationHook",
    "UserAvatarHistoryPort",
    "UserProfilePort",
]
