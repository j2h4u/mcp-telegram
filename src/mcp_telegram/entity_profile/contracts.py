"""Stable contracts for progressive entity profiles.

The daemon owns refresh policy. These types describe only what can be safely
returned to a caller while enrichment is incomplete.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

PROFILE_SECTIONS: tuple[str, ...] = (
    "full_profile",
    "common_chats",
    "contact_overlap",
    "avatar_history",
    "personal_channel",
)

PROFILE_ACQUISITION_OUTCOMES: frozenset[str] = frozenset(
    {"usable", "partial", "absent", "unavailable"}
)
PROFILE_ENDPOINT = "users.GetFullUser"
PROFILE_NORMALIZATION_VERSION = "entity-profile-v1"


@dataclass(frozen=True, slots=True)
class ProfileAcquisitionEvidence:
    """Bounded evidence attached to one section materialization.

    The repository stores this as a small JSON projection.  Raw Telegram
    envelopes and arbitrary response objects deliberately have no place in the
    contract.
    """

    generation: int
    outcome: str
    provenance: Mapping[str, object] | None = None
    normalization_version: str | None = PROFILE_NORMALIZATION_VERSION
    observation_started_at: int | None = None
    observation_completed_at: int | None = None
    identity: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.generation < 0:
            raise ValueError("generation must be non-negative")
        if self.outcome not in PROFILE_ACQUISITION_OUTCOMES:
            raise ValueError(f"unsupported profile acquisition outcome: {self.outcome}")
        if self.observation_started_at is not None and self.observation_started_at < 0:
            raise ValueError("observation_started_at must be non-negative")
        if self.observation_completed_at is not None and self.observation_completed_at < 0:
            raise ValueError("observation_completed_at must be non-negative")
        if (
            self.observation_started_at is not None
            and self.observation_completed_at is not None
            and self.observation_completed_at < self.observation_started_at
        ):
            raise ValueError("observation interval is reversed")

    @property
    def observation_at(self) -> int | None:
        """Use the conservative start of the original observation interval."""
        return self.observation_started_at


def completeness(sections: dict[str, dict[str, object]]) -> str:
    """Return ``complete`` only when every applicable section is fresh."""
    if not sections:
        return "partial"
    statuses = {str(value.get("status")) for value in sections.values()}
    return "complete" if statuses <= {"fresh", "not_applicable"} else "partial"
