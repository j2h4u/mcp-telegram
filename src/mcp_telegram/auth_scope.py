"""Private authenticated-session identity used by local fact receipts.

The scope is deliberately derived from the already-connected primary Telethon
session.  It is not a Telegram API identity and must never cross a public
response or telemetry boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, cast

AUTH_SCOPE_VERSION: Final = 1


@dataclass(frozen=True, slots=True)
class TelegramAuthScope:
    """Stable-in-process identity for one authenticated primary session."""

    version: int
    account_id: int
    dc_id: int
    auth_key_id: int

    def __post_init__(self) -> None:
        if self.version != AUTH_SCOPE_VERSION:
            raise ValueError("unsupported auth scope version")
        for value, name in (
            (self.account_id, "account_id"),
            (self.dc_id, "dc_id"),
            (self.auth_key_id, "auth_key_id"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    def as_private_mapping(self) -> dict[str, int]:
        """Return the bounded representation permitted inside v61 evidence."""
        return {
            "version": self.version,
            "account_id": self.account_id,
            "dc_id": self.dc_id,
            "auth_key_id": self.auth_key_id,
        }


def capture_auth_scope(profile: object, client: object) -> TelegramAuthScope | None:
    """Read account and primary permanent-session identity without an RPC."""
    account_id = getattr(profile, "id", None)
    session = getattr(client, "session", None)
    dc_id = getattr(session, "dc_id", None)
    auth_key = getattr(session, "auth_key", None)
    auth_key_id = getattr(auth_key, "key_id", None)
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in (account_id, dc_id, auth_key_id)
    ):
        return None
    account_id = cast(int, account_id)
    dc_id = cast(int, dc_id)
    auth_key_id = cast(int, auth_key_id)
    try:
        return TelegramAuthScope(
            version=AUTH_SCOPE_VERSION,
            account_id=account_id,
            dc_id=dc_id,
            auth_key_id=auth_key_id,
        )
    except ValueError:
        return None


__all__ = ["AUTH_SCOPE_VERSION", "TelegramAuthScope", "capture_auth_scope"]
