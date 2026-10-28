from dataclasses import replace

import pytest

from mcp_telegram.telegram_demand_progress import SliceOutcome, SliceOutcomeKind, WorkKey, WorkRevision
from mcp_telegram.telegram_rpc_consumers import DemandKind


def _profile_selection() -> WorkRevision:
    return WorkRevision(
        WorkKey(DemandKind.ENTITY_PROFILE_REFRESH, 42, "user:123"), "generation=3;section=core;cursor=0"
    )


@pytest.mark.parametrize("kind", [SliceOutcomeKind.PROGRESSED, SliceOutcomeKind.TERMINAL])
def test_committed_outcome_is_bound_to_the_consumed_owner_revision(kind: SliceOutcomeKind) -> None:
    selected = _profile_selection()
    committed = "generation=3;section=complete;cursor=0"
    receipt = SliceOutcome(selected, kind, committed)

    assert receipt.committed_revision_for(_profile_selection()) == committed
    assert receipt.committed_revision_for(replace(selected, revision=committed)) is None
    for key in (
        replace(selected.key, account_id=43),
        replace(selected.key, target="user:456"),
        replace(selected.key, demand_kind=DemandKind.TOPIC_SNAPSHOT),
    ):
        assert receipt.committed_revision_for(replace(selected, key=key)) is None


@pytest.mark.parametrize("kind", [SliceOutcomeKind.DEFERRED, SliceOutcomeKind.STALE, SliceOutcomeKind.NO_PROGRESS])
def test_fetch_without_accepted_commit_cannot_report_progress(kind: SliceOutcomeKind) -> None:
    selected = _profile_selection()
    assert SliceOutcome(selected, kind).committed_revision_for(selected) is None
    with pytest.raises(ValueError, match="uncommitted outcomes"):
        SliceOutcome(selected, kind, "newer-state-observed-after-rejected-cas")


@pytest.mark.parametrize("kind", [SliceOutcomeKind.PROGRESSED, SliceOutcomeKind.TERMINAL])
@pytest.mark.parametrize("committed", [None, "", " ", "generation=3;section=core;cursor=0"])
def test_progress_requires_an_accepted_changed_revision(kind: SliceOutcomeKind, committed: str | None) -> None:
    with pytest.raises(ValueError, match="committed_revision"):
        SliceOutcome(_profile_selection(), kind, committed)


def test_page_progress_keeps_logical_key_and_opaque_revision_is_not_ordered() -> None:
    key = WorkKey(DemandKind.FULL_SYNC_PAGE, 42, "dialog:-100123:history")
    selected = WorkRevision(key, "generation=7;offset=100")
    receipt = SliceOutcome(selected, SliceOutcomeKind.PROGRESSED, "generation=7;offset=50")

    assert receipt.committed_revision_for(selected) == "generation=7;offset=50"
    assert len({key, WorkKey(DemandKind.FULL_SYNC_PAGE, 42, "dialog:-100123:history")}) == 1


@pytest.mark.parametrize("account_id", [0, -1, True])
def test_work_identity_requires_a_canonical_account(account_id: int) -> None:
    with pytest.raises(ValueError, match="account_id"):
        WorkKey(DemandKind.FULL_SYNC_PAGE, account_id, "dialog:123")


@pytest.mark.parametrize("token", ["", " "])
def test_empty_owner_identity_and_revision_are_rejected(token: str) -> None:
    with pytest.raises(ValueError, match="target"):
        WorkKey(DemandKind.FULL_SYNC_PAGE, 42, token)
    with pytest.raises(ValueError, match="revision"):
        WorkRevision(_profile_selection().key, token)
