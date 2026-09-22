"""Lifecycle owner for the one account-wide Telegram draft projection."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol, cast

from mcp_telegram.demand_wiring import DemandOfferSink, offer_durable_demand
from mcp_telegram.drafts.contracts import DraftObservation, DraftObservationSource, DraftScope, SnapshotCoverage
from mcp_telegram.drafts.ports import DraftProjectionRepository, DraftSnapshotGateway
from mcp_telegram.drafts.telethon_adapter import (
    TelethonDraftSnapshotGateway,
    draft_update_event,
    normalize_update_draft,
)
from mcp_telegram.event_handlers import UpdateProcessingBarrier
from mcp_telegram.telegram_demand import DemandStatus, RpcAttemptBudget, demand_context
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import rpc_attempt_budget


class _DraftEventClient(Protocol):
    def add_event_handler(self, callback: object, event: object) -> None: ...

    def remove_event_handler(self, callback: object) -> None: ...


class RuntimeObserver(Protocol):
    """Receive content-free lifecycle observations from draft ownership."""

    def __call__(  # noqa: PLR0913 - mirrors the bounded runtime-observation contract
        self,
        kind: str,
        outcome: str,
        reason_code: str | None,
        *,
        duration_ms: float | None = None,
        payload: Mapping[str, object] | None = None,
        observed_at_ms: int | None = None,
    ) -> None: ...


def _utc_now_ms() -> int:
    return int(time.time() * 1000)


# Canonical retrospective p95 predicate: kind="draft.observed", outcome="applied",
# duration_ms IS NOT NULL, payload.publication_changed=true, and
# payload.barrier_gated=false.  duration_ms is the only latency source; UTC
# fields only delimit the retrospective window. barrier_gated=false deliberately
# measures steady-state callback-to-commit; startup-gated updates remain
# queryable separately with barrier_gated=true.
_REALTIME_LATENCY_BASIS = "callback_to_commit_monotonic"


class DraftMessageOwner:
    """Own raw draft ingestion, durable recovery, and account binding.

    The owner intentionally has no dialog traversal or peer enrichment path:
    ``UpdateDraftMessage`` already supplies every identity used in the local
    draft scope.  It also never interprets callback arrival as a revision.
    """

    demand_kind = DemandKind.DRAFT_SNAPSHOT

    def __init__(  # noqa: PLR0913 - lifecycle dependencies are intentionally explicit
        self,
        client: object,
        repository: DraftProjectionRepository,
        shutdown_event: asyncio.Event,
        update_barrier: UpdateProcessingBarrier,
        *,
        observe: RuntimeObserver | None = None,
        snapshot_gateway_factory: Callable[[object, int], DraftSnapshotGateway] = TelethonDraftSnapshotGateway,
        monotonic_clock: Callable[[], float] = time.monotonic,
        utc_now_ms: Callable[[], int] = _utc_now_ms,
    ) -> None:
        self._client = client
        self._event_client = cast(_DraftEventClient, client)
        self._repository = repository
        self._shutdown_event = shutdown_event
        self._update_barrier = update_barrier
        self._observe = observe
        self._snapshot_gateway_factory = snapshot_gateway_factory
        self._monotonic_clock = monotonic_clock
        self._utc_now_ms = utc_now_ms
        self._account_id: int | None = None
        self._snapshot_gateway: DraftSnapshotGateway | None = None
        self._demand_sink: DemandOfferSink | None = None
        self._registered = False

    def register(self) -> None:
        """Register the barrier-gated raw callback before the client connects."""
        if self._registered:
            return
        self._event_client.add_event_handler(self.on_raw_draft_update, draft_update_event())
        self._registered = True

    def unregister(self) -> None:
        """Detach the raw callback during daemon shutdown."""
        if not self._registered:
            return
        self._event_client.remove_event_handler(self.on_raw_draft_update)
        self._registered = False

    def bind_account(self, account_id: int) -> None:
        """Fence all subsequent observations to the authenticated account."""
        if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
            raise ValueError("account_id must be positive")
        if self._account_id is not None and self._account_id != account_id:
            raise RuntimeError("draft owner cannot change account within one daemon lifetime")
        self._account_id = account_id
        self._repository.bind_account(account_id)
        self._snapshot_gateway = self._snapshot_gateway_factory(self._client, account_id)

    def bind_demand_sink(self, sink: DemandOfferSink) -> None:
        """Attach the process-wide coordinator after durable composition exists."""
        self._demand_sink = sink

    def request_recovery(self, reason: str) -> None:
        """Durably coalesce a startup, reconnect, or ambiguity recovery signal."""
        if self._account_id is None:
            return
        self._repository.mark_recovery_needed(reason=reason, observed_at=datetime.now(UTC))
        sink = self._demand_sink
        if sink is not None:
            offer_durable_demand(sink, self.demand_kind)
        self._record("draft.recovery", "requested", reason)

    async def on_raw_draft_update(self, update: object) -> None:
        """Persist one raw update after the startup account barrier opens.

        This callback deliberately performs no Telegram RPC and no peer lookup.
        """
        barrier_gated = not self._update_barrier.is_open
        receipt_monotonic = self._monotonic_clock()
        receipt_utc_ms = self._utc_now_ms()
        await self._update_barrier.wait(self._shutdown_event)
        account_id = self._account_id
        if account_id is None:
            self.request_recovery("account_unbound")
            return
        observation = normalize_update_draft(
            update,
            account_id=account_id,
            source=DraftObservationSource.REALTIME,
            observed_at=datetime.now(UTC),
        )
        if observation is None:
            self.request_recovery("normalization_incomplete")
            return
        try:
            result = self._repository.apply_realtime(observation)
        except Exception:  # noqa: BLE001 - recovery preserves the next authoritative path
            self.request_recovery("realtime_persistence_failed")
            self._record("draft.observed", "deferred", "persistence_failed")
            return
        if observation.ambiguity or result.ambiguous:
            self.request_recovery("ambiguous_realtime")
        duration_ms: float | None = None
        payload: Mapping[str, object] | None = None
        commit_utc_ms: int | None = None
        if result.accepted:
            commit_utc_ms = self._utc_now_ms()
            duration_ms = max(0.0, (self._monotonic_clock() - receipt_monotonic) * 1000)
            payload = {
                "barrier_gated": barrier_gated,
                "commit_utc_ms": commit_utc_ms,
                "latency_basis": _REALTIME_LATENCY_BASIS,
                "publication_changed": result.publication_changed,
                "receipt_utc_ms": receipt_utc_ms,
            }
        self._record(
            "draft.observed",
            "applied" if result.accepted else "ignored",
            observation.disposition.value,
            duration_ms=duration_ms,
            payload=payload,
            observed_at_ms=commit_utc_ms,
        )

    def status(self, now: float) -> DemandStatus | None:
        """Expose only a durable repository request to the coordinator."""
        if self._account_id is None:
            return None
        due_at = self._repository.recovery_due_at()
        return None if due_at is None else DemandStatus(release_at=due_at)

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        """Apply one account-wide snapshot without allowing a stale baseline to win."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        snapshot_run = self._prepare_snapshot_run(budget)
        if snapshot_run is None:
            return
        gateway, baselines, claim_token = snapshot_run
        try:
            with demand_context(self.demand_kind):
                with rpc_attempt_budget(budget):
                    coverage, observations = await gateway.fetch_all_drafts()
        except BaseException:
            self._rearm_claimed_recovery("snapshot_fetch_failed", claim_token)
            raise
        try:
            self._publish_snapshot(coverage, observations, baselines, claim_token)
        except BaseException:
            self._rearm_claimed_recovery("snapshot_publish_failed", claim_token)
            raise

    def _prepare_snapshot_run(
        self,
        budget: RpcAttemptBudget,
    ) -> tuple[DraftSnapshotGateway, Mapping[DraftScope, int], int] | None:
        account_id = self._account_id
        gateway = self._snapshot_gateway
        now = time.time()
        if account_id is None or gateway is None or budget.exhausted:
            return None
        status = self.status(now)
        if status is None or not status.is_ready(now):
            return None
        claim_token = self._repository.claim_recovery(now=now)
        if claim_token is None:
            return None
        # This fence is intentionally taken before the RPC.  The persistence
        # worker compares it against revisions written by later realtime rows
        # and tombstones, so an older snapshot cannot overwrite either one.
        try:
            baselines = self._repository.snapshot_baselines(account_id)
        except BaseException:
            self._rearm_claimed_recovery("snapshot_baselines_failed", claim_token)
            raise
        return gateway, baselines, claim_token

    def _publish_snapshot(
        self,
        coverage: SnapshotCoverage,
        observations: tuple[DraftObservation, ...],
        baselines: Mapping[DraftScope, int],
        claim_token: int,
    ) -> None:
        if not coverage.authoritative:
            self._rearm_claimed_recovery("snapshot_coverage_incomplete", claim_token)
            self._record("draft.recovery", "deferred", "snapshot_coverage_incomplete")
            return
        result = self._repository.apply_snapshot(observations, coverage, baselines, claim_token=claim_token)
        if result.ambiguous:
            self._rearm_claimed_recovery("ambiguous_snapshot", claim_token)
        self._record("draft.recovery", "applied" if result.accepted else "ignored", None)

    def _rearm_claimed_recovery(self, reason: str, claim_token: int) -> None:
        """Return a claimed recovery to durable, bounded retry cadence."""
        if not self._repository.rearm_recovery(reason=reason, now=time.time(), claim_token=claim_token):
            return
        sink = self._demand_sink
        if sink is not None:
            offer_durable_demand(sink, self.demand_kind)
        self._record("draft.recovery", "deferred", reason)

    def _record(  # noqa: PLR0913 - mirrors the bounded runtime-observation contract
        self,
        kind: str,
        outcome: str,
        reason_code: str | None,
        *,
        duration_ms: float | None = None,
        payload: Mapping[str, object] | None = None,
        observed_at_ms: int | None = None,
    ) -> None:
        observer = self._observe
        if observer is not None:
            observer(
                kind,
                outcome,
                reason_code,
                duration_ms=duration_ms,
                payload=payload,
                observed_at_ms=observed_at_ms,
            )


__all__ = ["DraftMessageOwner", "RuntimeObserver"]
