"""Semantic membership signals from retained Telegram service messages."""

from __future__ import annotations

from dataclasses import dataclass

from ..message_composition import decode_service_action


@dataclass(frozen=True, slots=True)
class MembershipFact:
    """Service-message membership semantics and Telegram revision time."""

    is_service: int
    action_json: str | None
    sender_id: int | None
    occurred_at: int | None
    edited_at: int | None


@dataclass(frozen=True, slots=True)
class MembershipChange:
    dialog_id: int
    subjects: frozenset[int]
    revision_at: int | None


@dataclass(frozen=True, slots=True)
class MembershipSignalCounts:
    received: int = 0
    ignored: int = 0
    queued: int = 0
    coalesced: int = 0
    ignored_account_mismatch: int = 0
    ignored_ineligible: int = 0
    ignored_snapshot_newer: int = 0
    ignored_already_pending: int = 0
    ignored_request_rejected: int = 0

    def as_observation(self) -> dict[str, int]:
        return {
            "received": self.received,
            "ignored": self.ignored,
            "queued": self.queued,
            "coalesced": self.coalesced,
            "ignored_account_mismatch": self.ignored_account_mismatch,
            "ignored_ineligible": self.ignored_ineligible,
            "ignored_snapshot_newer": self.ignored_snapshot_newer,
            "ignored_already_pending": self.ignored_already_pending,
            "ignored_request_rejected": self.ignored_request_rejected,
        }


def changed_members(before: MembershipFact | None, after: MembershipFact) -> frozenset[int]:
    """Return both prior and new subjects only when membership semantics change."""
    old = membership_signal(before.is_service, before.action_json, before.sender_id) if before else frozenset()
    new = membership_signal(after.is_service, after.action_json, after.sender_id)
    if old == new:
        return frozenset()
    return frozenset(user for _, user in old | new)


def membership_signal(is_service: int, action_json: str | None, sender_id: int | None) -> frozenset[tuple[str, int]]:
    """Identify affected users without asserting their present membership."""
    if not is_service or action_json is None:
        return frozenset()
    try:
        action = decode_service_action(action_json)
    except ValueError, TypeError:
        return frozenset()
    if action is None:
        return frozenset()
    return _action_members(action, sender_id)


def _action_members(action: dict[str, object], sender_id: int | None) -> frozenset[tuple[str, int]]:
    kind = action.get("_")
    users: object = []
    direction = "joined"
    if kind == "MessageActionChatAddUser":
        users = action.get("users")
    elif kind == "MessageActionChatDeleteUser":
        users, direction = [action.get("user_id")], "left"
    elif kind in {"MessageActionChatJoinedByLink", "MessageActionChatJoinedByRequest"}:
        users = [sender_id]
    if not isinstance(users, list):
        return frozenset()
    return frozenset((direction, user) for user in users if type(user) is int and user > 0)
