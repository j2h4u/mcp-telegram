from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from types import MappingProxyType

import pytest

from mcp_telegram.media_hydration import MediaFactHydrationHandler
from mcp_telegram.telegram_rpc_consumers import (
    DURABLE_DEMAND_ORDER,
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
    demand_freshness_seconds,
    telegram_rpc_consumer,
    validate_demand_contracts,
)
from mcp_telegram.telegram_rpc_scheduler import RPC_SOURCE_SERVICE_CLASS
from mcp_telegram.transcription_hydration import TranscriptionHydrationHandler

EXPECTED_DURABLE_DEMAND_ORDER = (
    DemandKind.SELF_PROFILE_MAINTENANCE,
    DemandKind.ENTITY_PROFILE_REFRESH,
    DemandKind.DELTA_GAP_FILL,
    DemandKind.DELTA_ACCESS_PROBE,
    DemandKind.HOT_ACTIVITY_PAGE,
    DemandKind.LIVE_HYDRATION_BATCH,
    DemandKind.FULL_SYNC_DM_ENROLLMENT,
    DemandKind.FULL_SYNC_PAGE,
    DemandKind.DIALOG_BOOTSTRAP,
    DemandKind.DIALOG_LIGHT_RECONCILIATION,
    DemandKind.DIALOG_FULL_RECONCILIATION,
    DemandKind.ARCHIVE_BACKFILL,
    DemandKind.ARCHIVE_INCREMENTAL,
    DemandKind.COLD_PEER_PAGE,
    DemandKind.BACKFILL_HYDRATION_BATCH,
    DemandKind.FOLDER_SNAPSHOT,
    DemandKind.MESSAGE_FACT_REFRESH,
    DemandKind.READ_RECEIPT_BATCH,
    DemandKind.SCHEDULED_REPAIR,
    DemandKind.SCHEDULED_DISCOVERY,
)


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


def test_startup_identity_contract_covers_both_possible_network_sends() -> None:
    contract = demand_contract(DemandKind.SELF_PROFILE_MAINTENANCE)

    assert contract.max_rpc_attempts_per_slice == 2


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
    assert demand_contract(DemandKind.ARCHIVE_INCREMENTAL).freshness_target == timedelta(hours=1)
    assert demand_contract(DemandKind.DIALOG_FULL_RECONCILIATION).freshness_target == timedelta(days=1)
    assert demand_freshness_seconds(DemandKind.ARCHIVE_INCREMENTAL) == 3_600
    assert demand_freshness_seconds(DemandKind.DIALOG_FULL_RECONCILIATION) == 86_400
    assert demand_contract(DemandKind.FULL_SYNC_DM_ENROLLMENT).max_rpc_attempts_per_slice == 32
    assert demand_contract(DemandKind.HOT_ACTIVITY_PAGE).max_rpc_attempts_per_slice == 2
    assert demand_contract(DemandKind.COLD_PEER_PAGE).max_rpc_attempts_per_slice == 2
    assert demand_contract(DemandKind.FOLDER_SNAPSHOT).max_rpc_attempts_per_slice == 2
    assert demand_contract(DemandKind.LIVE_HYDRATION_BATCH).max_rpc_attempts_per_slice == 2
    assert demand_contract(DemandKind.BACKFILL_HYDRATION_BATCH).max_rpc_attempts_per_slice == 2
    assert demand_contract(DemandKind.REACTION_REFRESH_BATCH).execution_mode is ExecutionMode.INLINE
    assert demand_contract(DemandKind.TOPIC_SNAPSHOT).execution_mode is ExecutionMode.INLINE
    assert demand_contract(DemandKind.RECONNECT_DIFFERENCE).execution_mode is ExecutionMode.INLINE


def test_durable_demand_order_is_a_literal_complete_contract() -> None:
    assert DURABLE_DEMAND_ORDER == EXPECTED_DURABLE_DEMAND_ORDER
    assert len(DURABLE_DEMAND_ORDER) == 20
    assert {
        kind for kind, contract in TELEGRAM_DEMAND_CONTRACTS.items() if contract.execution_mode is ExecutionMode.DURABLE
    } == set(EXPECTED_DURABLE_DEMAND_ORDER)


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

    durable_mode_drift = dict(TELEGRAM_DEMAND_CONTRACTS)
    durable_mode_drift[DemandKind.SCHEDULED_REPAIR] = replace(
        durable_mode_drift[DemandKind.SCHEDULED_REPAIR],
        execution_mode=ExecutionMode.INLINE,
        freshness_target=None,
        max_rpc_attempts_per_slice=None,
    )
    with pytest.raises(RuntimeError, match="explicit durable demand order"):
        validate_demand_contracts(MappingProxyType(durable_mode_drift))


def test_scheduled_consumer_declares_realtime_and_difference_overlap() -> None:
    acquisition = TELEGRAM_RPC_CONSUMERS[TelegramRpcSource.SCHEDULED_MESSAGES].acquisition
    assert acquisition.domains == frozenset({TelegramFactDomain.SCHEDULED_MESSAGES})
    assert acquisition.role is AcquisitionRole.RECONCILIATION
    assert acquisition.repairs == (
        TelegramRpcSource.REALTIME_EVENT,
        TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE,
    )
    assert TELEGRAM_RPC_CONSUMERS[TelegramRpcSource.SCHEDULED_MESSAGES].demand.bound is DemandBound.PRODUCER_BOUNDED


def test_every_durable_producer_is_bounded_or_resumable() -> None:
    for kind in EXPECTED_DURABLE_DEMAND_ORDER:
        source = demand_contract(kind).source
        demand = TELEGRAM_RPC_CONSUMERS[source].demand
        assert demand.owner is DemandPolicyOwner.PRODUCER
        assert demand.bound in {DemandBound.PRODUCER_BOUNDED, DemandBound.PRODUCER_RESUMABLE}

    assert TELEGRAM_RPC_CONSUMERS[TelegramRpcSource.DIALOG_SYNC].demand.bound is DemandBound.PRODUCER_RESUMABLE


def test_lookup_rejects_non_enum_source() -> None:
    with pytest.raises(TypeError, match="source must be a TelegramRpcSource"):
        telegram_rpc_consumer("scheduled_messages")  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="kind must be a DemandKind"):
        demand_contract("scheduled_repair")  # type: ignore[arg-type]
