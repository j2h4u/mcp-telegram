"""Shared identity observation rules for partial Telegram user snapshots."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

USERNAME_UNOBSERVED = object()


def observe_username(primary: object, alternates: object = USERNAME_UNOBSERVED) -> object:
    """Return a normalized username, explicit deletion, or unobserved sentinel.

    The first active alternate is authoritative when the primary username is
    absent or empty.  An explicitly materialized alternate collection with no
    usable active entry means the username was deleted.
    """
    if primary is not USERNAME_UNOBSERVED:
        normalized = _usable_username(primary)
        if normalized is not None:
            return normalized
    if alternates is USERNAME_UNOBSERVED:
        return USERNAME_UNOBSERVED
    return _observe_alternates(alternates)


def _usable_username(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = _normalize_username(value)
    return normalized or None


def _observe_alternates(alternates: object) -> object:
    if alternates is None:
        return None
    if isinstance(alternates, (str, bytes, bytearray, Mapping)) or not isinstance(alternates, Sequence):
        return None
    for candidate in alternates:
        active = candidate.get("active") if isinstance(candidate, Mapping) else getattr(candidate, "active", False)
        username = candidate.get("username") if isinstance(candidate, Mapping) else getattr(candidate, "username", None)
        if active and isinstance(username, str):
            normalized = _usable_username(username)
            if normalized is not None:
                return normalized
    return None


def _normalize_username(value: str) -> str:
    return value.strip().removeprefix("@").strip()
