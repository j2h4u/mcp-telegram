"""Owner-issued identities and committed outcomes for durable demand slices.

Owners derive targets and revisions from persisted business state, never from a
slice UUID, retry timestamp or lease. These values do not grant RPC eligibility.
"""

from dataclasses import dataclass
from enum import StrEnum

from mcp_telegram.telegram_rpc_consumers import DemandKind


@dataclass(frozen=True, slots=True)
class WorkKey:
    """One account's stable owner target, independent of its current cursor."""

    demand_kind: DemandKind
    account_id: int
    target: str

    def __post_init__(self) -> None:
        if not isinstance(self.demand_kind, DemandKind):
            raise TypeError("demand_kind must be a DemandKind")
        if type(self.account_id) is not int or self.account_id <= 0:
            raise ValueError("account_id must be a positive integer")
        _require_token(self.target, "target")


@dataclass(frozen=True, slots=True)
class WorkRevision:
    """Exact persisted version/cursor selected by the owner for a slice.

    The revision is an opaque deterministic token for the owner's existing
    generation, phase and cursor; consumers compare equality, not ordering.
    """

    key: WorkKey
    revision: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, WorkKey):
            raise TypeError("key must be a WorkKey")
        _require_token(self.revision, "revision")


class SliceOutcomeKind(StrEnum):
    """Business outcomes; a successful fetch alone is not progress."""

    PROGRESSED = "progressed"
    TERMINAL = "terminal"
    DEFERRED = "deferred"
    RETRYABLE_FAILURE = "retryable_failure"
    STALE = "stale"
    NO_PROGRESS = "no_progress"


@dataclass(frozen=True, slots=True)
class SliceOutcome:
    """Receipt issued after the owner's transaction accepts or rejects a slice.

    Only an accepted progress/terminal transition supplies a new revision.
    A rejected CAS reports STALE even if the fetched RPC succeeded.
    """

    selected: WorkRevision
    kind: SliceOutcomeKind
    committed_revision: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.selected, WorkRevision):
            raise TypeError("selected must be a WorkRevision")
        if not isinstance(self.kind, SliceOutcomeKind):
            raise TypeError("kind must be a SliceOutcomeKind")
        if self.kind in {SliceOutcomeKind.PROGRESSED, SliceOutcomeKind.TERMINAL}:
            _require_token(self.committed_revision, "committed_revision")
            if self.committed_revision == self.selected.revision:
                raise ValueError("committed_revision must represent a changed business state")
        elif self.committed_revision is not None:
            raise ValueError("uncommitted outcomes cannot supply committed_revision")

    def committed_revision_for(self, selected: WorkRevision) -> str | None:
        """Return accepted progress only for the exact selection it consumed."""
        return self.committed_revision if self.selected == selected else None


def _require_token(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
