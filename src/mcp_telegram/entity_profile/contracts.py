"""Stable contracts for progressive entity profiles.

The daemon owns refresh policy. These types describe only what can be safely
returned to a caller while enrichment is incomplete.
"""

PROFILE_SECTIONS: tuple[str, ...] = (
    "full_profile",
    "common_chats",
    "contact_overlap",
    "avatar_history",
    "personal_channel",
)


def completeness(sections: dict[str, dict[str, object]]) -> str:
    """Return ``complete`` only when every applicable section is fresh."""
    if not sections:
        return "partial"
    statuses = {str(value.get("status")) for value in sections.values()}
    return "complete" if statuses <= {"fresh", "not_applicable"} else "partial"
