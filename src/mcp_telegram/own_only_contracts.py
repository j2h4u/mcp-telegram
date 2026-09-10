"""Neutral account context shared by own-message classification consumers."""

from __future__ import annotations

from dataclasses import dataclass


def normalize_channel_peer_id(value: int | None) -> int | None:
    """Normalize a raw channel id without requiring a Telethon entity."""
    if value is None:
        return None
    value = int(value)
    return value if value <= 0 else -1000000000000 - value


@dataclass(frozen=True, slots=True)
class OwnOnlyContext:
    """Account facts needed by own-message classification.

    ``personal_channel_id`` and ``linked_chat_id`` use Telegram's canonical
    peer-id form (``-100...``). Raw positive channel ids are normalized for
    callers that obtained them from UserFull.
    """

    account_id: int
    personal_channel_id: int | None = None
    personal_channel_linked_chat_id: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", int(self.account_id))
        object.__setattr__(self, "personal_channel_id", normalize_channel_peer_id(self.personal_channel_id))
        object.__setattr__(
            self,
            "personal_channel_linked_chat_id",
            normalize_channel_peer_id(self.personal_channel_linked_chat_id),
        )


__all__ = ["OwnOnlyContext", "normalize_channel_peer_id"]
