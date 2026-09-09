from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from mcp_telegram.telegram_rpc_consumers import (
    TELEGRAM_RPC_CONSUMERS,
    AcquisitionRole,
    AcquisitionTrigger,
    DemandBound,
    DemandPolicyOwner,
    FanoutScope,
    TelegramFactDomain,
    TelegramRpcSource,
    telegram_rpc_consumer,
)
from mcp_telegram.telegram_rpc_scheduler import RPC_SOURCE_SERVICE_CLASS


def test_consumer_registry_is_exhaustive_and_semantically_complete() -> None:
    assert set(TELEGRAM_RPC_CONSUMERS) == set(TelegramRpcSource)
    for source, spec in TELEGRAM_RPC_CONSUMERS.items():
        assert spec.label.strip()
        assert spec.purpose.strip()
        assert spec.acquisition.domains
        assert isinstance(spec.acquisition.role, AcquisitionRole)
        assert isinstance(spec.acquisition.trigger, AcquisitionTrigger)
        assert isinstance(spec.acquisition.fanout, FanoutScope)
        assert isinstance(spec.demand.owner, DemandPolicyOwner)
        assert isinstance(spec.demand.bound, DemandBound)
        assert all(repaired in TELEGRAM_RPC_CONSUMERS for repaired in spec.acquisition.repairs)
        assert source not in spec.acquisition.repairs


def test_scheduler_classification_is_derived_from_consumer_registry() -> None:
    assert set(RPC_SOURCE_SERVICE_CLASS) == set(TELEGRAM_RPC_CONSUMERS)
    assert {
        source: spec.admission.service_class for source, spec in TELEGRAM_RPC_CONSUMERS.items()
    } == RPC_SOURCE_SERVICE_CLASS


def test_consumer_registry_and_records_are_immutable() -> None:
    source = TelegramRpcSource.SCHEDULED_MESSAGES
    with pytest.raises(TypeError):
        TELEGRAM_RPC_CONSUMERS[source] = TELEGRAM_RPC_CONSUMERS[source]  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        TELEGRAM_RPC_CONSUMERS[source].label = "changed"  # type: ignore[misc]


def test_scheduled_consumer_declares_realtime_and_difference_overlap() -> None:
    acquisition = TELEGRAM_RPC_CONSUMERS[TelegramRpcSource.SCHEDULED_MESSAGES].acquisition
    assert acquisition.domains == frozenset({TelegramFactDomain.SCHEDULED_MESSAGES})
    assert acquisition.role is AcquisitionRole.RECONCILIATION
    assert acquisition.repairs == (
        TelegramRpcSource.REALTIME_EVENT,
        TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE,
    )
    assert TELEGRAM_RPC_CONSUMERS[TelegramRpcSource.SCHEDULED_MESSAGES].demand.bound is (DemandBound.PRODUCER_UNBOUNDED)


def test_unbounded_producer_demand_is_explicit_and_reviewable() -> None:
    unbounded = {
        source for source, spec in TELEGRAM_RPC_CONSUMERS.items() if spec.demand.bound is DemandBound.PRODUCER_UNBOUNDED
    }
    assert unbounded == {TelegramRpcSource.DIALOG_SYNC, TelegramRpcSource.SCHEDULED_MESSAGES}


def test_lookup_rejects_non_enum_source() -> None:
    with pytest.raises(TypeError, match="source must be a TelegramRpcSource"):
        telegram_rpc_consumer("scheduled_messages")  # type: ignore[arg-type]
