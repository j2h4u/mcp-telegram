from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from mcp_telegram.telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    UnclassifiedTelegramDemandError,
    acquisition_context,
    current_demand_token,
    demand_context,
    resolve_admission_deadline,
    transferred_demand_context,
)
from mcp_telegram.telegram_rpc_consumers import DemandKind, demand_contract


def test_caller_deadline_can_only_tighten_registered_policy() -> None:
    contract = demand_contract(DemandKind.MCP_REMOTE_ACQUISITION)
    assert resolve_admission_deadline(contract, None, now=100.0) == 115.0
    assert resolve_admission_deadline(contract, 108.0, now=100.0) == 108.0
    assert resolve_admission_deadline(contract, 999.0, now=100.0) == 115.0

    with pytest.raises(ValueError, match="caller_deadline"):
        resolve_admission_deadline(contract, float("inf"), now=100.0)


def test_nested_acquisition_preserves_root_identity_and_restores_context() -> None:
    contract = demand_contract(DemandKind.SCHEDULED_REPAIR)
    with demand_context(DemandKind.SCHEDULED_REPAIR) as root:
        assert root.kind is contract.kind
        assert root.source is contract.source
        assert root.service_class is contract.service_class
        assert root.acquisition_kind is None

        with acquisition_context(AcquisitionKind.ENTITY_LOOKUP) as nested:
            assert nested.kind is root.kind
            assert nested.source is root.source
            assert nested.service_class is root.service_class
            assert nested.admission_deadline == root.admission_deadline
            assert nested.acquisition_kind is AcquisitionKind.ENTITY_LOOKUP
            assert current_demand_token() is nested

        assert current_demand_token() is root

    with pytest.raises(UnclassifiedTelegramDemandError):
        current_demand_token()


def test_root_context_and_tokens_are_immutable() -> None:
    with demand_context(DemandKind.MESSAGE_READ_FALLBACK) as token:
        with pytest.raises(FrozenInstanceError):
            token.kind = DemandKind.MCP_REMOTE_ACQUISITION  # type: ignore[misc]
        with pytest.raises(RuntimeError, match="nested root demand"):
            with demand_context(DemandKind.MCP_REMOTE_ACQUISITION):
                pass


@pytest.mark.asyncio
async def test_detached_task_requires_explicit_context_transfer() -> None:
    async def inspect() -> object:
        return current_demand_token()

    with demand_context(DemandKind.MCP_REMOTE_ACQUISITION) as root:
        inherited = asyncio.create_task(inspect())

        async def inspect_transferred() -> object:
            with transferred_demand_context(root):
                return current_demand_token()

        transferred = asyncio.create_task(inspect_transferred())

    with pytest.raises(UnclassifiedTelegramDemandError, match="inherited another task"):
        await inherited
    transferred_token = await transferred
    assert transferred_token.kind is root.kind
    assert transferred_token.source is root.source
    assert transferred_token.service_class is root.service_class
    assert transferred_token.owner_task is transferred


def test_rpc_attempt_budget_fails_before_exceeding_contract() -> None:
    budget = RpcAttemptBudget(limit=2)
    budget.debit()
    assert budget.remaining == 1
    assert budget.try_debit()
    assert budget.exhausted
    assert not budget.try_debit()
    with pytest.raises(RpcAttemptBudgetExhaustedError):
        budget.debit()
    assert budget.attempts == 2


def test_demand_status_reports_readiness_and_freshness_debt() -> None:
    status = DemandStatus(release_at=100.0, freshness_deadline=120.0)
    assert not status.is_ready(99.0)
    assert status.is_ready(100.0)
    assert status.overdue_seconds(119.0) == 0.0
    assert status.overdue_seconds(125.0) == 5.0

    with pytest.raises(FrozenInstanceError):
        status.release_at = 0.0  # type: ignore[misc]
