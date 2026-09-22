from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from telethon.tl import types  # type: ignore[import-untyped]

from mcp_telegram.drafts.contracts import (
    DraftApplyResult,
    DraftObservation,
    DraftScope,
    SnapshotCoverage,
)
from mcp_telegram.drafts.owner import DraftMessageOwner, RuntimeObserver
from mcp_telegram.drafts.ports import DraftSnapshotGateway
from mcp_telegram.event_handlers import UpdateProcessingBarrier
from mcp_telegram.runtime_observations import encode_payload, record_runtime_observation
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.telegram_demand import RpcAttemptBudget
from mcp_telegram.telegram_rpc_consumers import DemandKind


class _Repository:
    def __init__(self) -> None:
        self.account_id: int | None = None
        self.realtime: list[DraftObservation] = []
        self.snapshot_calls: list[tuple[Sequence[DraftObservation], SnapshotCoverage, Mapping[DraftScope, int]]] = []
        self.reasons: list[str] = []
        self.rearmed: list[tuple[str, float]] = []
        self.due_at: float | None = None
        self.baselines: dict[DraftScope, int] = {}
        self.baselines_error: Exception | None = None
        self.realtime_error: Exception | None = None
        self.realtime_result = DraftApplyResult(True, publication_changed=True)
        self.before_realtime: Callable[[], None] | None = None
        self.snapshot_error: Exception | None = None

    def bind_account(self, account_id: int) -> None:
        self.account_id = account_id

    def apply_realtime(self, observation: DraftObservation) -> DraftApplyResult:
        callback = self.before_realtime
        if callback is not None:
            callback()
        if self.realtime_error is not None:
            raise self.realtime_error
        self.realtime.append(observation)
        return self.realtime_result

    def snapshot_baselines(self, account_id: int) -> Mapping[DraftScope, int]:
        assert account_id == self.account_id
        if self.baselines_error is not None:
            raise self.baselines_error
        return dict(self.baselines)

    def apply_snapshot(
        self,
        observations: Sequence[DraftObservation],
        coverage: SnapshotCoverage,
        baselines: Mapping[DraftScope, int],
        *,
        claim_token: int,
    ) -> DraftApplyResult:
        assert claim_token >= 0
        if self.snapshot_error is not None:
            raise self.snapshot_error
        self.snapshot_calls.append((observations, coverage, baselines))
        return DraftApplyResult(True)

    def mark_recovery_needed(self, *, reason: str, observed_at: datetime) -> None:
        assert observed_at.tzinfo is not None
        self.reasons.append(reason)
        self.due_at = 0.0

    def recovery_due_at(self) -> float | None:
        return self.due_at

    def claim_recovery(self, *, now: float) -> int | None:
        if self.due_at is None or self.due_at > now:
            return None
        self.due_at = None
        return int(now)

    def rearm_recovery(self, *, reason: str, now: float, claim_token: int) -> bool:
        assert claim_token >= 0
        self.rearmed.append((reason, now))
        self.due_at = now + 1
        return True


class _Gateway(DraftSnapshotGateway):
    def __init__(self, coverage: SnapshotCoverage, observations: tuple[DraftObservation, ...]) -> None:
        self.coverage = coverage
        self.observations = observations
        self.calls = 0
        self.failure: BaseException | None = None

    async def fetch_all_drafts(self) -> tuple[SnapshotCoverage, tuple[DraftObservation, ...]]:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.coverage, self.observations


class _EventClient:
    def __init__(self) -> None:
        self.event_handlers: list[tuple[object, object]] = []

    def add_event_handler(self, callback: object, event: object) -> None:
        self.event_handlers.append((callback, event))

    def remove_event_handler(self, callback: object) -> None:
        self.event_handlers = [registered for registered in self.event_handlers if registered[0] is not callback]


class _DemandSink:
    def __init__(self) -> None:
        self.offered: list[DemandKind] = []

    def offer(self, kind: DemandKind) -> bool:
        self.offered.append(kind)
        return True


class _Observer:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def __call__(  # noqa: PLR0913 - mirrors the bounded runtime-observation contract
        self,
        kind: str,
        outcome: str,
        reason_code: str | None,
        *,
        duration_ms: float | None = None,
        payload: Mapping[str, object] | None = None,
        observed_at_ms: int | None = None,
    ) -> None:
        self.events.append(
            {
                "kind": kind,
                "outcome": outcome,
                "reason": reason_code,
                "duration_ms": duration_ms,
                "payload": payload,
                "observed_at_ms": observed_at_ms,
            }
        )


def _owner(
    repository: _Repository,
    gateway: _Gateway,
    *,
    observe: RuntimeObserver | None = None,
    monotonic_clock: Callable[[], float] = time.monotonic,
    utc_now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
) -> tuple[DraftMessageOwner, _EventClient]:
    client = _EventClient()
    barrier = UpdateProcessingBarrier(closed=True)
    owner = DraftMessageOwner(
        client,
        repository,
        asyncio.Event(),
        barrier,
        observe=observe,
        snapshot_gateway_factory=lambda _client, _account: gateway,
        monotonic_clock=monotonic_clock,
        utc_now_ms=utc_now_ms,
    )
    owner.bind_account(42)
    barrier.open()
    return owner, client


@pytest.mark.asyncio
async def test_raw_callback_registers_before_connect_and_never_enriches_peer() -> None:
    repository = _Repository()
    gateway = _Gateway(SnapshotCoverage(42, True, 0), ())
    owner, client = _owner(repository, gateway)

    owner.register()
    update = types.UpdateDraftMessage(types.PeerUser(91), types.DraftMessage("draft", datetime(2026, 1, 1, tzinfo=UTC)))
    await owner.on_raw_draft_update(update)

    assert len(client.event_handlers) == 1
    assert len(repository.realtime) == 1
    assert repository.realtime[0].scope.dialog_id == 91


@pytest.mark.asyncio
async def test_realtime_publication_telemetry_captures_receipt_before_commit_and_is_content_free(
    tmp_path: Path,
) -> None:
    repository = _Repository()
    gateway = _Gateway(SnapshotCoverage(42, True, 0), ())
    observer = _Observer()
    monotonic_calls: list[float] = []
    monotonic_values = iter((100.0, 100.25))
    utc_values = iter((1_700_000_000.0, 1_700_000_000.25))

    def monotonic() -> float:
        value = next(monotonic_values)
        monotonic_calls.append(value)
        return value

    def assert_receipt_was_captured() -> None:
        assert monotonic_calls == [100.0]

    repository.before_realtime = assert_receipt_was_captured
    owner, _client = _owner(
        repository,
        gateway,
        observe=observer,
        monotonic_clock=monotonic,
        utc_now_ms=lambda: int(next(utc_values) * 1000),
    )

    await owner.on_raw_draft_update(
        types.UpdateDraftMessage(
            types.PeerUser(91),
            types.DraftMessage("private draft https://secret.invalid", datetime(2026, 1, 1, tzinfo=UTC)),
        )
    )

    assert len(observer.events) == 1
    event = observer.events[0]
    assert event == {
        "kind": "draft.observed",
        "outcome": "applied",
        "reason": "present",
        "duration_ms": 250.0,
        "payload": {
            "barrier_gated": False,
            "commit_utc_ms": 1_700_000_000_250,
            "latency_basis": "callback_to_commit_monotonic",
            "publication_changed": True,
            "receipt_utc_ms": 1_700_000_000_000,
        },
        "observed_at_ms": 1_700_000_000_250,
    }
    payload_json = json.dumps(event["payload"], sort_keys=True)
    assert len(encode_payload(cast(Mapping[str, object], event["payload"])).encode()) <= 1024
    assert "private draft" not in payload_json
    assert "secret.invalid" not in payload_json

    database = tmp_path / "sync.db"
    ensure_sync_schema(database)
    conn = sqlite3.connect(database)
    try:
        record_runtime_observation(
            conn,
            kind=cast(str, event["kind"]),
            outcome=cast(str, event["outcome"]),
            reason_code=cast(str, event["reason"]),
            duration_ms=cast(float, event["duration_ms"]),
            payload=cast(Mapping[str, object], event["payload"]),
            observed_at_ms=cast(int, event["observed_at_ms"]),
        )
        row: tuple[int, float, str] = cast(
            tuple[int, float, str],
            conn.execute(
                "SELECT observed_at_ms,duration_ms,payload_json FROM runtime_observations WHERE kind='draft.observed'"
            ).fetchone(),
        )
    finally:
        conn.close()
    assert row == (
        1_700_000_000_250,
        250.0,
        (
            '{"barrier_gated":false,"commit_utc_ms":1700000000250,"latency_basis":"callback_to_commit_monotonic",'
            '"publication_changed":true,"receipt_utc_ms":1700000000000}'
        ),
    )


@pytest.mark.asyncio
async def test_realtime_duplicate_confirmation_is_marked_excludable_from_publication_latency() -> None:
    repository = _Repository()
    repository.realtime_result = DraftApplyResult(accepted=True, revision=17, publication_changed=False)
    gateway = _Gateway(SnapshotCoverage(42, True, 0), ())
    observer = _Observer()
    owner, _client = _owner(repository, gateway, observe=observer)

    await owner.on_raw_draft_update(
        types.UpdateDraftMessage(types.PeerUser(91), types.DraftMessage("draft", datetime(2026, 1, 1, tzinfo=UTC)))
    )

    event = observer.events[-1]
    assert event["outcome"] == "applied"
    assert event["duration_ms"] is not None
    assert cast(Mapping[str, object], event["payload"])["publication_changed"] is False


@pytest.mark.asyncio
async def test_realtime_telemetry_marks_receipt_held_by_startup_barrier() -> None:
    repository = _Repository()
    gateway = _Gateway(SnapshotCoverage(42, True, 0), ())
    observer = _Observer()
    client = _EventClient()
    barrier = UpdateProcessingBarrier(closed=True)
    owner = DraftMessageOwner(
        client,
        repository,
        asyncio.Event(),
        barrier,
        observe=observer,
        snapshot_gateway_factory=lambda _client, _account: gateway,
    )
    owner.bind_account(42)

    pending = asyncio.create_task(
        owner.on_raw_draft_update(
            types.UpdateDraftMessage(types.PeerUser(91), types.DraftMessage("draft", datetime(2026, 1, 1, tzinfo=UTC)))
        )
    )
    await asyncio.sleep(0)
    assert repository.realtime == []
    barrier.open()
    await pending

    assert cast(Mapping[str, object], observer.events[-1]["payload"])["barrier_gated"] is True


@pytest.mark.asyncio
async def test_realtime_persistence_failure_does_not_claim_local_publication() -> None:
    repository = _Repository()
    repository.realtime_error = RuntimeError("database failed")
    gateway = _Gateway(SnapshotCoverage(42, True, 0), ())
    observer = _Observer()
    owner, _client = _owner(repository, gateway, observe=observer)

    await owner.on_raw_draft_update(
        types.UpdateDraftMessage(types.PeerUser(91), types.DraftMessage("draft", datetime(2026, 1, 1, tzinfo=UTC)))
    )

    assert observer.events[-1] == {
        "kind": "draft.observed",
        "outcome": "deferred",
        "reason": "persistence_failed",
        "duration_ms": None,
        "payload": None,
        "observed_at_ms": None,
    }
    assert all(event["outcome"] != "applied" for event in observer.events)


@pytest.mark.asyncio
async def test_undated_realtime_is_coalesced_for_snapshot_instead_of_arrival_ordering() -> None:
    repository = _Repository()
    gateway = _Gateway(SnapshotCoverage(42, True, 0), ())
    owner, _client = _owner(repository, gateway)
    sink = _DemandSink()
    owner.bind_demand_sink(sink)

    await owner.on_raw_draft_update(types.UpdateDraftMessage(types.PeerUser(91), types.DraftMessage("draft", None)))

    assert repository.realtime[0].ambiguity is True
    assert repository.reasons == ["ambiguous_realtime"]
    assert sink.offered == [DemandKind.DRAFT_SNAPSHOT]


@pytest.mark.asyncio
async def test_realtime_empty_update_persists_a_tombstone_without_requesting_recovery() -> None:
    repository = _Repository()
    gateway = _Gateway(SnapshotCoverage(42, True, 0), ())
    owner, _client = _owner(repository, gateway)

    await owner.on_raw_draft_update(types.UpdateDraftMessage(types.PeerUser(91), types.DraftMessageEmpty()))

    assert repository.realtime[0].disposition.value == "tombstone"
    assert repository.reasons == []


@pytest.mark.asyncio
async def test_snapshot_captures_baseline_before_the_single_rpc_and_applies_authoritative_coverage() -> None:
    repository = _Repository()
    gateway = _Gateway(SnapshotCoverage(42, True, 0), ())
    owner, _client = _owner(repository, gateway)
    repository.due_at = 0.0
    baseline_scope = DraftScope(42, 91)
    repository.baselines = {baseline_scope: 7}

    await owner.run_slice(RpcAttemptBudget(1))

    assert gateway.calls == 1
    assert len(repository.snapshot_calls) == 1
    assert repository.snapshot_calls[0][2] == {baseline_scope: 7}


@pytest.mark.asyncio
async def test_snapshot_recovery_does_not_emit_realtime_latency() -> None:
    repository = _Repository()
    gateway = _Gateway(SnapshotCoverage(42, True, 0), ())
    observer = _Observer()
    owner, _client = _owner(repository, gateway, observe=observer)
    repository.due_at = 0.0

    await owner.run_slice(RpcAttemptBudget(1))

    assert observer.events == [
        {
            "kind": "draft.recovery",
            "outcome": "applied",
            "reason": None,
            "duration_ms": None,
            "payload": None,
            "observed_at_ms": None,
        }
    ]


@pytest.mark.asyncio
async def test_incomplete_snapshot_never_calls_snapshot_apply_or_infers_absence() -> None:
    repository = _Repository()
    gateway = _Gateway(SnapshotCoverage(42, False, 0), ())
    owner, _client = _owner(repository, gateway)
    repository.due_at = 0.0

    await owner.run_slice(RpcAttemptBudget(1))

    assert repository.snapshot_calls == []
    assert [reason for reason, _now in repository.rearmed] == ["snapshot_coverage_incomplete"]


@pytest.mark.asyncio
async def test_claimed_recovery_is_rearmed_when_snapshot_fetch_fails() -> None:
    repository = _Repository()
    gateway = _Gateway(SnapshotCoverage(42, True, 0), ())
    gateway.failure = RuntimeError("transport failed")
    owner, _client = _owner(repository, gateway)
    repository.due_at = 0.0

    with pytest.raises(RuntimeError, match="transport failed"):
        await owner.run_slice(RpcAttemptBudget(1))

    assert [reason for reason, _now in repository.rearmed] == ["snapshot_fetch_failed"]
    assert repository.due_at is not None


@pytest.mark.asyncio
async def test_claimed_recovery_is_rearmed_when_snapshot_baselines_fail() -> None:
    repository = _Repository()
    repository.baselines_error = RuntimeError("baseline write failed")
    gateway = _Gateway(SnapshotCoverage(42, True, 0), ())
    owner, _client = _owner(repository, gateway)
    repository.due_at = 0.0

    with pytest.raises(RuntimeError, match="baseline write failed"):
        await owner.run_slice(RpcAttemptBudget(1))

    assert gateway.calls == 0
    assert [reason for reason, _now in repository.rearmed] == ["snapshot_baselines_failed"]


@pytest.mark.asyncio
async def test_claimed_recovery_is_rearmed_and_cancellation_propagates() -> None:
    repository = _Repository()
    gateway = _Gateway(SnapshotCoverage(42, True, 0), ())
    gateway.failure = asyncio.CancelledError()
    owner, _client = _owner(repository, gateway)
    repository.due_at = 0.0

    with pytest.raises(asyncio.CancelledError):
        await owner.run_slice(RpcAttemptBudget(1))

    assert [reason for reason, _now in repository.rearmed] == ["snapshot_fetch_failed"]


@pytest.mark.asyncio
async def test_claimed_recovery_is_rearmed_when_snapshot_publish_fails() -> None:
    repository = _Repository()
    repository.snapshot_error = RuntimeError("database failed")
    gateway = _Gateway(SnapshotCoverage(42, True, 0), ())
    owner, _client = _owner(repository, gateway)
    repository.due_at = 0.0

    with pytest.raises(RuntimeError, match="database failed"):
        await owner.run_slice(RpcAttemptBudget(1))

    assert [reason for reason, _now in repository.rearmed] == ["snapshot_publish_failed"]


@pytest.mark.asyncio
async def test_realtime_persistence_error_requests_durable_recovery() -> None:
    repository = _Repository()
    repository.realtime_error = RuntimeError("database failed")
    gateway = _Gateway(SnapshotCoverage(42, True, 0), ())
    owner, _client = _owner(repository, gateway)

    await owner.on_raw_draft_update(
        types.UpdateDraftMessage(types.PeerUser(91), types.DraftMessage("draft", datetime(2026, 1, 1, tzinfo=UTC)))
    )

    assert repository.reasons == ["realtime_persistence_failed"]
