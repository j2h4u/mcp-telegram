from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from types import MappingProxyType

import pytest

from mcp_telegram.media_hydration import MediaFactHydrationHandler
from mcp_telegram.telegram_rpc_consumers import (
    TELEGRAM_DEMAND_CONTRACTS,
    TELEGRAM_RPC_CONSUMERS,
    AcquisitionRole,
    AcquisitionTrigger,
    DemandBound,
    DemandKind,
    DemandPolicyOwner,
    ExecutionMode,
    FanoutScope,
    TelegramFactDomain,
    TelegramRpcSource,
    demand_contract,
    telegram_rpc_consumer,
    validate_demand_contracts,
)
from mcp_telegram.telegram_rpc_scheduler import RPC_SOURCE_SERVICE_CLASS
from mcp_telegram.transcription_hydration import TranscriptionHydrationHandler


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


def test_demand_registry_has_exact_operation_and_source_coverage() -> None:
    assert set(TELEGRAM_DEMAND_CONTRACTS) == set(DemandKind)
    expected_by_source = {
        TelegramRpcSource.MCP_INTERACTIVE: {DemandKind.MCP_REMOTE_ACQUISITION},
        TelegramRpcSource.MESSAGE_READ_FALLBACK: {DemandKind.MESSAGE_READ_FALLBACK},
        TelegramRpcSource.DIALOG_RESOLUTION: {DemandKind.ENTITY_LOOKUP, DemandKind.DIALOG_TRAVERSAL},
        TelegramRpcSource.TOPIC_RESOLUTION: {DemandKind.TOPIC_LOOKUP},
        TelegramRpcSource.ENTITY_INFO_FOREGROUND: {DemandKind.FOREGROUND_ENTITY_FACTS},
        TelegramRpcSource.ENTITY_INFO_REFRESH: {DemandKind.ENTITY_PROFILE_REFRESH},
        TelegramRpcSource.ACCOUNT_TRACE: {DemandKind.ACCOUNT_TRACE_PAGE},
        TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE: {DemandKind.TELETHON_UPDATE_DIFFERENCE},
        TelegramRpcSource.RECONNECT_DIFFERENCE: {DemandKind.RECONNECT_DIFFERENCE},
        TelegramRpcSource.REALTIME_EVENT: {DemandKind.REALTIME_EVENT_ACQUISITION},
        TelegramRpcSource.DELTA_SYNC: {DemandKind.DELTA_GAP_FILL, DemandKind.DELTA_ACCESS_PROBE},
        TelegramRpcSource.ACTIVITY_HOT_SWEEP: {DemandKind.HOT_ACTIVITY_PAGE},
        TelegramRpcSource.FACT_HYDRATION_LIVE: {DemandKind.LIVE_HYDRATION_BATCH},
        TelegramRpcSource.FULL_SYNC: {DemandKind.FULL_SYNC_DM_ENROLLMENT, DemandKind.FULL_SYNC_PAGE},
        TelegramRpcSource.DIALOG_SYNC: {
            DemandKind.DIALOG_BOOTSTRAP,
            DemandKind.DIALOG_LIGHT_RECONCILIATION,
            DemandKind.DIALOG_FULL_RECONCILIATION,
        },
        TelegramRpcSource.ACTIVITY_ARCHIVE: {DemandKind.ARCHIVE_BACKFILL, DemandKind.ARCHIVE_INCREMENTAL},
        TelegramRpcSource.ACTIVITY_COLD_BACKFILL: {DemandKind.COLD_PEER_PAGE},
        TelegramRpcSource.FACT_HYDRATION_BACKFILL: {DemandKind.BACKFILL_HYDRATION_BATCH},
        TelegramRpcSource.FOLDER_RECONCILIATION: {DemandKind.FOLDER_SNAPSHOT},
        TelegramRpcSource.TOPIC_RECONCILIATION: {DemandKind.TOPIC_SNAPSHOT},
        TelegramRpcSource.MESSAGE_FACT_REFRESH: {DemandKind.MESSAGE_FACT_REFRESH},
        TelegramRpcSource.REACTION_REFRESH: {DemandKind.REACTION_REFRESH_BATCH},
        TelegramRpcSource.READ_RECEIPT_PROBE: {DemandKind.READ_RECEIPT_BATCH},
        TelegramRpcSource.SCHEDULED_MESSAGES: {
            DemandKind.SCHEDULED_REPAIR,
            DemandKind.SCHEDULED_DISCOVERY,
        },
        TelegramRpcSource.MAINTENANCE: {DemandKind.SELF_PROFILE_MAINTENANCE},
    }
    actual_by_source = {
        source: {kind for kind, contract in TELEGRAM_DEMAND_CONTRACTS.items() if contract.source is source}
        for source in TelegramRpcSource
    }
    assert actual_by_source == expected_by_source


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

    kind = DemandKind.SCHEDULED_REPAIR
    with pytest.raises(TypeError):
        TELEGRAM_DEMAND_CONTRACTS[kind] = TELEGRAM_DEMAND_CONTRACTS[kind]  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        TELEGRAM_DEMAND_CONTRACTS[kind].source_outstanding_limit = 99  # type: ignore[misc]


def test_demand_contract_modes_and_policy_are_internally_consistent() -> None:
    for contract in TELEGRAM_DEMAND_CONTRACTS.values():
        consumer = TELEGRAM_RPC_CONSUMERS[contract.source]
        assert contract.service_class is consumer.admission.service_class
        assert contract.admission_timeout_seconds > 0
        assert contract.source_outstanding_limit > 0
        if contract.execution_mode is ExecutionMode.DURABLE:
            assert contract.max_rpc_attempts_per_slice is not None
            assert contract.max_rpc_attempts_per_slice > 0
        else:
            assert contract.max_rpc_attempts_per_slice is None
            assert contract.freshness_target is None

    assert demand_contract(DemandKind.SCHEDULED_REPAIR).freshness_target == timedelta(minutes=15)
    assert demand_contract(DemandKind.SCHEDULED_DISCOVERY).freshness_target == timedelta(hours=24)
    assert demand_contract(DemandKind.FULL_SYNC_DM_ENROLLMENT).max_rpc_attempts_per_slice == 32
    assert demand_contract(DemandKind.LIVE_HYDRATION_BATCH).max_rpc_attempts_per_slice == 2
    assert demand_contract(DemandKind.BACKFILL_HYDRATION_BATCH).max_rpc_attempts_per_slice == 2
    assert demand_contract(DemandKind.REACTION_REFRESH_BATCH).execution_mode is ExecutionMode.INLINE


def test_hydration_slice_bounds_cover_every_registered_handler_cost() -> None:
    max_handler_cost = max(
        MediaFactHydrationHandler.request_cost,
        TranscriptionHydrationHandler.request_cost,
    )
    assert max_handler_cost == 2
    live_bound = demand_contract(DemandKind.LIVE_HYDRATION_BATCH).max_rpc_attempts_per_slice
    backfill_bound = demand_contract(DemandKind.BACKFILL_HYDRATION_BATCH).max_rpc_attempts_per_slice
    assert live_bound is not None and live_bound >= max_handler_cost
    assert backfill_bound is not None and backfill_bound >= max_handler_cost


def test_demand_registry_validation_rejects_missing_kind_and_contract_key_mismatch() -> None:
    missing = dict(TELEGRAM_DEMAND_CONTRACTS)
    missing.pop(DemandKind.SCHEDULED_REPAIR)
    with pytest.raises(RuntimeError, match="cover every demand kind"):
        validate_demand_contracts(MappingProxyType(missing))

    mismatched = dict(TELEGRAM_DEMAND_CONTRACTS)
    mismatched[DemandKind.SCHEDULED_REPAIR] = replace(
        mismatched[DemandKind.SCHEDULED_REPAIR], kind=DemandKind.SCHEDULED_DISCOVERY
    )
    with pytest.raises(RuntimeError, match="key does not match"):
        validate_demand_contracts(MappingProxyType(mismatched))


def test_scheduled_consumer_declares_realtime_and_difference_overlap() -> None:
    acquisition = TELEGRAM_RPC_CONSUMERS[TelegramRpcSource.SCHEDULED_MESSAGES].acquisition
    assert acquisition.domains == frozenset({TelegramFactDomain.SCHEDULED_MESSAGES})
    assert acquisition.role is AcquisitionRole.RECONCILIATION
    assert acquisition.repairs == (
        TelegramRpcSource.REALTIME_EVENT,
        TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE,
    )
    assert TELEGRAM_RPC_CONSUMERS[TelegramRpcSource.SCHEDULED_MESSAGES].demand.bound is DemandBound.PRODUCER_BOUNDED


def test_unbounded_producer_demand_is_explicit_and_reviewable() -> None:
    unbounded = {
        source for source, spec in TELEGRAM_RPC_CONSUMERS.items() if spec.demand.bound is DemandBound.PRODUCER_UNBOUNDED
    }
    assert unbounded == {TelegramRpcSource.DIALOG_SYNC}


def test_lookup_rejects_non_enum_source() -> None:
    with pytest.raises(TypeError, match="source must be a TelegramRpcSource"):
        telegram_rpc_consumer("scheduled_messages")  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="kind must be a DemandKind"):
        demand_contract("scheduled_repair")  # type: ignore[arg-type]
