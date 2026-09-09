import asyncio

import pytest

from mcp_telegram.demand_composition import TelegramDemandShadow
from mcp_telegram.demand_shadow_wiring import offer_durable_demand, run_legacy_demand_cycle
from mcp_telegram.rpc_admission_observations import DemandEvidenceOutcome
from mcp_telegram.telegram_demand import DemandStatus, RpcAttemptBudget, current_demand_token, demand_context
from mcp_telegram.telegram_rpc_consumers import TELEGRAM_DEMAND_CONTRACTS, DemandKind, ExecutionMode


class _Adapter:
    def __init__(self, status: DemandStatus | None = None) -> None:
        self.current_status = status

    def status(self, now: float) -> DemandStatus | None:
        del now
        return self.current_status

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        del budget
        raise AssertionError("shadow must not execute adapters")


class _Observer:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def observe_demand(self, **event: object) -> None:
        self.events.append(event)


def _shadow(observer: _Observer) -> TelegramDemandShadow:
    adapters = {
        kind: _Adapter(DemandStatus(release_at=0.0) if kind is DemandKind.ENTITY_PROFILE_REFRESH else None)
        for kind, contract in TELEGRAM_DEMAND_CONTRACTS.items()
        if contract.execution_mode is ExecutionMode.DURABLE
    }
    return TelegramDemandShadow(adapters, asyncio.Event(), observer=observer, clock=lambda: 100.0)


@pytest.mark.asyncio
async def test_real_cycle_transfers_prediction_and_attempt_evidence() -> None:
    observer = _Observer()
    shadow = _shadow(observer)

    async def operation() -> str:
        token = current_demand_token()
        assert token.kind is DemandKind.ENTITY_PROFILE_REFRESH
        assert token.prediction is not None
        token.attempt_evidence.record_dispatch()
        return "done"

    assert await run_legacy_demand_cycle(shadow, DemandKind.ENTITY_PROFILE_REFRESH, operation) == "done"

    terminal = [event for event in observer.events if event["outcome"] is DemandEvidenceOutcome.COMPLETED]
    assert len(terminal) == 1
    assert terminal[0]["actual_attempts"] == 1
    assert terminal[0]["selection_match"] is True


@pytest.mark.asyncio
async def test_real_cycle_replaces_a_long_lived_legacy_root_in_a_clean_child_task() -> None:
    observer = _Observer()
    shadow = _shadow(observer)
    outer_task = asyncio.current_task()

    async def operation() -> None:
        assert asyncio.current_task() is not outer_task
        assert current_demand_token().kind is DemandKind.ENTITY_PROFILE_REFRESH

    with demand_context(DemandKind.ENTITY_PROFILE_REFRESH):
        await run_legacy_demand_cycle(shadow, DemandKind.ENTITY_PROFILE_REFRESH, operation)
        assert current_demand_token().prediction is None


def test_offer_bridge_is_post_commit_safe_when_shadow_observation_fails() -> None:
    class _FailingShadow:
        def offer(self, _kind: DemandKind) -> bool:
            raise RuntimeError("telemetry unavailable")

    offer_durable_demand(_FailingShadow(), DemandKind.FULL_SYNC_PAGE)  # type: ignore[arg-type]
