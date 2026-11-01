from dataclasses import FrozenInstanceError, replace
from itertools import product

import pytest

from mcp_telegram.telegram_rpc_recovery import (
    RecoveryCause,
    RecoveryDecision,
    RecoveryFloor,
    RecoverySnapshot,
    backoff_seconds,
    decide_auto_recovery,
    recovery_floor_release_at,
)
from mcp_telegram.telegram_rpc_recovery import (
    RecoveryCommand as C,
)
from mcp_telegram.telegram_rpc_recovery import (
    RecoveryEvent as E,
)
from mcp_telegram.telegram_rpc_recovery import (
    RecoveryMode as M,
)
from mcp_telegram.telegram_rpc_recovery import (
    RecoveryPhase as P,
)
from mcp_telegram.telegram_rpc_recovery import (
    RecoveryReason as R,
)
from mcp_telegram.telegram_rpc_recovery import (
    RecoveryRun as Run,
)


def snapshot(phase: P, **changes: object) -> RecoverySnapshot:
    value = RecoverySnapshot(
        phase=phase,
        mode=M.NONE if phase is P.NONE else M.AUTO,
        run=Run.CURRENT,
        cause=None,
        safe=True,
        owner_pending=False,
        quiesced=True,
        due=True,
        flood_budget_available=True,
        stability_confirmed=True,
        enabled=True,
        lifecycle_available=True,
    )
    return replace(value, **changes)


_INVALID = (C.REJECT, R.INVALID_EVENT)
_HEALTHY = (C.NOOP, R.HEALTHY)
_LEGACY = (C.WAIT, R.LEGACY_HOLD)
_FENCED = (C.WAIT, R.ALREADY_FENCED)
_TRIAL = (C.WAIT, R.TRIAL_IN_PROGRESS)
_EVENT_COLUMNS = (
    E.FAILURE_SETTLED,
    E.FAILURE_OBSERVED,
    E.STARTUP_RECONCILE,
    E.QUIESCED,
    E.DUE,
    E.TRIAL_SUCCESS,
    E.STABILITY_CONFIRMED,
)
_EXPECTED: dict[P, tuple[tuple[C, R], ...]] = {
    P.NONE: (_HEALTHY, _HEALTHY, _HEALTHY, _INVALID, _INVALID, _INVALID, _INVALID),
    P.AUTHORIZED: (_LEGACY, _LEGACY, _LEGACY, _INVALID, _INVALID, _INVALID, _INVALID),
    P.FENCING: (_FENCED, _FENCED, _FENCED, (C.SET_WAIT, R.READY), _INVALID, _INVALID, _INVALID),
    P.WAITING: (_FENCED, _FENCED, _FENCED, _INVALID, (C.CONSUME, R.READY), _INVALID, _INVALID),
    P.CONSUMED: (_HEALTHY, _TRIAL, _TRIAL, _INVALID, _INVALID, (C.MARK_SUCCESS, R.TRIAL_SUCCEEDED), _INVALID),
    P.SUCCEEDED: (_HEALTHY, _HEALTHY, _HEALTHY, _INVALID, _INVALID, _INVALID, (C.RESET, R.STABLE)),
    P.FAILED: (_LEGACY, _LEGACY, _LEGACY, _INVALID, _INVALID, _INVALID, _INVALID),
    P.EXHAUSTED: ((C.WAIT, R.EXHAUSTED),) * 3 + (_INVALID,) * 4,
}


def test_every_phase_event_pair_has_independent_expected_decision() -> None:
    assert len(P) == 8 and len(E) == 7
    assert set(_EXPECTED) == set(P)
    assert len(_EVENT_COLUMNS) == len(set(_EVENT_COLUMNS)) and set(_EVENT_COLUMNS) == set(E)
    for phase, event in product(P, E):
        command, reason = _EXPECTED[phase][_EVENT_COLUMNS.index(event)]
        target = {
            C.SET_WAIT: P.WAITING,
            C.CONSUME: P.CONSUMED,
            C.MARK_SUCCESS: P.SUCCEEDED,
            C.RESET: P.NONE,
        }.get(command, phase)
        assert decide_auto_recovery(snapshot(phase), event) == RecoveryDecision(target, command, reason)


@pytest.mark.parametrize("mode", [M.AUTO, M.MANUAL])
@pytest.mark.parametrize("run", list(Run))
@pytest.mark.parametrize("event", [E.FAILURE_SETTLED, E.FAILURE_OBSERVED, E.STARTUP_RECONCILE])
def test_success_then_new_blocker_rearms_in_any_run(mode: M, run: Run, event: E) -> None:
    state = snapshot(P.SUCCEEDED, mode=mode, run=run, cause=RecoveryCause.READ_UNCERTAINTY)
    assert decide_auto_recovery(state, event) == RecoveryDecision(P.FENCING, C.ARM_FENCE, R.FAILURE)


@pytest.mark.parametrize("event", [E.FAILURE_SETTLED, E.FAILURE_OBSERVED, E.STARTUP_RECONCILE])
def test_first_failure_arms_and_settling_own_claim_does_not_block(event: E) -> None:
    state = snapshot(P.NONE, cause=RecoveryCause.TRANSIENT, owner_pending=True)
    assert decide_auto_recovery(state, event) == RecoveryDecision(P.FENCING, C.ARM_FENCE, R.FAILURE)


def test_interrupted_other_run_rearms_but_current_trial_waits() -> None:
    assert decide_auto_recovery(snapshot(P.CONSUMED, run=Run.OTHER), E.STARTUP_RECONCILE) == RecoveryDecision(
        P.FENCING, C.ARM_FENCE, R.INTERRUPTED_TRIAL
    )
    for run in (Run.CURRENT, Run.ABSENT):
        assert decide_auto_recovery(snapshot(P.CONSUMED, run=run), E.STARTUP_RECONCILE).command is C.WAIT
    assert decide_auto_recovery(snapshot(P.CONSUMED, run=Run.OTHER), E.TRIAL_SUCCESS).reason is R.WRONG_RUN
    for run in (Run.OTHER, Run.ABSENT):
        stale = snapshot(P.CONSUMED, run=run, cause=RecoveryCause.TRANSIENT)
        assert decide_auto_recovery(stale, E.FAILURE_SETTLED) == RecoveryDecision(P.CONSUMED, C.WAIT, R.WRONG_RUN)


@pytest.mark.parametrize("phase", [P.FENCING, P.WAITING])
def test_only_actual_new_flood_strengthens_active_episode(phase: P) -> None:
    state = snapshot(phase, cause=RecoveryCause.FLOOD_WAIT)
    assert decide_auto_recovery(state, E.FAILURE_SETTLED) == RecoveryDecision(
        phase, C.STRENGTHEN_FLOOD, R.FLOOD_STRENGTHENED
    )
    for event in (E.FAILURE_OBSERVED, E.STARTUP_RECONCILE):
        assert decide_auto_recovery(state, event) == RecoveryDecision(phase, C.WAIT, R.ALREADY_FENCED)
    spent = replace(state, flood_budget_available=False)
    assert decide_auto_recovery(spent, E.FAILURE_SETTLED) == RecoveryDecision(
        P.EXHAUSTED, C.STRENGTHEN_FLOOD, R.FLOOD_BUDGET_SPENT
    )


@pytest.mark.parametrize(
    "field,reason", [("safe", R.HARD_HOLD), ("enabled", R.DISABLED), ("lifecycle_available", R.LIFECYCLE_UNAVAILABLE)]
)
@pytest.mark.parametrize(
    "phase,event",
    [
        (P.NONE, E.FAILURE_SETTLED),
        (P.FENCING, E.QUIESCED),
        (P.WAITING, E.DUE),
        (P.CONSUMED, E.TRIAL_SUCCESS),
        (P.SUCCEEDED, E.STABILITY_CONFIRMED),
    ],
)
def test_global_guards_block_grants(field: str, reason: R, phase: P, event: E) -> None:
    cause = RecoveryCause.TRANSIENT if phase is P.NONE else None
    decision = decide_auto_recovery(snapshot(phase, cause=cause, **{field: False}), event)
    assert decision == RecoveryDecision(phase, C.WAIT, reason)


@pytest.mark.parametrize(
    "phase,event",
    [(P.FENCING, E.QUIESCED), (P.WAITING, E.DUE), (P.CONSUMED, E.TRIAL_SUCCESS), (P.SUCCEEDED, E.STABILITY_CONFIRMED)],
)
def test_pending_owner_barrier_blocks_grants(phase: P, event: E) -> None:
    assert decide_auto_recovery(snapshot(phase, owner_pending=True), event) == RecoveryDecision(
        phase, C.WAIT, R.PENDING_OWNER
    )


def test_quiet_deadline_stability_and_flood_budget_guards() -> None:
    assert decide_auto_recovery(snapshot(P.FENCING, quiesced=False), E.QUIESCED).reason is R.NOT_QUIESCED
    assert decide_auto_recovery(snapshot(P.WAITING, due=False), E.DUE).reason is R.NOT_DUE
    assert (
        decide_auto_recovery(snapshot(P.SUCCEEDED, stability_confirmed=False), E.STABILITY_CONFIRMED).reason
        is R.NOT_STABLE
    )
    waiting = snapshot(P.WAITING, cause=RecoveryCause.FLOOD_WAIT, flood_budget_available=False)
    assert decide_auto_recovery(waiting, E.DUE) == RecoveryDecision(P.WAITING, C.WAIT, R.FLOOD_BUDGET_SPENT)
    new = snapshot(P.NONE, cause=RecoveryCause.FLOOD_WAIT, flood_budget_available=False)
    assert decide_auto_recovery(new, E.FAILURE_SETTLED) == RecoveryDecision(
        P.EXHAUSTED, C.ARM_FENCE, R.FLOOD_BUDGET_SPENT
    )


@pytest.mark.parametrize("phase", [P.AUTHORIZED, P.FENCING, P.WAITING, P.CONSUMED, P.FAILED, P.EXHAUSTED])
def test_manual_authority_never_replaced_by_automatic_observation(phase: P) -> None:
    state = snapshot(phase, mode=M.MANUAL, cause=RecoveryCause.READ_UNCERTAINTY)
    assert decide_auto_recovery(state, E.FAILURE_OBSERVED) == RecoveryDecision(phase, C.WAIT, R.MANUAL_HOLD)


def test_immutable_snapshot_and_backoff_contract() -> None:
    state = snapshot(P.NONE)
    with pytest.raises(FrozenInstanceError):
        state.safe = False  # type: ignore[misc]
    assert backoff_seconds(RecoveryCause.FLOOD_WAIT, 0) == 3600
    assert backoff_seconds(RecoveryCause.TRANSIENT, 100) == 1800
    with pytest.raises(ValueError):
        backoff_seconds(RecoveryCause.TRANSIENT, -1)


@pytest.mark.parametrize("phase", list(P))
@pytest.mark.parametrize("mode", list(M))
def test_every_phase_and_mode_is_classified_explicitly(phase: P, mode: M) -> None:
    state = snapshot(phase, mode=mode, cause=RecoveryCause.READ_UNCERTAINTY)
    result = decide_auto_recovery(state, E.FAILURE_OBSERVED)
    if mode is M.MANUAL and phase is not P.SUCCEEDED:
        assert result == RecoveryDecision(phase, C.WAIT, R.MANUAL_HOLD)
    elif (mode is M.AUTO and phase is P.NONE) or (mode is M.NONE and phase is not P.NONE):
        assert result == RecoveryDecision(phase, C.WAIT, R.LEGACY_HOLD)
    else:
        assert isinstance(result.command, C) and isinstance(result.reason, R)


@pytest.mark.parametrize("run", [Run.CURRENT, Run.OTHER, Run.ABSENT])
def test_success_restart_without_blocker_does_not_rearm(run: Run) -> None:
    state = snapshot(P.SUCCEEDED, run=run)
    for event in (E.FAILURE_OBSERVED, E.STARTUP_RECONCILE):
        assert decide_auto_recovery(state, event) == RecoveryDecision(P.SUCCEEDED, C.NOOP, R.HEALTHY)


def test_current_trial_failure_then_quiesce_due_success_stability() -> None:
    failed = snapshot(P.CONSUMED, cause=RecoveryCause.READ_UNCERTAINTY)
    assert decide_auto_recovery(failed, E.FAILURE_SETTLED).next_phase is P.FENCING
    assert decide_auto_recovery(snapshot(P.FENCING, owner_pending=True), E.QUIESCED).reason is R.PENDING_OWNER
    assert decide_auto_recovery(snapshot(P.FENCING), E.QUIESCED).next_phase is P.WAITING
    assert decide_auto_recovery(snapshot(P.WAITING), E.DUE).next_phase is P.CONSUMED
    assert decide_auto_recovery(snapshot(P.CONSUMED), E.TRIAL_SUCCESS).next_phase is P.SUCCEEDED
    assert decide_auto_recovery(snapshot(P.SUCCEEDED), E.STABILITY_CONFIRMED).next_phase is P.NONE


@pytest.mark.parametrize("event", [E.TRIAL_SUCCESS, E.STABILITY_CONFIRMED])
def test_new_blocker_forbids_success_or_stability_reset(event: E) -> None:
    phase = P.CONSUMED if event is E.TRIAL_SUCCESS else P.SUCCEEDED
    state = snapshot(phase, cause=RecoveryCause.READ_UNCERTAINTY)
    assert decide_auto_recovery(state, event) == RecoveryDecision(phase, C.REJECT, R.FAILURE)


def test_invalid_guard_types_are_rejected() -> None:
    with pytest.raises(TypeError, match="guards must be bool"):
        decide_auto_recovery(snapshot(P.NONE, safe=1), E.FAILURE_OBSERVED)
    with pytest.raises(TypeError, match="typed snapshot"):
        decide_auto_recovery(snapshot(P.NONE), "future")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("floor", "now_monotonic", "now_utc", "expected"),
    [
        (RecoveryFloor(150, 1040, 999, True), 100, 1000, 150),
        (RecoveryFloor(110, 1070, 999, True), 100, 1000, 170),
        (RecoveryFloor(40, 990, 999, True), 100, 1000, 100),
        (RecoveryFloor(5000, 1010, 60, False), 100, 1000, 160),
        (RecoveryFloor(5000, 1100, 60, False), 100, 1000, 200),
        (RecoveryFloor(5000, 990, 0, False), 100, 1000, 100),
        (RecoveryFloor(0, 0, 0, True), 0, 0, 0),
    ],
)
def test_recovery_floor_uses_explicit_clock_pair(
    floor: RecoveryFloor, now_monotonic: float, now_utc: float, expected: float
) -> None:
    assert recovery_floor_release_at(floor, now_monotonic=now_monotonic, now_utc=now_utc) == expected


def test_recovery_floor_is_immutable() -> None:
    floor = RecoveryFloor(100, 1000, 10, True)
    with pytest.raises(FrozenInstanceError):
        floor.same_boot = False  # type: ignore[misc]
