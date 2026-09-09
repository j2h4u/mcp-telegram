"""Canonical, immutable registry of application-owned Telegram RPC consumers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
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
    PRODUCER_UNBOUNDED = "producer_unbounded"


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
        demand_bound=DemandBound.PRODUCER_UNBOUNDED,
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
        demand_bound=DemandBound.PRODUCER_UNBOUNDED,
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


def _validate_registry(registry: Mapping[TelegramRpcSource, TelegramRpcConsumerSpec]) -> None:
    if set(registry) != set(TelegramRpcSource):
        raise RuntimeError("Telegram RPC consumer registry must cover every source exactly once")
    for source, spec in registry.items():
        if not spec.label.strip() or not spec.purpose.strip() or not spec.acquisition.domains:
            raise RuntimeError(f"Telegram RPC consumer {source.value} has incomplete semantics")
        if spec.demand.bound is DemandBound.PRODUCER_UNBOUNDED and spec.demand.owner is not DemandPolicyOwner.PRODUCER:
            raise RuntimeError(f"Telegram RPC consumer {source.value} has an invalid unbounded demand owner")


_validate_registry(_REGISTRY)

TELEGRAM_RPC_CONSUMERS: Mapping[TelegramRpcSource, TelegramRpcConsumerSpec] = MappingProxyType(_REGISTRY)


def telegram_rpc_consumer(source: TelegramRpcSource) -> TelegramRpcConsumerSpec:
    """Return the canonical consumer record, failing closed for unknown values."""
    if not isinstance(source, TelegramRpcSource):
        raise TypeError("source must be a TelegramRpcSource")
    return TELEGRAM_RPC_CONSUMERS[source]


__all__ = [
    "TELEGRAM_RPC_CONSUMERS",
    "AcquisitionRole",
    "AcquisitionSpec",
    "AcquisitionTrigger",
    "AdmissionSpec",
    "DemandBound",
    "DemandPolicyOwner",
    "DemandSpec",
    "FanoutScope",
    "RpcServiceClass",
    "TelegramFactDomain",
    "TelegramRpcConsumerSpec",
    "TelegramRpcSource",
    "telegram_rpc_consumer",
]
