"""Three-valued local evaluation of Telegram folder rules."""

from __future__ import annotations

from .contracts import DialogFacts, FolderRule, FolderRuleKind, MembershipState


def evaluate(rule: FolderRule, facts: DialogFacts | None, *, now: int) -> MembershipState:
    """Evaluate one current canonical fact without inventing absence from gaps."""
    state = _excluded_state(rule, facts)
    if state is not None:
        return state
    state = _explicit_state(rule, facts)
    if state is not None:
        return state
    state = _chatlist_state(rule)
    if state is not None:
        return state
    state = _missing_facts_state(facts)
    if state is not None:
        return state
    assert facts is not None
    if rule.kind is FolderRuleKind.DEFAULT:
        return _default_state(facts)
    return _filter_state(rule, facts, now=now)


def _excluded_state(rule: FolderRule, facts: DialogFacts | None) -> MembershipState | None:
    if facts is not None and facts.dialog_id in rule.excluded_ids:
        return MembershipState.ABSENT
    return None


def _explicit_state(rule: FolderRule, facts: DialogFacts | None) -> MembershipState | None:
    if facts is not None and facts.dialog_id in rule.explicit_ids:
        return MembershipState.PRESENT
    return None


def _chatlist_state(rule: FolderRule) -> MembershipState | None:
    if rule.kind is FolderRuleKind.CHATLIST:
        return MembershipState.ABSENT
    return None


def _missing_facts_state(facts: DialogFacts | None) -> MembershipState | None:
    if facts is None:
        return MembershipState.UNKNOWN
    return None


def _default_state(facts: DialogFacts) -> MembershipState:
    if facts.archived is None:
        return MembershipState.UNKNOWN
    return MembershipState.ABSENT if facts.archived else MembershipState.PRESENT


def _filter_state(rule: FolderRule, facts: DialogFacts, *, now: int) -> MembershipState:
    return _compose_states(_category_state(rule, facts), _exclusions_state(rule, facts, now=now))


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
    read_excluded = None if facts.unread is None else not facts.unread
    mute_excluded = None if facts.mute_until is None else facts.mute_until > now
    states = (
        _optional_exclusion_state(rule.exclude_archived, facts.archived),
        _optional_exclusion_state(rule.exclude_read, read_excluded),
        _optional_exclusion_state(rule.exclude_muted, mute_excluded),
    )
    if MembershipState.ABSENT in states:
        return MembershipState.ABSENT
    if MembershipState.UNKNOWN in states:
        return MembershipState.UNKNOWN
    return MembershipState.PRESENT


def _optional_exclusion_state(enabled: bool, excluded: bool | None) -> MembershipState | None:
    if not enabled:
        return None
    if excluded is None:
        return MembershipState.UNKNOWN
    return MembershipState.ABSENT if excluded else MembershipState.PRESENT


def _compose_states(category: MembershipState, exclusions: MembershipState) -> MembershipState:
    if MembershipState.ABSENT in (category, exclusions):
        return MembershipState.ABSENT
    if MembershipState.UNKNOWN in (category, exclusions):
        return MembershipState.UNKNOWN
    return MembershipState.PRESENT


def matches(rule: FolderRule, facts: DialogFacts, *, now: int = 0) -> bool:
    return evaluate(rule, facts, now=now) is MembershipState.PRESENT
