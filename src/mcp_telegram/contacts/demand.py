"""Demand adapter for the canonical account contact catalogue."""

# pyright: reportAny=false
import sqlite3

from ..telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    DurableDemandAdapter,
    RpcAttemptBudget,
    acquisition_context,
    durable_demand_context,
)
from ..telegram_demand_progress import SliceOutcome, SliceOutcomeKind, WorkKey, WorkRevision
from ..telegram_rpc_consumers import DemandKind
from ..telegram_rpc_scheduler import rpc_attempt_budget
from .refresh import ContactCatalogueRefresher


class ContactCatalogueDemandAdapter(DurableDemandAdapter):
    demand_kind = DemandKind.CONTACT_CATALOGUE

    def __init__(self, conn: sqlite3.Connection, account_id: int, refresher: ContactCatalogueRefresher) -> None:
        self._conn, self._account_id, self._refresher = conn, account_id, refresher

    def status(self, now: float, *, excluded: frozenset[WorkRevision] = frozenset()) -> DemandStatus | None:
        row = self._conn.execute(
            "SELECT requested_revision,accepted_revision FROM contact_catalogue_state WHERE account_id=?",
            (self._account_id,),
        ).fetchone()
        if row is None or row[0] <= row[1]:
            return None
        selected = WorkRevision(WorkKey(self.demand_kind, self._account_id, "contact-catalogue"), str(row[0]))
        return None if selected in excluded else DemandStatus(release_at=now, selected=selected)

    async def run_slice(self, budget: RpcAttemptBudget) -> SliceOutcome:
        selected = budget.selected
        if selected is None:
            raise ValueError("contact catalogue needs a selected revision")
        current = self.status(0)
        if current is None or current.selected != selected:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        if budget.exhausted and not budget.can_prepare_retry:
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        with (
            durable_demand_context(self.demand_kind),
            rpc_attempt_budget(budget),
            acquisition_context(AcquisitionKind.CONTACT_CATALOGUE),
        ):
            result = await self._refresher.refresh(expected_revision=int(selected.revision))
        if not result.accepted:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        return SliceOutcome(selected, SliceOutcomeKind.PROGRESSED, f"accepted:{selected.revision}")
