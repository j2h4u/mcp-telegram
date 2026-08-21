"""Pure projection of Telegram entity facts into the agent-facing identity contract.

This module intentionally has no knowledge of SQLite, Telethon, or MCP.  Read
models may provide raw entity facts to :func:`project_entity_identity`, but
identity fallback and username normalisation live here so every delivery
surface gets the same wire shape.
"""

from __future__ import annotations

from typing import TypedDict


class UsernameEntityIdentity(TypedDict):
    display_name: str
    username: str


class TelegramIdEntityIdentity(TypedDict):
    display_name: str
    telegram_id: int


EntityIdentity = UsernameEntityIdentity | TelegramIdEntityIdentity


# ``oneOf`` is intentional: the two legal arms make the mutual exclusion of
# ``username`` and ``telegram_id`` machine-checkable on the MCP wire.
ENTITY_IDENTITY_SCHEMA: dict[str, object] = {
    "type": "object",
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "display_name": {"type": "string"},
                "username": {"type": "string"},
            },
            "required": ["display_name", "username"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "display_name": {"type": "string"},
                "telegram_id": {"type": "integer"},
            },
            "required": ["display_name", "telegram_id"],
            "additionalProperties": False,
        },
    ],
}


def _normalise_username(username: str | None) -> str | None:
    if not isinstance(username, str):
        return None
    value = username.strip()
    if value.startswith("@"):
        value = value[1:].strip()
    return f"@{value}" if value else None


def _normalise_display_name(display_name: str | None, username: str | None, telegram_id: int) -> str:
    if isinstance(display_name, str) and display_name.strip():
        return display_name.strip()
    return username or str(telegram_id)


def project_entity_identity(*, display_name: str | None, username: str | None, telegram_id: int) -> EntityIdentity:
    """Project raw identity facts to one of the two legal agent-facing arms.

    A non-empty Telegram username is canonicalised with exactly one leading
    ``@`` and wins over the numeric identifier.  When it is unavailable, the
    numeric Telegram identifier is the only stable fallback.  Missing display
    names use the canonical username or, as a last resort, the numeric id so
    the required ``display_name`` field is always a string.
    """
    if isinstance(telegram_id, bool) or not isinstance(telegram_id, int):
        raise TypeError("telegram_id must be an integer")
    canonical_username = _normalise_username(username)
    canonical_display_name = _normalise_display_name(display_name, canonical_username, telegram_id)
    if canonical_username is not None:
        return {"display_name": canonical_display_name, "username": canonical_username}
    return {"display_name": canonical_display_name, "telegram_id": telegram_id}


__all__ = ["ENTITY_IDENTITY_SCHEMA", "EntityIdentity", "project_entity_identity"]
