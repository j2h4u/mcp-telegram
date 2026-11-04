"""Shared backoff policy for the durable Telegram safety owner."""

from dataclasses import dataclass
from enum import StrEnum


class RecoveryCause(StrEnum):
    FLOOD_WAIT = "flood_wait"
    TRANSIENT = "transient"
    READ_UNCERTAINTY = "read_uncertainty"


_BACKOFF = {
    RecoveryCause.FLOOD_WAIT: (3600, 86400),
    RecoveryCause.TRANSIENT: (30, 1800),
    RecoveryCause.READ_UNCERTAINTY: (300, 3600),
}


def backoff_seconds(cause: RecoveryCause, failure_stage: int) -> int:
    """Calculate a capped exponential delay without unbounded integer growth."""
    if isinstance(failure_stage, bool) or not isinstance(failure_stage, int) or failure_stage < 0:
        raise ValueError("failure_stage must be a nonnegative integer")
    base, cap = _BACKOFF[cause]
    return min(base << min(failure_stage, cap.bit_length()), cap)


@dataclass(frozen=True, slots=True)
class RecoveryFloor:
    """Validated persisted timing facts normalized by the ledger owner."""

    until_monotonic: float
    until_utc: float
    remaining_seconds: float
    same_boot: bool


def recovery_floor_release_at(floor: RecoveryFloor, *, now_monotonic: float, now_utc: float) -> float:
    """Calculate a conservative monotonic release using one observed clock pair."""
    utc_remaining = max(0.0, floor.until_utc - now_utc)
    if floor.same_boot:
        return max(floor.until_monotonic, now_monotonic + utc_remaining)
    return now_monotonic + max(utc_remaining, floor.remaining_seconds)


class RecoveryPhase(StrEnum):
    NONE = "none"
    AUTHORIZED = "authorized"
    FENCING = "fencing"
    WAITING = "waiting"
    CONSUMED = "consumed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXHAUSTED = "exhausted"


class RecoveryMode(StrEnum):
    NONE = "none"
    MANUAL = "manual"
    AUTO = "auto"


class RecoveryRun(StrEnum):
    CURRENT = "current"
    OTHER = "other"
    ABSENT = "absent"


class RecoveryEvent(StrEnum):
    FAILURE_SETTLED = "failure_settled"
    FAILURE_OBSERVED = "failure_observed"
    STARTUP_RECONCILE = "startup_reconcile"
    QUIESCED = "quiesced"
    DUE = "due"
    TRIAL_SUCCESS = "trial_success"
    STABILITY_CONFIRMED = "stability_confirmed"


class RecoveryCommand(StrEnum):
    NOOP = "noop"
    WAIT = "wait"
    REJECT = "reject"
    ARM_FENCE = "arm_fence"
    STRENGTHEN_FLOOD = "strengthen_flood"
    SET_WAIT = "set_wait"
    CONSUME = "consume"
    MARK_SUCCESS = "mark_success"
    RESET = "reset"


class RecoveryReason(StrEnum):
    HEALTHY = "healthy"
    FAILURE = "failure"
    INTERRUPTED_TRIAL = "interrupted_trial"
    PENDING_OWNER = "pending_owner"
    NOT_QUIESCED = "not_quiesced"
    HARD_HOLD = "hard_hold"
    DISABLED = "disabled"
    LIFECYCLE_UNAVAILABLE = "lifecycle_unavailable"
    NOT_DUE = "not_due"
    READY = "ready"
    FLOOD_BUDGET_SPENT = "flood_budget_spent"
    ALREADY_FENCED = "already_fenced"
    TRIAL_IN_PROGRESS = "trial_in_progress"
    INVALID_EVENT = "invalid_event"
    WRONG_RUN = "wrong_run"
    NOT_STABLE = "not_stable"
    STABLE = "stable"
    MANUAL_HOLD = "manual_hold"
    EXHAUSTED = "exhausted"
    LEGACY_HOLD = "legacy_hold"
    FLOOD_STRENGTHENED = "flood_strengthened"
    TRIAL_SUCCEEDED = "trial_succeeded"


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    """Finite, immutable facts normalized by the existing safety owner."""

    phase: RecoveryPhase
    mode: RecoveryMode
    run: RecoveryRun
    cause: RecoveryCause | None
    safe: bool
    owner_pending: bool
    quiesced: bool
    due: bool
    flood_budget_available: bool
    stability_confirmed: bool
    enabled: bool
    lifecycle_available: bool


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    next_phase: RecoveryPhase
    command: RecoveryCommand
    reason: RecoveryReason


class RecoveryStage(StrEnum):
    DECISION = "decision"
    GUARD = "guard"
    MONITOR = "monitor"
    AWAIT = "await"
    COMMIT = "commit"


class RecoveryObservationEvent(StrEnum):
    DECISION = "decision"
    STARTUP_UNPREPARED = "startup_unprepared"
    LIFECYCLE_UNAVAILABLE = "lifecycle_unavailable"
    PENDING_COOLDOWN = "pending_cooldown"
    PENDING_RECEIPT = "pending_receipt"
    PENDING_LEDGER = "pending_ledger"
    TRANSPORT_HOLD = "transport_hold"
    MONITOR_EVALUATE_START = "monitor_evaluate_start"
    MONITOR_EVALUATE_RETURN = "monitor_evaluate_return"
    MONITOR_EVALUATE_FAILED = "monitor_evaluate_failed"
    MONITOR_EVALUATE_CANCELLED = "monitor_evaluate_cancelled"
    MONITOR_SLEEP_START = "monitor_sleep_start"
    MONITOR_SLEEP_RETURN = "monitor_sleep_return"
    MONITOR_SLEEP_FAILED = "monitor_sleep_failed"
    MONITOR_SLEEP_CANCELLED = "monitor_sleep_cancelled"
    MONITOR_RESTART = "monitor_restart"
    AWAIT_ENTERED = "await_entered"
    AWAIT_COMPLETED = "await_completed"
    AWAIT_CANCELLED = "await_cancelled"
    AWAIT_FAILED = "await_failed"
    CONSUMED = "consumed"
    SUCCEEDED = "succeeded"
    WAITING = "waiting"
    RESET = "reset"


class RecoveryAction(StrEnum):
    FAILURE_SETTLED = "failure_settled"
    MAINTENANCE = "maintenance"
    QUIESCE = "quiesce"
    TRANSITION = "transition"
    CONSUME = "consume"
    RESET = "reset"
    SUCCESS = "success"
    LEDGER_MAINTAIN = "ledger_maintain"
    DISCONNECT = "disconnect"
    STARTUP_RECONCILE = "startup_reconcile"
    STARTUP_CONSUME = "startup_consume"
    SAVE = "save"


@dataclass(frozen=True, slots=True)
class RecoveryObservation:
    """Only finite, content-free facts from an actual recovery action."""

    stage: RecoveryStage
    event: RecoveryObservationEvent
    observed_at_ms: int
    action: RecoveryAction | None = None
    policy_event: RecoveryEvent | None = None
    elapsed_ms: float | None = None
    phase: RecoveryPhase | None = None
    cause: RecoveryCause | None = None
    command: RecoveryCommand | None = None
    reason: RecoveryReason | None = None
    snapshot: RecoverySnapshot | None = None
    episode_id: str | None = None
    grant_id: str | None = None
    attempts_total: int | None = None
    failure_stage: int | None = None
    scheduled_due_utc: float | None = None
    restriction_floor_utc: float | None = None
    effective_eligible_utc: float | None = None
    eligible_reason: str | None = None


class _Rule(StrEnum):
    ARM = "arm"
    ACTIVE_FAILURE = "active_failure"
    HOLD = "hold"
    CONSUMED_FAILURE = "consumed_failure"
    CONSUMED_OBSERVED = "consumed_observed"
    CONSUMED_STARTUP = "consumed_startup"
    QUIESCE = "quiesce"
    DUE = "due"
    SUCCESS = "success"
    STABILITY = "stability"
    REJECT = "reject"


# Each row covers the seven events in RecoveryEvent declaration order.
_RULE_ROWS: dict[RecoveryPhase, tuple[_Rule, ...]] = {
    RecoveryPhase.NONE: (_Rule.ARM, _Rule.ARM, _Rule.ARM, _Rule.REJECT, _Rule.REJECT, _Rule.REJECT, _Rule.REJECT),
    RecoveryPhase.AUTHORIZED: (
        _Rule.HOLD,
        _Rule.HOLD,
        _Rule.HOLD,
        _Rule.REJECT,
        _Rule.REJECT,
        _Rule.REJECT,
        _Rule.REJECT,
    ),
    RecoveryPhase.FENCING: (
        _Rule.ACTIVE_FAILURE,
        _Rule.HOLD,
        _Rule.HOLD,
        _Rule.QUIESCE,
        _Rule.REJECT,
        _Rule.REJECT,
        _Rule.REJECT,
    ),
    RecoveryPhase.WAITING: (
        _Rule.ACTIVE_FAILURE,
        _Rule.HOLD,
        _Rule.HOLD,
        _Rule.REJECT,
        _Rule.DUE,
        _Rule.REJECT,
        _Rule.REJECT,
    ),
    RecoveryPhase.CONSUMED: (
        _Rule.CONSUMED_FAILURE,
        _Rule.CONSUMED_OBSERVED,
        _Rule.CONSUMED_STARTUP,
        _Rule.REJECT,
        _Rule.REJECT,
        _Rule.SUCCESS,
        _Rule.REJECT,
    ),
    RecoveryPhase.SUCCEEDED: (
        _Rule.ARM,
        _Rule.ARM,
        _Rule.ARM,
        _Rule.REJECT,
        _Rule.REJECT,
        _Rule.REJECT,
        _Rule.STABILITY,
    ),
    RecoveryPhase.FAILED: (_Rule.HOLD, _Rule.HOLD, _Rule.HOLD, _Rule.REJECT, _Rule.REJECT, _Rule.REJECT, _Rule.REJECT),
    RecoveryPhase.EXHAUSTED: (
        _Rule.HOLD,
        _Rule.HOLD,
        _Rule.HOLD,
        _Rule.REJECT,
        _Rule.REJECT,
        _Rule.REJECT,
        _Rule.REJECT,
    ),
}


def decide_auto_recovery(snapshot: RecoverySnapshot, event: RecoveryEvent) -> RecoveryDecision:
    """Decide one explicit recovery cell without performing any effect."""
    if not isinstance(snapshot, RecoverySnapshot) or not isinstance(event, RecoveryEvent):
        raise TypeError("recovery decision requires typed snapshot and event")
    if not isinstance(snapshot.phase, RecoveryPhase) or not isinstance(snapshot.mode, RecoveryMode):
        raise TypeError("invalid recovery phase or mode")
    if not isinstance(snapshot.run, RecoveryRun) or (
        snapshot.cause is not None and not isinstance(snapshot.cause, RecoveryCause)
    ):
        raise TypeError("invalid recovery run or cause")
    guards = (
        snapshot.safe,
        snapshot.owner_pending,
        snapshot.quiesced,
        snapshot.due,
        snapshot.flood_budget_available,
        snapshot.stability_confirmed,
        snapshot.enabled,
        snapshot.lifecycle_available,
    )
    if any(type(guard) is not bool for guard in guards):
        raise TypeError("recovery guards must be bool")
    rule = _RULE_ROWS[snapshot.phase][tuple(RecoveryEvent).index(event)]
    return _decide_rule(snapshot, rule)


def _decision(
    snapshot: RecoverySnapshot, command: RecoveryCommand, reason: RecoveryReason, phase: RecoveryPhase | None = None
) -> RecoveryDecision:
    return RecoveryDecision(phase if phase is not None else snapshot.phase, command, reason)


def _guard(snapshot: RecoverySnapshot, *, barrier: bool) -> RecoveryReason | None:
    if not snapshot.safe:
        return RecoveryReason.HARD_HOLD
    if not snapshot.enabled:
        return RecoveryReason.DISABLED
    if not snapshot.lifecycle_available:
        return RecoveryReason.LIFECYCLE_UNAVAILABLE
    if barrier and snapshot.owner_pending:
        return RecoveryReason.PENDING_OWNER
    return None


def _decide_rule(snapshot: RecoverySnapshot, rule: _Rule) -> RecoveryDecision:
    base = _base_decision(snapshot, rule)
    if base is not None:
        return base
    if rule in {_Rule.ARM, _Rule.CONSUMED_FAILURE, _Rule.CONSUMED_OBSERVED, _Rule.CONSUMED_STARTUP}:
        return _failure_decision(snapshot, rule)
    if snapshot.mode is not RecoveryMode.AUTO:
        return _decision(snapshot, RecoveryCommand.WAIT, RecoveryReason.MANUAL_HOLD)
    return _decide_auto_action(snapshot, rule)


def _base_decision(snapshot: RecoverySnapshot, rule: _Rule) -> RecoveryDecision | None:
    if rule is _Rule.REJECT:
        return _decision(snapshot, RecoveryCommand.REJECT, RecoveryReason.INVALID_EVENT)
    mode_hold = _mode_hold(snapshot)
    if mode_hold is not None:
        return _decision(snapshot, RecoveryCommand.WAIT, mode_hold)
    if rule is _Rule.HOLD:
        reason = (
            RecoveryReason.EXHAUSTED
            if snapshot.phase is RecoveryPhase.EXHAUSTED
            else (
                RecoveryReason.ALREADY_FENCED
                if snapshot.phase in {RecoveryPhase.FENCING, RecoveryPhase.WAITING}
                else RecoveryReason.LEGACY_HOLD
            )
        )
        return _decision(snapshot, RecoveryCommand.WAIT, reason)
    return None


def _mode_hold(snapshot: RecoverySnapshot) -> RecoveryReason | None:
    if snapshot.mode is RecoveryMode.MANUAL and snapshot.phase is not RecoveryPhase.SUCCEEDED:
        return RecoveryReason.MANUAL_HOLD
    if snapshot.mode is RecoveryMode.NONE and snapshot.phase is not RecoveryPhase.NONE:
        return RecoveryReason.LEGACY_HOLD
    if snapshot.mode is RecoveryMode.AUTO and snapshot.phase is RecoveryPhase.NONE:
        return RecoveryReason.LEGACY_HOLD
    return None


def _decide_auto_action(snapshot: RecoverySnapshot, rule: _Rule) -> RecoveryDecision:
    if rule is _Rule.ACTIVE_FAILURE:
        return _strengthen_decision(snapshot)
    if rule is _Rule.QUIESCE:
        return _quiesce_decision(snapshot)
    if rule is _Rule.DUE:
        return _due_decision(snapshot)
    if rule is _Rule.SUCCESS:
        return _success_decision(snapshot)
    if rule is _Rule.STABILITY:
        return _stability_decision(snapshot)
    raise ValueError(f"unhandled recovery rule: {rule}")


def _failure_decision(snapshot: RecoverySnapshot, rule: _Rule) -> RecoveryDecision:
    interrupted = rule is _Rule.CONSUMED_STARTUP and snapshot.run is RecoveryRun.OTHER
    preliminary = _failure_precondition(snapshot, rule, interrupted=interrupted)
    if preliminary is not None:
        return preliminary
    hold = _guard(snapshot, barrier=False)
    if hold is not None:
        return _decision(snapshot, RecoveryCommand.WAIT, hold)
    exhausted = snapshot.cause is RecoveryCause.FLOOD_WAIT and not snapshot.flood_budget_available
    return _decision(
        snapshot,
        RecoveryCommand.ARM_FENCE,
        RecoveryReason.FLOOD_BUDGET_SPENT
        if exhausted
        else (RecoveryReason.INTERRUPTED_TRIAL if interrupted else RecoveryReason.FAILURE),
        RecoveryPhase.EXHAUSTED if exhausted else RecoveryPhase.FENCING,
    )


def _failure_precondition(snapshot: RecoverySnapshot, rule: _Rule, *, interrupted: bool) -> RecoveryDecision | None:
    if rule is _Rule.CONSUMED_FAILURE and snapshot.run is not RecoveryRun.CURRENT:
        return _decision(snapshot, RecoveryCommand.WAIT, RecoveryReason.WRONG_RUN)
    if rule in {_Rule.CONSUMED_OBSERVED, _Rule.CONSUMED_STARTUP} and not interrupted:
        return _decision(snapshot, RecoveryCommand.WAIT, RecoveryReason.TRIAL_IN_PROGRESS)
    if not interrupted and snapshot.cause is None:
        return _decision(snapshot, RecoveryCommand.NOOP, RecoveryReason.HEALTHY)
    return None


def _strengthen_decision(snapshot: RecoverySnapshot) -> RecoveryDecision:
    if snapshot.cause is not RecoveryCause.FLOOD_WAIT:
        return _decision(snapshot, RecoveryCommand.WAIT, RecoveryReason.ALREADY_FENCED)
    hold = _guard(snapshot, barrier=False)
    if hold is not None:
        return _decision(snapshot, RecoveryCommand.WAIT, hold)
    exhausted = not snapshot.flood_budget_available
    return _decision(
        snapshot,
        RecoveryCommand.STRENGTHEN_FLOOD,
        RecoveryReason.FLOOD_BUDGET_SPENT if exhausted else RecoveryReason.FLOOD_STRENGTHENED,
        RecoveryPhase.EXHAUSTED if exhausted else snapshot.phase,
    )


def _quiesce_decision(snapshot: RecoverySnapshot) -> RecoveryDecision:
    hold = _guard(snapshot, barrier=True)
    if hold is not None:
        return _decision(snapshot, RecoveryCommand.WAIT, hold)
    if not snapshot.quiesced:
        return _decision(snapshot, RecoveryCommand.WAIT, RecoveryReason.NOT_QUIESCED)
    return _decision(snapshot, RecoveryCommand.SET_WAIT, RecoveryReason.READY, RecoveryPhase.WAITING)


def _due_decision(snapshot: RecoverySnapshot) -> RecoveryDecision:
    hold = _guard(snapshot, barrier=True)
    if hold is not None:
        return _decision(snapshot, RecoveryCommand.WAIT, hold)
    if snapshot.cause is RecoveryCause.FLOOD_WAIT and not snapshot.flood_budget_available:
        return _decision(snapshot, RecoveryCommand.WAIT, RecoveryReason.FLOOD_BUDGET_SPENT)
    if not snapshot.due:
        return _decision(snapshot, RecoveryCommand.WAIT, RecoveryReason.NOT_DUE)
    return _decision(snapshot, RecoveryCommand.CONSUME, RecoveryReason.READY, RecoveryPhase.CONSUMED)


def _success_decision(snapshot: RecoverySnapshot) -> RecoveryDecision:
    if snapshot.run is not RecoveryRun.CURRENT:
        return _decision(snapshot, RecoveryCommand.REJECT, RecoveryReason.WRONG_RUN)
    hold = _guard(snapshot, barrier=True)
    if hold is not None:
        return _decision(snapshot, RecoveryCommand.WAIT, hold)
    if snapshot.cause is not None:
        return _decision(snapshot, RecoveryCommand.REJECT, RecoveryReason.FAILURE)
    return _decision(snapshot, RecoveryCommand.MARK_SUCCESS, RecoveryReason.TRIAL_SUCCEEDED, RecoveryPhase.SUCCEEDED)


def _stability_decision(snapshot: RecoverySnapshot) -> RecoveryDecision:
    hold = _guard(snapshot, barrier=True)
    if hold is not None:
        return _decision(snapshot, RecoveryCommand.WAIT, hold)
    if snapshot.cause is not None:
        return _decision(snapshot, RecoveryCommand.REJECT, RecoveryReason.FAILURE)
    if not snapshot.stability_confirmed:
        return _decision(snapshot, RecoveryCommand.WAIT, RecoveryReason.NOT_STABLE)
    return _decision(snapshot, RecoveryCommand.RESET, RecoveryReason.STABLE, RecoveryPhase.NONE)
