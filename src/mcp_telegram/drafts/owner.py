"""Lifecycle owner for the one account-wide Telegram draft projection."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, cast

from telethon import events  # type: ignore[import-untyped]
from telethon.tl import types  # type: ignore[import-untyped]

from mcp_telegram.demand_wiring import DemandOfferSink, offer_durable_demand
from mcp_telegram.drafts.contracts import DraftObservationSource
from mcp_telegram.drafts.ports import DraftProjectionRepository, DraftSnapshotGateway
from mcp_telegram.drafts.telethon_adapter import TelethonDraftSnapshotGateway, normalize_update_draft
from mcp_telegram.event_handlers import UpdateProcessingBarrier
from mcp_telegram.telegram_demand import DemandStatus, RpcAttemptBudget, demand_context
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import rpc_attempt_budget


class _DraftEventClient(Protocol):
    def add_event_handler(self, callback: object, event: object) -> None: ...

    def remove_event_handler(self, callback: object) -> None: ...


RuntimeObserver = Callable[[str, str, str | None], None]


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
    ) -> None:
        self._client = client
        self._event_client = cast(_DraftEventClient, client)
        self._repository = repository
        self._shutdown_event = shutdown_event
        self._update_barrier = update_barrier
        self._observe = observe
        self._snapshot_gateway_factory = snapshot_gateway_factory
        self._account_id: int | None = None
        self._snapshot_gateway: DraftSnapshotGateway | None = None
        self._demand_sink: DemandOfferSink | None = None
        self._registered = False

    def register(self) -> None:
        """Register the barrier-gated raw callback before the client connects."""
        if self._registered:
            return
        self._event_client.add_event_handler(self.on_raw_draft_update, events.Raw(types=[types.UpdateDraftMessage]))
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
        result = self._repository.apply_realtime(observation)
        if observation.ambiguity or result.ambiguous:
            self.request_recovery("ambiguous_realtime")
        self._record(
            "draft.observed",
            "applied" if result.accepted else "ignored",
            observation.disposition.value,
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
        account_id = self._account_id
        gateway = self._snapshot_gateway
        now = time.time()
        if account_id is None or gateway is None or budget.exhausted:
            return
        status = self.status(now)
        if status is None or not status.is_ready(now):
            return
        if not self._repository.claim_recovery(now=now):
            return
        # This fence is intentionally taken before the RPC.  The persistence
        # worker compares it against revisions written by later realtime rows
        # and tombstones, so an older snapshot cannot overwrite either one.
        baselines = self._repository.snapshot_baselines(account_id)
        with demand_context(self.demand_kind):
            with rpc_attempt_budget(budget):
                coverage, observations = await gateway.fetch_all_drafts()
        if not coverage.authoritative:
            self.request_recovery("snapshot_coverage_incomplete")
            self._record("draft.recovery", "deferred", "snapshot_coverage_incomplete")
            return
        result = self._repository.apply_snapshot(observations, coverage, baselines)
        if result.ambiguous:
            self.request_recovery("ambiguous_snapshot")
        self._record("draft.recovery", "applied" if result.accepted else "ignored", None)

    def _record(self, kind: str, outcome: str, reason: str | None) -> None:
        observer = self._observe
        if observer is not None:
            observer(kind, outcome, reason)


__all__ = ["DraftMessageOwner", "RuntimeObserver"]
