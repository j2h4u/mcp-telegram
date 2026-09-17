"""Stable ports for entity profile application consumers."""

from __future__ import annotations

from typing import Protocol

from .contracts import (
    GroupProfileObservation,
    PersonalChannelPost,
    PersonalChannelReference,
    TargetKind,
    UserProfileObservation,
)


class GroupProfilePort(Protocol):
    """Fetch one normalized legacy group profile observation."""

    async def fetch_group_profile(self, group_id: int) -> GroupProfileObservation: ...


class UserProfilePort(Protocol):
    """Fetch one normalized user or bot profile observation."""

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


__all__ = ["GroupProfilePort", "ProfilePairObservationHook", "UserProfilePort"]
