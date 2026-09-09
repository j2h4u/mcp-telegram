from __future__ import annotations

from collections.abc import Mapping

import pytest

from mcp_telegram.telegram_demand import DemandStatus, RpcAttemptBudget, current_demand_token
from mcp_telegram.telegram_demand_coordinator import TelegramDemandCoordinator, validate_durable_adapters
from mcp_telegram.telegram_rpc_consumers import TELEGRAM_DEMAND_CONTRACTS, DemandKind, ExecutionMode


class _Adapter:
    def __init__(self, status: DemandStatus | None = None) -> None:
        self.current_status = status
        self.status_calls: list[float] = []
        self.run_calls: list[RpcAttemptBudget] = []

    def status(self, now: float) -> DemandStatus | None:
        self.status_calls.append(now)
        return self.current_status

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        self.run_calls.append(budget)


def _adapters() -> dict[DemandKind, _Adapter]:
    return {
        kind: _Adapter()
        for kind, contract in TELEGRAM_DEMAND_CONTRACTS.items()
        if contract.execution_mode is ExecutionMode.DURABLE
    }


def _run_calls(adapters: Mapping[DemandKind, _Adapter]) -> int:
    return sum(len(adapter.run_calls) for adapter in adapters.values())


def test_startup_reconstructs_ready_and_timer_state_from_adapter_status() -> None:
    adapters = _adapters()
    adapters[DemandKind.SCHEDULED_REPAIR].current_status = DemandStatus(release_at=50.0, freshness_deadline=75.0)
    adapters[DemandKind.SCHEDULED_DISCOVERY].current_status = DemandStatus(release_at=150.0)

    coordinator = TelegramDemandCoordinator(adapters, clock=lambda: 100.0)

    assert coordinator.ready_kinds == (DemandKind.SCHEDULED_REPAIR,)
    assert coordinator.next_release_at == 150.0
    assert set(coordinator.statuses) == {DemandKind.SCHEDULED_REPAIR, DemandKind.SCHEDULED_DISCOVERY}
    assert all(adapter.status_calls == [100.0] for adapter in adapters.values())
    assert _run_calls(adapters) == 0


def test_one_thousand_offers_coalesce_to_one_ready_entry_and_one_status_refresh() -> None:
    adapters = _adapters()
    coordinator = TelegramDemandCoordinator(adapters, clock=lambda: 100.0)
    adapter = adapters[DemandKind.SCHEDULED_REPAIR]
    adapter.current_status = DemandStatus(release_at=100.0)

    accepted = [coordinator.offer(DemandKind.SCHEDULED_REPAIR) for _ in range(1_000)]

    assert accepted.count(True) == 1
    assert coordinator.ready_kinds == (DemandKind.SCHEDULED_REPAIR,)
    assert coordinator.offered_kinds == frozenset({DemandKind.SCHEDULED_REPAIR})
    assert adapter.status_calls == [100.0, 100.0]
    assert _run_calls(adapters) == 0


def test_all_shadow_scan_paths_are_authoritative_and_never_execute() -> None:
    adapters = _adapters()
    coordinator = TelegramDemandCoordinator(adapters, clock=lambda: 100.0)
    adapter = adapters[DemandKind.SCHEDULED_DISCOVERY]
    adapter.current_status = DemandStatus(release_at=200.0)

    coordinator.offer(DemandKind.SCHEDULED_DISCOVERY)
    coordinator.timer_scan(now=200.0)
    assert coordinator.ready_kinds == (DemandKind.SCHEDULED_DISCOVERY,)

    adapter.current_status = None
    coordinator.after_cycle_scan(now=201.0)
    assert coordinator.ready_kinds == ()
    assert coordinator.statuses == {}
    assert _run_calls(adapters) == 0


def test_startup_validation_requires_exact_durable_adapter_coverage() -> None:
    adapters = _adapters()
    adapters.pop(DemandKind.SCHEDULED_REPAIR)
    with pytest.raises(RuntimeError, match="coverage mismatch"):
        validate_durable_adapters(adapters)

    complete = _adapters()
    complete[DemandKind.MCP_REMOTE_ACQUISITION] = _Adapter()
    with pytest.raises(RuntimeError, match="unexpected"):
        validate_durable_adapters(complete)


@pytest.mark.asyncio
async def test_inline_run_installs_registered_root_context_and_preserves_result() -> None:
    coordinator = TelegramDemandCoordinator(_adapters(), clock=lambda: 100.0)

    async def operation() -> tuple[DemandKind, object, object]:
        token = current_demand_token()
        return token.kind, token.source, token.service_class

    result = await coordinator.run(DemandKind.MCP_REMOTE_ACQUISITION, operation)
    contract = TELEGRAM_DEMAND_CONTRACTS[DemandKind.MCP_REMOTE_ACQUISITION]
    assert result == (contract.kind, contract.source, contract.service_class)

    with pytest.raises(RuntimeError, match="not registered for inline"):
        await coordinator.run(DemandKind.SCHEDULED_REPAIR, operation)


def test_protocol_scope_accepts_only_protocol_owned_kind() -> None:
    coordinator = TelegramDemandCoordinator(_adapters(), clock=lambda: 100.0)
    with coordinator.protocol_scope(DemandKind.TELETHON_UPDATE_DIFFERENCE) as token:
        assert current_demand_token() is token

    with pytest.raises(RuntimeError, match="not registered for protocol"):
        with coordinator.protocol_scope(DemandKind.RECONNECT_DIFFERENCE):
            pass


def test_offer_rejects_non_durable_kind() -> None:
    coordinator = TelegramDemandCoordinator(_adapters(), clock=lambda: 100.0)
    with pytest.raises(RuntimeError, match="not registered for durable"):
        coordinator.offer(DemandKind.MCP_REMOTE_ACQUISITION)
