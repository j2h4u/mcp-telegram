"""Canonical, immutable registry of application-owned Telegram RPC consumers."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from types import MappingProxyType


class RpcServiceClass(StrEnum):
    """Internal service classes for account-wide Telegram RPC admission."""

    INTERACTIVE = "interactive"
    LIVE_SYNC = "live_sync"
    BACKGROUND = "background"


class TelegramRpcSource(StrEnum):
    """Application-owned reasons for issuing Telegram RPCs."""

    MCP_INTERACTIVE = "mcp_interactive"
    MESSAGE_READ_FALLBACK = "message_read_fallback"
    DIALOG_RESOLUTION = "dialog_resolution"
    TOPIC_RESOLUTION = "topic_resolution"
    ENTITY_INFO_FOREGROUND = "entity_info_foreground"
    ENTITY_INFO_REFRESH = "entity_info_refresh"
    ACCOUNT_TRACE = "account_trace"
    TELETHON_UPDATE_DIFFERENCE = "telethon_update_difference"
    RECONNECT_DIFFERENCE = "reconnect_difference"
    REALTIME_EVENT = "realtime_event"
    DELTA_SYNC = "delta_sync"
    ACTIVITY_HOT_SWEEP = "activity_hot_sweep"
    FACT_HYDRATION_LIVE = "fact_hydration_live"
    FULL_SYNC = "full_sync"
    DIALOG_SYNC = "dialog_sync"
    ACTIVITY_ARCHIVE = "activity_archive"
    ACTIVITY_COLD_BACKFILL = "activity_cold_backfill"
    FACT_HYDRATION_BACKFILL = "fact_hydration_backfill"
    FOLDER_RECONCILIATION = "folder_reconciliation"
    TOPIC_RECONCILIATION = "topic_reconciliation"
    MESSAGE_FACT_REFRESH = "message_fact_refresh"
    REACTION_REFRESH = "reaction_refresh"
    READ_RECEIPT_PROBE = "read_receipt_probe"
    SCHEDULED_MESSAGES = "scheduled_messages"
    MAINTENANCE = "maintenance"


class DemandKind(StrEnum):
    """Stable root operations that can cause application Telegram traffic."""

    MCP_REMOTE_ACQUISITION = "mcp_remote_acquisition"
    MESSAGE_READ_FALLBACK = "message_read_fallback"
    ENTITY_LOOKUP = "entity_lookup"
    DIALOG_TRAVERSAL = "dialog_traversal"
    TOPIC_LOOKUP = "topic_lookup"
    FOREGROUND_ENTITY_FACTS = "foreground_entity_facts"
    ENTITY_PROFILE_REFRESH = "entity_profile_refresh"
    ACCOUNT_TRACE_PAGE = "account_trace_page"
    TELETHON_UPDATE_DIFFERENCE = "telethon_update_difference"
    RECONNECT_DIFFERENCE = "reconnect_difference"
    REALTIME_EVENT_ACQUISITION = "realtime_event_acquisition"
    DELTA_GAP_FILL = "delta_gap_fill"
    DELTA_ACCESS_PROBE = "delta_access_probe"
    HOT_ACTIVITY_PAGE = "hot_activity_page"
    LIVE_HYDRATION_BATCH = "live_hydration_batch"
    FULL_SYNC_DM_ENROLLMENT = "full_sync_dm_enrollment"
    FULL_SYNC_PAGE = "full_sync_page"
    DIALOG_BOOTSTRAP = "dialog_bootstrap"
    DIALOG_LIGHT_RECONCILIATION = "dialog_light_reconciliation"
    DIALOG_FULL_RECONCILIATION = "dialog_full_reconciliation"
    ARCHIVE_BACKFILL = "archive_backfill"
    ARCHIVE_INCREMENTAL = "archive_incremental"
    COLD_PEER_PAGE = "cold_peer_page"
    BACKFILL_HYDRATION_BATCH = "backfill_hydration_batch"
    FOLDER_SNAPSHOT = "folder_snapshot"
    TOPIC_SNAPSHOT = "topic_snapshot"
    MESSAGE_FACT_REFRESH = "message_fact_refresh"
    REACTION_REFRESH_BATCH = "reaction_refresh_batch"
    READ_RECEIPT_BATCH = "read_receipt_batch"
    SCHEDULED_REPAIR = "scheduled_repair"
    SCHEDULED_DISCOVERY = "scheduled_discovery"
    SELF_PROFILE_MAINTENANCE = "self_profile_maintenance"


# Scheduler order is an independent product contract.  Keep this literal: deriving
# it from ``DemandKind`` or ``_DEMAND_CONTRACTS`` would let coordinated omissions
# pass startup validation.
DURABLE_DEMAND_ORDER: tuple[DemandKind, ...] = (
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
    DemandKind.SELF_PROFILE_MAINTENANCE,
)
_EXPECTED_DURABLE_DEMAND_COUNT = 20


class ExecutionMode(StrEnum):
    """Layer that owns the lifecycle of a root demand operation."""

    INLINE = "inline"
    PROTOCOL = "protocol"
    DURABLE = "durable"


class TelegramFactDomain(StrEnum):
    """Stable fact families acquired from Telegram."""

    ACCOUNT = "account"
    DIALOGS = "dialogs"
    ENTITIES = "entities"
    FOLDERS = "folders"
    MESSAGE_HISTORY = "message_history"
    MESSAGE_FACTS = "message_facts"
    OWN_ACTIVITY = "own_activity"
    READ_STATE = "read_state"
    REACTIONS = "reactions"
    SCHEDULED_MESSAGES = "scheduled_messages"
    TOPICS = "topics"
    UPDATE_STATE = "update_state"


class AcquisitionRole(StrEnum):
    """Why the consumer acquires its facts."""

    DIRECT = "direct"
    REALTIME = "realtime"
    CATCH_UP = "catch_up"
    RECONCILIATION = "reconciliation"
    BACKFILL = "backfill"
    ENRICHMENT = "enrichment"


class AcquisitionTrigger(StrEnum):
    """What causes the consumer to offer work."""

    REQUEST = "request"
    TELEGRAM_UPDATE = "telegram_update"
    TELETHON_INTERNAL = "telethon_internal"
    CONNECTION_TRANSITION = "connection_transition"
    PERIODIC = "periodic"
    DURABLE_BACKLOG = "durable_backlog"


class FanoutScope(StrEnum):
    """Largest logical selection scope before scalar Telegram admission."""

    SINGLE = "single"
    MESSAGE = "message"
    ENTITY = "entity"
    DIALOG = "dialog"
    ACCOUNT = "account"


class DemandPolicyOwner(StrEnum):
    """Layer responsible for deciding how much work to offer."""

    CALLER = "caller"
    TELETHON = "telethon"
    PRODUCER = "producer"


class DemandBound(StrEnum):
    """Where the maximum amount of offered work is enforced."""

    REQUEST = "request"
    EVENT = "event"
    TELETHON = "telethon"
    PRODUCER_BOUNDED = "producer_bounded"
    PRODUCER_RESUMABLE = "producer_resumable"


@dataclass(frozen=True, slots=True)
class AdmissionSpec:
    """Narrow transport-facing consumer policy."""

    service_class: RpcServiceClass


@dataclass(frozen=True, slots=True)
class AcquisitionSpec:
    """Typed facts used to inspect acquisition overlap and demand ownership."""

    domains: frozenset[TelegramFactDomain]
    role: AcquisitionRole
    trigger: AcquisitionTrigger
    fanout: FanoutScope
    repairs: tuple[TelegramRpcSource, ...] = ()


@dataclass(frozen=True, slots=True)
class DemandSpec:
    """Demand-facing declaration used to find and remove unbounded work."""

    owner: DemandPolicyOwner
    bound: DemandBound


@dataclass(frozen=True, slots=True)
class TelegramRpcConsumerSpec:
    """One application-owned Telegram RPC consumer."""

    label: str
    purpose: str
    admission: AdmissionSpec
    acquisition: AcquisitionSpec
    demand: DemandSpec


@dataclass(frozen=True, slots=True)
class DemandContract:
    """Code-owned policy for one root Telegram demand operation."""

    kind: DemandKind
    source: TelegramRpcSource
    service_class: RpcServiceClass
    execution_mode: ExecutionMode
    admission_timeout_seconds: float
    freshness_target: timedelta | None
    max_rpc_attempts_per_slice: int | None
    source_outstanding_limit: int


def _consumer(  # noqa: PLR0913, PLR0917 - compact declarations keep registry entries readable
    label: str,
    purpose: str,
    service_class: RpcServiceClass,
    domains: TelegramFactDomain | tuple[TelegramFactDomain, ...],
    role: AcquisitionRole,
    trigger: AcquisitionTrigger,
    fanout: FanoutScope,
    demand_owner: DemandPolicyOwner,
    *,
    demand_bound: DemandBound | None = None,
    repairs: tuple[TelegramRpcSource, ...] = (),
) -> TelegramRpcConsumerSpec:
    domain_items = (domains,) if isinstance(domains, TelegramFactDomain) else domains
    return TelegramRpcConsumerSpec(
        label=label,
        purpose=purpose,
        admission=AdmissionSpec(service_class),
        acquisition=AcquisitionSpec(frozenset(domain_items), role, trigger, fanout, repairs),
        demand=DemandSpec(demand_owner, demand_bound or _default_demand_bound(demand_owner, trigger)),
    )


def _default_demand_bound(owner: DemandPolicyOwner, trigger: AcquisitionTrigger) -> DemandBound:
    if owner is DemandPolicyOwner.CALLER:
        return DemandBound.REQUEST
    if owner is DemandPolicyOwner.TELETHON:
        return DemandBound.TELETHON
    if trigger is AcquisitionTrigger.TELEGRAM_UPDATE:
        return DemandBound.EVENT
    raise ValueError("producer-owned Telegram RPC demand must declare its bound")


_I = RpcServiceClass.INTERACTIVE
_L = RpcServiceClass.LIVE_SYNC
_B = RpcServiceClass.BACKGROUND
_D = AcquisitionRole.DIRECT
_R = AcquisitionRole.RECONCILIATION
_P = AcquisitionTrigger.PERIODIC
_PRODUCER = DemandPolicyOwner.PRODUCER

_REGISTRY: dict[TelegramRpcSource, TelegramRpcConsumerSpec] = {
    TelegramRpcSource.MCP_INTERACTIVE: _consumer(
        "MCP interactive",
        "Serve an explicit MCP request requiring Telegram facts",
        _I,
        (TelegramFactDomain.DIALOGS, TelegramFactDomain.MESSAGE_HISTORY),
        _D,
        AcquisitionTrigger.REQUEST,
        FanoutScope.SINGLE,
        DemandPolicyOwner.CALLER,
    ),
    TelegramRpcSource.MESSAGE_READ_FALLBACK: _consumer(
        "Message read fallback",
        "Fetch one message absent from the local mirror",
        _I,
        TelegramFactDomain.MESSAGE_HISTORY,
        _D,
        AcquisitionTrigger.REQUEST,
        FanoutScope.MESSAGE,
        DemandPolicyOwner.CALLER,
    ),
    TelegramRpcSource.DIALOG_RESOLUTION: _consumer(
        "Dialog resolution",
        "Resolve an explicitly requested Telegram dialog",
        _I,
        (TelegramFactDomain.DIALOGS, TelegramFactDomain.ENTITIES),
        _D,
        AcquisitionTrigger.REQUEST,
        FanoutScope.DIALOG,
        DemandPolicyOwner.CALLER,
    ),
    TelegramRpcSource.TOPIC_RESOLUTION: _consumer(
        "Topic resolution",
        "Resolve an explicitly requested Telegram topic",
        _I,
        TelegramFactDomain.TOPICS,
        _D,
        AcquisitionTrigger.REQUEST,
        FanoutScope.DIALOG,
        DemandPolicyOwner.CALLER,
    ),
    TelegramRpcSource.ENTITY_INFO_FOREGROUND: _consumer(
        "Entity info foreground",
        "Fetch entity facts requested by an MCP caller",
        _I,
        TelegramFactDomain.ENTITIES,
        _D,
        AcquisitionTrigger.REQUEST,
        FanoutScope.ENTITY,
        DemandPolicyOwner.CALLER,
    ),
    TelegramRpcSource.ENTITY_INFO_REFRESH: _consumer(
        "Entity info refresh",
        "Refresh queued entity profile facts",
        _B,
        TelegramFactDomain.ENTITIES,
        AcquisitionRole.ENRICHMENT,
        AcquisitionTrigger.DURABLE_BACKLOG,
        FanoutScope.ENTITY,
        _PRODUCER,
        demand_bound=DemandBound.PRODUCER_BOUNDED,
    ),
    TelegramRpcSource.ACCOUNT_TRACE: _consumer(
        "Account trace",
        "Collect authored-message evidence for an explicit trace",
        _I,
        (TelegramFactDomain.MESSAGE_HISTORY, TelegramFactDomain.OWN_ACTIVITY),
        _D,
        AcquisitionTrigger.REQUEST,
        FanoutScope.ACCOUNT,
        DemandPolicyOwner.CALLER,
    ),
    TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE: _consumer(
        "Telethon update difference",
        "Let Telethon recover gaps in the account update stream",
        _L,
        TelegramFactDomain.UPDATE_STATE,
        AcquisitionRole.CATCH_UP,
        AcquisitionTrigger.TELETHON_INTERNAL,
        FanoutScope.ACCOUNT,
        DemandPolicyOwner.TELETHON,
        repairs=(TelegramRpcSource.REALTIME_EVENT,),
    ),
    TelegramRpcSource.RECONNECT_DIFFERENCE: _consumer(
        "Reconnect difference",
        "Request missed updates after a reconnect transition",
        _L,
        TelegramFactDomain.UPDATE_STATE,
        AcquisitionRole.CATCH_UP,
        AcquisitionTrigger.CONNECTION_TRANSITION,
        FanoutScope.ACCOUNT,
        _PRODUCER,
        demand_bound=DemandBound.PRODUCER_BOUNDED,
        repairs=(TelegramRpcSource.REALTIME_EVENT,),
    ),
    TelegramRpcSource.REALTIME_EVENT: _consumer(
        "Realtime event",
        "Resolve facts needed while applying Telegram updates",
        _L,
        (
            TelegramFactDomain.DIALOGS,
            TelegramFactDomain.MESSAGE_HISTORY,
            TelegramFactDomain.MESSAGE_FACTS,
            TelegramFactDomain.READ_STATE,
            TelegramFactDomain.REACTIONS,
            TelegramFactDomain.SCHEDULED_MESSAGES,
            TelegramFactDomain.TOPICS,
        ),
        AcquisitionRole.REALTIME,
        AcquisitionTrigger.TELEGRAM_UPDATE,
        FanoutScope.SINGLE,
        _PRODUCER,
    ),
    TelegramRpcSource.DELTA_SYNC: _consumer(
        "Delta sync",
        "Repair forward message-history gaps and lost access",
        _L,
        (TelegramFactDomain.MESSAGE_HISTORY, TelegramFactDomain.DIALOGS),
        AcquisitionRole.CATCH_UP,
        _P,
        FanoutScope.ACCOUNT,
        _PRODUCER,
        demand_bound=DemandBound.PRODUCER_BOUNDED,
        repairs=(TelegramRpcSource.REALTIME_EVENT,),
    ),
    TelegramRpcSource.ACTIVITY_HOT_SWEEP: _consumer(
        "Activity hot sweep",
        "Refresh recent authored activity for active peers",
        _L,
        TelegramFactDomain.OWN_ACTIVITY,
        _R,
        _P,
        FanoutScope.ACCOUNT,
        _PRODUCER,
        demand_bound=DemandBound.PRODUCER_BOUNDED,
        repairs=(TelegramRpcSource.ACTIVITY_ARCHIVE,),
    ),
    TelegramRpcSource.FACT_HYDRATION_LIVE: _consumer(
        "Live fact hydration",
        "Fill high-priority missing message facts",
        _L,
        TelegramFactDomain.MESSAGE_FACTS,
        AcquisitionRole.ENRICHMENT,
        AcquisitionTrigger.DURABLE_BACKLOG,
        FanoutScope.MESSAGE,
        _PRODUCER,
        demand_bound=DemandBound.PRODUCER_BOUNDED,
    ),
    TelegramRpcSource.FULL_SYNC: _consumer(
        "Full sync",
        "Backfill enrolled dialog message history",
        _B,
        TelegramFactDomain.MESSAGE_HISTORY,
        AcquisitionRole.BACKFILL,
        AcquisitionTrigger.DURABLE_BACKLOG,
        FanoutScope.DIALOG,
        _PRODUCER,
        demand_bound=DemandBound.PRODUCER_RESUMABLE,
    ),
    TelegramRpcSource.DIALOG_SYNC: _consumer(
        "Dialog sync",
        "Refresh dialog inventory and ownership facts",
        _B,
        (TelegramFactDomain.DIALOGS, TelegramFactDomain.ENTITIES),
        _R,
        _P,
        FanoutScope.ACCOUNT,
        _PRODUCER,
        demand_bound=DemandBound.PRODUCER_RESUMABLE,
    ),
    TelegramRpcSource.ACTIVITY_ARCHIVE: _consumer(
        "Activity archive",
        "Advance the global authored-message archive",
        _B,
        TelegramFactDomain.OWN_ACTIVITY,
        AcquisitionRole.BACKFILL,
        AcquisitionTrigger.DURABLE_BACKLOG,
        FanoutScope.ACCOUNT,
        _PRODUCER,
        demand_bound=DemandBound.PRODUCER_RESUMABLE,
    ),
    TelegramRpcSource.ACTIVITY_COLD_BACKFILL: _consumer(
        "Activity cold backfill",
        "Backfill authored activity per peer",
        _B,
        TelegramFactDomain.OWN_ACTIVITY,
        AcquisitionRole.BACKFILL,
        AcquisitionTrigger.DURABLE_BACKLOG,
        FanoutScope.DIALOG,
        _PRODUCER,
        demand_bound=DemandBound.PRODUCER_BOUNDED,
        repairs=(TelegramRpcSource.ACTIVITY_ARCHIVE,),
    ),
    TelegramRpcSource.FACT_HYDRATION_BACKFILL: _consumer(
        "Fact hydration backfill",
        "Fill low-priority historical message facts",
        _B,
        TelegramFactDomain.MESSAGE_FACTS,
        AcquisitionRole.ENRICHMENT,
        AcquisitionTrigger.DURABLE_BACKLOG,
        FanoutScope.MESSAGE,
        _PRODUCER,
        demand_bound=DemandBound.PRODUCER_BOUNDED,
    ),
    TelegramRpcSource.FOLDER_RECONCILIATION: _consumer(
        "Folder reconciliation",
        "Refresh Telegram folder definitions and membership",
        _B,
        (TelegramFactDomain.FOLDERS, TelegramFactDomain.DIALOGS),
        _R,
        _P,
        FanoutScope.ACCOUNT,
        _PRODUCER,
        demand_bound=DemandBound.PRODUCER_BOUNDED,
    ),
    TelegramRpcSource.TOPIC_RECONCILIATION: _consumer(
        "Topic reconciliation",
        "Refresh topic metadata for a dialog",
        _B,
        TelegramFactDomain.TOPICS,
        _R,
        _P,
        FanoutScope.DIALOG,
        _PRODUCER,
        demand_bound=DemandBound.PRODUCER_BOUNDED,
        repairs=(TelegramRpcSource.REALTIME_EVENT,),
    ),
    TelegramRpcSource.MESSAGE_FACT_REFRESH: _consumer(
        "Message fact refresh",
        "Refresh optional facts for selected messages",
        _B,
        TelegramFactDomain.MESSAGE_FACTS,
        AcquisitionRole.ENRICHMENT,
        AcquisitionTrigger.DURABLE_BACKLOG,
        FanoutScope.MESSAGE,
        _PRODUCER,
        demand_bound=DemandBound.PRODUCER_BOUNDED,
    ),
    TelegramRpcSource.REACTION_REFRESH: _consumer(
        "Reaction refresh",
        "Refresh detailed reactions for one message",
        _B,
        TelegramFactDomain.REACTIONS,
        AcquisitionRole.ENRICHMENT,
        AcquisitionTrigger.DURABLE_BACKLOG,
        FanoutScope.MESSAGE,
        _PRODUCER,
        demand_bound=DemandBound.PRODUCER_BOUNDED,
        repairs=(TelegramRpcSource.REALTIME_EVENT,),
    ),
    TelegramRpcSource.READ_RECEIPT_PROBE: _consumer(
        "Read receipt probe",
        "Reconcile read positions not established by updates",
        _B,
        TelegramFactDomain.READ_STATE,
        _R,
        _P,
        FanoutScope.ACCOUNT,
        _PRODUCER,
        demand_bound=DemandBound.PRODUCER_BOUNDED,
        repairs=(TelegramRpcSource.REALTIME_EVENT,),
    ),
    TelegramRpcSource.SCHEDULED_MESSAGES: _consumer(
        "Scheduled messages",
        "Reconcile server-side scheduled-message queues",
        _B,
        TelegramFactDomain.SCHEDULED_MESSAGES,
        _R,
        _P,
        FanoutScope.ACCOUNT,
        _PRODUCER,
        demand_bound=DemandBound.PRODUCER_BOUNDED,
        repairs=(TelegramRpcSource.REALTIME_EVENT, TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE),
    ),
    TelegramRpcSource.MAINTENANCE: _consumer(
        "Maintenance",
        "Refresh account-owned maintenance facts",
        _B,
        (TelegramFactDomain.ACCOUNT, TelegramFactDomain.ENTITIES),
        _R,
        _P,
        FanoutScope.ACCOUNT,
        _PRODUCER,
        demand_bound=DemandBound.PRODUCER_BOUNDED,
    ),
}


_ADMISSION_TIMEOUT_SECONDS = {
    RpcServiceClass.INTERACTIVE: 15.0,
    RpcServiceClass.LIVE_SYNC: 60.0,
    RpcServiceClass.BACKGROUND: 900.0,
}
_SOURCE_OUTSTANDING_LIMIT = {
    RpcServiceClass.INTERACTIVE: 8,
    RpcServiceClass.LIVE_SYNC: 16,
    RpcServiceClass.BACKGROUND: 8,
}


def _contract(
    kind: DemandKind,
    source: TelegramRpcSource,
    execution_mode: ExecutionMode,
    *,
    freshness_target: timedelta | None = None,
    max_rpc_attempts_per_slice: int | None = None,
) -> DemandContract:
    service_class = _REGISTRY[source].admission.service_class
    return DemandContract(
        kind=kind,
        source=source,
        service_class=service_class,
        execution_mode=execution_mode,
        admission_timeout_seconds=_ADMISSION_TIMEOUT_SECONDS[service_class],
        freshness_target=freshness_target,
        max_rpc_attempts_per_slice=max_rpc_attempts_per_slice,
        source_outstanding_limit=_SOURCE_OUTSTANDING_LIMIT[service_class],
    )


_INLINE = ExecutionMode.INLINE
_PROTOCOL = ExecutionMode.PROTOCOL
_DURABLE = ExecutionMode.DURABLE

_DEMAND_CONTRACTS: dict[DemandKind, DemandContract] = {
    DemandKind.MCP_REMOTE_ACQUISITION: _contract(
        DemandKind.MCP_REMOTE_ACQUISITION, TelegramRpcSource.MCP_INTERACTIVE, _INLINE
    ),
    DemandKind.MESSAGE_READ_FALLBACK: _contract(
        DemandKind.MESSAGE_READ_FALLBACK, TelegramRpcSource.MESSAGE_READ_FALLBACK, _INLINE
    ),
    DemandKind.ENTITY_LOOKUP: _contract(DemandKind.ENTITY_LOOKUP, TelegramRpcSource.DIALOG_RESOLUTION, _INLINE),
    DemandKind.DIALOG_TRAVERSAL: _contract(DemandKind.DIALOG_TRAVERSAL, TelegramRpcSource.DIALOG_RESOLUTION, _INLINE),
    DemandKind.TOPIC_LOOKUP: _contract(DemandKind.TOPIC_LOOKUP, TelegramRpcSource.TOPIC_RESOLUTION, _INLINE),
    DemandKind.FOREGROUND_ENTITY_FACTS: _contract(
        DemandKind.FOREGROUND_ENTITY_FACTS, TelegramRpcSource.ENTITY_INFO_FOREGROUND, _INLINE
    ),
    DemandKind.ENTITY_PROFILE_REFRESH: _contract(
        DemandKind.ENTITY_PROFILE_REFRESH,
        TelegramRpcSource.ENTITY_INFO_REFRESH,
        _DURABLE,
        max_rpc_attempts_per_slice=1,
    ),
    DemandKind.ACCOUNT_TRACE_PAGE: _contract(DemandKind.ACCOUNT_TRACE_PAGE, TelegramRpcSource.ACCOUNT_TRACE, _INLINE),
    DemandKind.TELETHON_UPDATE_DIFFERENCE: _contract(
        DemandKind.TELETHON_UPDATE_DIFFERENCE,
        TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE,
        _PROTOCOL,
    ),
    DemandKind.RECONNECT_DIFFERENCE: _contract(
        DemandKind.RECONNECT_DIFFERENCE,
        TelegramRpcSource.RECONNECT_DIFFERENCE,
        _INLINE,
    ),
    DemandKind.REALTIME_EVENT_ACQUISITION: _contract(
        DemandKind.REALTIME_EVENT_ACQUISITION, TelegramRpcSource.REALTIME_EVENT, _INLINE
    ),
    DemandKind.DELTA_GAP_FILL: _contract(
        DemandKind.DELTA_GAP_FILL,
        TelegramRpcSource.DELTA_SYNC,
        _DURABLE,
        max_rpc_attempts_per_slice=1,
    ),
    DemandKind.DELTA_ACCESS_PROBE: _contract(
        DemandKind.DELTA_ACCESS_PROBE,
        TelegramRpcSource.DELTA_SYNC,
        _DURABLE,
        max_rpc_attempts_per_slice=1,
    ),
    DemandKind.HOT_ACTIVITY_PAGE: _contract(
        DemandKind.HOT_ACTIVITY_PAGE,
        TelegramRpcSource.ACTIVITY_HOT_SWEEP,
        _DURABLE,
        max_rpc_attempts_per_slice=2,
    ),
    DemandKind.LIVE_HYDRATION_BATCH: _contract(
        DemandKind.LIVE_HYDRATION_BATCH,
        TelegramRpcSource.FACT_HYDRATION_LIVE,
        _DURABLE,
        max_rpc_attempts_per_slice=2,
    ),
    DemandKind.FULL_SYNC_DM_ENROLLMENT: _contract(
        DemandKind.FULL_SYNC_DM_ENROLLMENT,
        TelegramRpcSource.FULL_SYNC,
        _DURABLE,
        max_rpc_attempts_per_slice=32,
    ),
    DemandKind.FULL_SYNC_PAGE: _contract(
        DemandKind.FULL_SYNC_PAGE,
        TelegramRpcSource.FULL_SYNC,
        _DURABLE,
        max_rpc_attempts_per_slice=1,
    ),
    DemandKind.DIALOG_BOOTSTRAP: _contract(
        DemandKind.DIALOG_BOOTSTRAP,
        TelegramRpcSource.DIALOG_SYNC,
        _DURABLE,
        max_rpc_attempts_per_slice=32,
    ),
    DemandKind.DIALOG_LIGHT_RECONCILIATION: _contract(
        DemandKind.DIALOG_LIGHT_RECONCILIATION,
        TelegramRpcSource.DIALOG_SYNC,
        _DURABLE,
        max_rpc_attempts_per_slice=8,
    ),
    DemandKind.DIALOG_FULL_RECONCILIATION: _contract(
        DemandKind.DIALOG_FULL_RECONCILIATION,
        TelegramRpcSource.DIALOG_SYNC,
        _DURABLE,
        freshness_target=timedelta(days=1),
        max_rpc_attempts_per_slice=32,
    ),
    DemandKind.ARCHIVE_BACKFILL: _contract(
        DemandKind.ARCHIVE_BACKFILL,
        TelegramRpcSource.ACTIVITY_ARCHIVE,
        _DURABLE,
        max_rpc_attempts_per_slice=1,
    ),
    DemandKind.ARCHIVE_INCREMENTAL: _contract(
        DemandKind.ARCHIVE_INCREMENTAL,
        TelegramRpcSource.ACTIVITY_ARCHIVE,
        _DURABLE,
        freshness_target=timedelta(hours=1),
        max_rpc_attempts_per_slice=1,
    ),
    DemandKind.COLD_PEER_PAGE: _contract(
        DemandKind.COLD_PEER_PAGE,
        TelegramRpcSource.ACTIVITY_COLD_BACKFILL,
        _DURABLE,
        max_rpc_attempts_per_slice=2,
    ),
    DemandKind.BACKFILL_HYDRATION_BATCH: _contract(
        DemandKind.BACKFILL_HYDRATION_BATCH,
        TelegramRpcSource.FACT_HYDRATION_BACKFILL,
        _DURABLE,
        max_rpc_attempts_per_slice=2,
    ),
    DemandKind.FOLDER_SNAPSHOT: _contract(
        DemandKind.FOLDER_SNAPSHOT,
        TelegramRpcSource.FOLDER_RECONCILIATION,
        _DURABLE,
        max_rpc_attempts_per_slice=2,
    ),
    DemandKind.TOPIC_SNAPSHOT: _contract(
        DemandKind.TOPIC_SNAPSHOT,
        TelegramRpcSource.TOPIC_RECONCILIATION,
        _INLINE,
    ),
    DemandKind.MESSAGE_FACT_REFRESH: _contract(
        DemandKind.MESSAGE_FACT_REFRESH,
        TelegramRpcSource.MESSAGE_FACT_REFRESH,
        _DURABLE,
        max_rpc_attempts_per_slice=16,
    ),
    DemandKind.REACTION_REFRESH_BATCH: _contract(
        DemandKind.REACTION_REFRESH_BATCH,
        TelegramRpcSource.REACTION_REFRESH,
        _INLINE,
    ),
    DemandKind.READ_RECEIPT_BATCH: _contract(
        DemandKind.READ_RECEIPT_BATCH,
        TelegramRpcSource.READ_RECEIPT_PROBE,
        _DURABLE,
        max_rpc_attempts_per_slice=16,
    ),
    DemandKind.SCHEDULED_REPAIR: _contract(
        DemandKind.SCHEDULED_REPAIR,
        TelegramRpcSource.SCHEDULED_MESSAGES,
        _DURABLE,
        freshness_target=timedelta(minutes=15),
        max_rpc_attempts_per_slice=16,
    ),
    DemandKind.SCHEDULED_DISCOVERY: _contract(
        DemandKind.SCHEDULED_DISCOVERY,
        TelegramRpcSource.SCHEDULED_MESSAGES,
        _DURABLE,
        freshness_target=timedelta(hours=24),
        max_rpc_attempts_per_slice=16,
    ),
    DemandKind.SELF_PROFILE_MAINTENANCE: _contract(
        DemandKind.SELF_PROFILE_MAINTENANCE,
        TelegramRpcSource.MAINTENANCE,
        _DURABLE,
        max_rpc_attempts_per_slice=1,
    ),
}


def _validate_registry(registry: Mapping[TelegramRpcSource, TelegramRpcConsumerSpec]) -> None:
    if set(registry) != set(TelegramRpcSource):
        raise RuntimeError("Telegram RPC consumer registry must cover every source exactly once")
    for source, spec in registry.items():
        if not spec.label.strip() or not spec.purpose.strip() or not spec.acquisition.domains:
            raise RuntimeError(f"Telegram RPC consumer {source.value} has incomplete semantics")


def _validate_contract_identity(kind: DemandKind, contract: DemandContract) -> None:
    if contract.kind is not kind:
        raise RuntimeError(f"Telegram demand contract key does not match {kind.value}")
    if not isinstance(contract.source, TelegramRpcSource):
        raise RuntimeError(f"Telegram demand contract {kind.value} has an invalid source")
    if not isinstance(contract.execution_mode, ExecutionMode):
        raise RuntimeError(f"Telegram demand contract {kind.value} has an invalid execution mode")
    expected_class = _REGISTRY[contract.source].admission.service_class
    if contract.service_class is not expected_class:
        raise RuntimeError(f"Telegram demand contract {kind.value} has an inconsistent service class")


def _validate_contract_limits(
    kind: DemandKind,
    contract: DemandContract,
    source_limits: dict[TelegramRpcSource, int],
) -> None:
    if (
        isinstance(contract.admission_timeout_seconds, bool)
        or not isinstance(contract.admission_timeout_seconds, (int, float))
        or not math.isfinite(contract.admission_timeout_seconds)
        or contract.admission_timeout_seconds <= 0
    ):
        raise RuntimeError(f"Telegram demand contract {kind.value} has invalid admission policy")
    if (
        isinstance(contract.source_outstanding_limit, bool)
        or not isinstance(contract.source_outstanding_limit, int)
        or contract.source_outstanding_limit < 1
    ):
        raise RuntimeError(f"Telegram demand contract {kind.value} has invalid source outstanding policy")
    previous_limit = source_limits.setdefault(contract.source, contract.source_outstanding_limit)
    if previous_limit != contract.source_outstanding_limit:
        raise RuntimeError(f"Telegram demand source {contract.source.value} has inconsistent outstanding limits")


def _validate_contract_durable_policy(kind: DemandKind, contract: DemandContract) -> None:
    if contract.freshness_target is not None and (
        not isinstance(contract.freshness_target, timedelta) or contract.freshness_target <= timedelta(0)
    ):
        raise RuntimeError(f"Telegram demand contract {kind.value} has an invalid freshness target")
    if contract.execution_mode is ExecutionMode.DURABLE:
        if (
            isinstance(contract.max_rpc_attempts_per_slice, bool)
            or not isinstance(contract.max_rpc_attempts_per_slice, int)
            or contract.max_rpc_attempts_per_slice < 1
        ):
            raise RuntimeError(f"Durable demand contract {kind.value} must have a positive slice bound")
        producer = _REGISTRY[contract.source].demand
        if producer.owner is not DemandPolicyOwner.PRODUCER or producer.bound not in {
            DemandBound.PRODUCER_BOUNDED,
            DemandBound.PRODUCER_RESUMABLE,
        }:
            raise RuntimeError(f"Durable demand contract {kind.value} must be producer-bounded or resumable")
    elif contract.max_rpc_attempts_per_slice is not None or contract.freshness_target is not None:
        raise RuntimeError(f"Non-durable demand contract {kind.value} has durable-only policy")


def validate_demand_contracts(contracts: Mapping[DemandKind, DemandContract]) -> None:
    """Fail startup when demand policy is incomplete or internally inconsistent."""
    if set(contracts) != set(DemandKind):
        raise RuntimeError("Telegram demand contracts must cover every demand kind exactly once")
    if any(not isinstance(contract, DemandContract) for contract in contracts.values()):
        raise RuntimeError("Telegram demand contracts contain an invalid record")
    if {contract.source for contract in contracts.values()} != set(TelegramRpcSource):
        raise RuntimeError("Telegram demand contracts must cover every RPC source")
    if (
        not isinstance(DURABLE_DEMAND_ORDER, tuple)
        or len(DURABLE_DEMAND_ORDER) != _EXPECTED_DURABLE_DEMAND_COUNT
        or any(not isinstance(kind, DemandKind) for kind in DURABLE_DEMAND_ORDER)
        or len(set(DURABLE_DEMAND_ORDER)) != _EXPECTED_DURABLE_DEMAND_COUNT
    ):
        raise RuntimeError("Durable demand order must contain exactly 20 unique kinds")
    durable_kinds = {kind for kind, contract in contracts.items() if contract.execution_mode is ExecutionMode.DURABLE}
    if durable_kinds != set(DURABLE_DEMAND_ORDER):
        raise RuntimeError("Durable demand contracts must match the explicit durable demand order")
    source_limits: dict[TelegramRpcSource, int] = {}
    for kind, contract in contracts.items():
        _validate_contract_identity(kind, contract)
        _validate_contract_limits(kind, contract, source_limits)
        _validate_contract_durable_policy(kind, contract)


_validate_registry(_REGISTRY)
validate_demand_contracts(_DEMAND_CONTRACTS)

TELEGRAM_RPC_CONSUMERS: Mapping[TelegramRpcSource, TelegramRpcConsumerSpec] = MappingProxyType(_REGISTRY)
TELEGRAM_DEMAND_CONTRACTS: Mapping[DemandKind, DemandContract] = MappingProxyType(_DEMAND_CONTRACTS)


def telegram_rpc_consumer(source: TelegramRpcSource) -> TelegramRpcConsumerSpec:
    """Return the canonical consumer record, failing closed for unknown values."""
    if not isinstance(source, TelegramRpcSource):
        raise TypeError("source must be a TelegramRpcSource")
    return TELEGRAM_RPC_CONSUMERS[source]


def demand_contract(kind: DemandKind) -> DemandContract:
    """Return the code-owned contract for a registered root demand kind."""
    if not isinstance(kind, DemandKind):
        raise TypeError("kind must be a DemandKind")
    return TELEGRAM_DEMAND_CONTRACTS[kind]


def demand_freshness_seconds(kind: DemandKind) -> int:
    """Return a durable demand freshness target as whole seconds."""
    target = demand_contract(kind).freshness_target
    if target is None:
        raise ValueError(f"{kind.value} has no freshness target")
    seconds = target.total_seconds()
    if not math.isfinite(seconds) or seconds <= 0 or not seconds.is_integer():
        raise ValueError(f"{kind.value} freshness target must be a positive whole number of seconds")
    return int(seconds)


__all__ = [
    "DURABLE_DEMAND_ORDER",
    "TELEGRAM_DEMAND_CONTRACTS",
    "TELEGRAM_RPC_CONSUMERS",
    "AcquisitionRole",
    "AcquisitionSpec",
    "AcquisitionTrigger",
    "AdmissionSpec",
    "DemandBound",
    "DemandContract",
    "DemandKind",
    "DemandPolicyOwner",
    "DemandSpec",
    "ExecutionMode",
    "FanoutScope",
    "RpcServiceClass",
    "TelegramFactDomain",
    "TelegramRpcConsumerSpec",
    "TelegramRpcSource",
    "demand_contract",
    "demand_freshness_seconds",
    "telegram_rpc_consumer",
    "validate_demand_contracts",
]
