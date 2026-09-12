"""Three-valued local evaluation of Telegram folder rules."""

from __future__ import annotations

from .contracts import DialogFacts, FolderRule, FolderRuleKind, MembershipState


def evaluate(rule: FolderRule, facts: DialogFacts | None, *, now: int) -> MembershipState:  # noqa: PLR0911
    """Evaluate one current canonical fact without inventing absence from gaps."""
    dialog_id = facts.dialog_id if facts is not None else None
    if dialog_id is not None and dialog_id in rule.excluded_ids:
        return MembershipState.ABSENT
    if dialog_id is not None and dialog_id in rule.explicit_ids:
        return MembershipState.PRESENT
    if rule.kind is FolderRuleKind.CHATLIST:
        return MembershipState.ABSENT
    if facts is None:
        return MembershipState.UNKNOWN
    if rule.kind is FolderRuleKind.DEFAULT:
        if facts.archived is None:
            return MembershipState.UNKNOWN
        return MembershipState.ABSENT if facts.archived else MembershipState.PRESENT
    category = _category_state(rule, facts)
    if category is MembershipState.ABSENT:
        return category
    exclusions = _exclusions_state(rule, facts, now=now)
    if exclusions is MembershipState.ABSENT:
        return exclusions
    if category is MembershipState.UNKNOWN or exclusions is MembershipState.UNKNOWN:
        return MembershipState.UNKNOWN
    return MembershipState.PRESENT


def pin_position(rule: FolderRule, dialog_id: int) -> int | None:
    for position, peer_id in enumerate(rule.pinned_ids):
        if peer_id == dialog_id:
            return position
    return None


def _category_state(rule: FolderRule, facts: DialogFacts) -> MembershipState:
    if not rule.categories:
        return MembershipState.ABSENT
    if facts.category is None:
        return MembershipState.UNKNOWN
    return MembershipState.PRESENT if facts.category in rule.categories else MembershipState.ABSENT


def _exclusions_state(rule: FolderRule, facts: DialogFacts, *, now: int) -> MembershipState:
    unknown = False
    if rule.exclude_archived:
        if facts.archived is None:
            unknown = True
        elif facts.archived:
            return MembershipState.ABSENT
    if rule.exclude_read:
        if facts.unread is None:
            unknown = True
        elif not facts.unread:
            return MembershipState.ABSENT
    if rule.exclude_muted:
        if facts.mute_until is None:
            unknown = True
        elif facts.mute_until > now:
            return MembershipState.ABSENT
    return MembershipState.UNKNOWN if unknown else MembershipState.PRESENT


def matches(rule: FolderRule, facts: DialogFacts, *, now: int = 0) -> bool:
    return evaluate(rule, facts, now=now) is MembershipState.PRESENT
