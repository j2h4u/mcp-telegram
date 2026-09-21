from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import pytest
from telethon.tl import types  # type: ignore[import-untyped]

from mcp_telegram.drafts.contracts import (
    DraftApplyResult,
    DraftObservation,
    DraftScope,
    SnapshotCoverage,
)
from mcp_telegram.drafts.owner import DraftMessageOwner
from mcp_telegram.drafts.ports import DraftSnapshotGateway
from mcp_telegram.event_handlers import UpdateProcessingBarrier
from mcp_telegram.telegram_demand import RpcAttemptBudget
from mcp_telegram.telegram_rpc_consumers import DemandKind


class _Repository:
    def __init__(self) -> None:
        self.account_id: int | None = None
        self.realtime: list[DraftObservation] = []
        self.snapshot_calls: list[tuple[Sequence[DraftObservation], SnapshotCoverage, Mapping[DraftScope, int]]] = []
        self.reasons: list[str] = []
        self.due_at: float | None = None
        self.baselines: dict[DraftScope, int] = {}

    def bind_account(self, account_id: int) -> None:
        self.account_id = account_id

    def apply_realtime(self, observation: DraftObservation) -> DraftApplyResult:
        self.realtime.append(observation)
        return DraftApplyResult(True)

    def snapshot_baselines(self, account_id: int) -> Mapping[DraftScope, int]:
        assert account_id == self.account_id
        return dict(self.baselines)

    def apply_snapshot(
        self,
        observations: Sequence[DraftObservation],
        coverage: SnapshotCoverage,
        baselines: Mapping[DraftScope, int],
    ) -> DraftApplyResult:
        self.snapshot_calls.append((observations, coverage, baselines))
        return DraftApplyResult(True)

    def mark_recovery_needed(self, *, reason: str, observed_at: datetime) -> None:
        assert observed_at.tzinfo is not None
        self.reasons.append(reason)
        self.due_at = 0.0

    def recovery_due_at(self) -> float | None:
        return self.due_at

    def claim_recovery(self, *, now: float) -> bool:
        if self.due_at is None or self.due_at > now:
            return False
        self.due_at = None
        return True


class _Gateway(DraftSnapshotGateway):
    def __init__(self, coverage: SnapshotCoverage, observations: tuple[DraftObservation, ...]) -> None:
        self.coverage = coverage
        self.observations = observations
        self.calls = 0

    async def fetch_all_drafts(self) -> tuple[SnapshotCoverage, tuple[DraftObservation, ...]]:
        self.calls += 1
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


def _owner(repository: _Repository, gateway: _Gateway) -> tuple[DraftMessageOwner, _EventClient]:
    client = _EventClient()
    barrier = UpdateProcessingBarrier(closed=True)
    owner = DraftMessageOwner(
        client,
        repository,
        asyncio.Event(),
        barrier,
        snapshot_gateway_factory=lambda _client, _account: gateway,
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
async def test_incomplete_snapshot_never_calls_snapshot_apply_or_infers_absence() -> None:
    repository = _Repository()
    gateway = _Gateway(SnapshotCoverage(42, False, 0), ())
    owner, _client = _owner(repository, gateway)
    repository.due_at = 0.0

    await owner.run_slice(RpcAttemptBudget(1))

    assert repository.snapshot_calls == []
    assert repository.reasons == ["snapshot_coverage_incomplete"]
