"""Stable ports for entity profile application consumers."""

from __future__ import annotations

from typing import Protocol


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


__all__ = ["ProfilePairObservationHook"]
