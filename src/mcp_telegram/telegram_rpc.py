"""Account-wide admission control for the daemon's Telethon client."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import Context, ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from functools import wraps
from itertools import chain
from pathlib import Path
from typing import Never, Protocol, cast
from weakref import WeakKeyDictionary

from aiolimiter import AsyncLimiter
from telethon import (  # type: ignore[import-untyped]
    TelegramClient,  # type: ignore[import-untyped]
    functions,
    helpers,
    types,
)
from telethon._updates import ChannelState, MessageBox, SessionState  # type: ignore[import-untyped]
from telethon.client.telegrambaseclient import (  # type: ignore[import-untyped]
    DEFAULT_DC_ID,
    DEFAULT_IPV4_IP,
    DEFAULT_IPV6_IP,
    DEFAULT_PORT,
)
from telethon.crypto import AuthKey  # type: ignore[import-untyped]
from telethon.errors import (  # type: ignore[import-untyped]
    FileMigrateError,
    FloodPremiumWaitError,
    FloodTestPhoneWaitError,
    FloodWaitError,
    InterdcCallErrorError,
    InterdcCallRichErrorError,
    RpcCallFailError,
    RPCError,
    RpcMcgetFailError,
    ServerError,
    TimedOutError,
)
from telethon.events import Raw  # type: ignore[import-untyped]
from telethon.events.common import EventBuilder  # type: ignore[import-untyped]
from telethon.sessions import Session  # type: ignore[import-untyped]
from telethon.tl.alltlobjects import LAYER, tlobjects  # type: ignore[import-untyped]
from telethon.tl.functions.updates import (  # type: ignore[import-untyped]
    GetChannelDifferenceRequest,
    GetDifferenceRequest,
)
from telethon.tl.tlobject import TLRequest  # type: ignore[import-untyped]
from telethon.utils import get_input_channel, get_peer_id, is_list_like  # type: ignore[import-untyped]

from .auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from .delivered_update import DeliveredUpdate, delivered_update_context, fail_delivered_update
from .flood import FloodWaitObservation, TelegramRpcThrottled, flood_seconds
from .image_authorization import ScopeCell, ScopeToken
from .request_timing import DaemonRequestTiming, current_timing
from .scheduled_write_fence import (
    ScheduledMutationCheck,
    ScheduledMutationIntent,
    ScheduledMutationToken,
    ScheduledWriteRejected,
    current_scheduled_mutation_check,
)
from .state import TelegramSessionLease, TelegramSessionLeasePin
from .telegram_delivery import DeliveryEnvelope, ProtocolSnapshot, load_delivery_envelope, load_protocol_snapshot
from .telegram_demand import (
    AcquisitionKind,
    DemandToken,
    MissingTelegramDemandContextError,
    RpcAttemptAccounting,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    RpcAttemptEvidence,
    RpcRetryAuthorization,
    UnclassifiedTelegramDemandError,
    acquisition_context,
    create_demand_token,
    current_demand_token,
    demand_context,
    require_execution_mode,
    transferred_demand_context,
)
from .telegram_demand_progress import WorkRevision
from .telegram_image_download import (
    PHOTO_CHUNK_BYTES,
    PHOTO_MAX_LEASE_CONTROLS,
    PHOTO_MAX_SLICE_REQUESTS,
    PhotoBudget,
    PhotoDownloadSession,
    PhotoLocator,
    PhotoObservation,
    PhotoSourceFence,
    PhotoSourceToken,
    PhotoTarget,
    PhotoWork,
    iter_photo_chunks,
    photo_locator_from_message,
)
from .telegram_rpc_consumers import (
    CONNECTION_BOOTSTRAP_MAX_RPC_ATTEMPTS,
    DemandKind,
    ExecutionMode,
    RpcEffect,
    demand_contract,
    rpc_effect,
)
from .telegram_rpc_recovery import RecoveryCause, backoff_seconds
from .telegram_rpc_scheduler import (
    AdmissionObserver,
    RpcAdmission,
    RpcAdmissionError,
    RpcAdmissionExpiredError,
    RpcAdmissionSaturatedError,
    RpcTransportReadiness,
    TelegramRpcAdmissionDeferred,
    TelegramRpcAdmissionScheduler,
    TelegramRpcMethodRecoveryPending,
    TelegramRpcSchedulerPolicy,
    TelegramRpcScope,
    TelegramRpcSource,
    UnclassifiedTelegramRpcError,
    create_scoped_rpc_task,
    current_rpc_scope,
    rpc_attempt_budget,
    rpc_scope,
)
from .telegram_transport import (
    GuardedConnectionTcpFull,
    GuardedMTProtoSender,
    ProtocolOrigin,
    TransportConnectError,
    TransportWriteError,
)
from .telegram_update_admission import GateMessageBox
from .update_delivery import UpdateDeliveryRepository

logger = logging.getLogger(__name__)
_DIAGNOSTIC_REQUESTS_MAX = 8
_TL_CONSTRUCTOR_HEX_CHARS = 8
_SCHEDULED_ONLINE_SEND_TIMESTAMP = 0x7FFFFFFE
_SCHEDULED_MINIMUM_LEAD_SECONDS = 60
_BACKEND_ERROR_CODE_MIN = 500
_BACKEND_ERROR_CODE_MAX = 599

_SAFETY_RECEIPT_VERSION = 3
_CANONICAL_METHOD_MAX_LENGTH = 256
_TL_CONSTRUCTOR_HEX_WIDTH = 8
_AUTO_RESUME_DELAYS = (3600, 7200, 14400, 28800, 57600, 86400)
_AUTO_RESUME_STABILITY_SECONDS = 3600


def raise_if_flood_wait_error(error: BaseException) -> None:
    """Re-raise vendor FloodWait outcomes before application catches."""
    if isinstance(error, FloodWaitError):
        raise error


def _annotate_flood_attempt(
    error: BaseException,
    *,
    request_method: str,
    admission_sequence: int | None,
    dispatch_at_monotonic: float | None,
    dispatch_kind: str,
) -> None:
    """Attach bounded request provenance without retaining request arguments."""
    if getattr(error, "_mcp_telegram_flood_attempt", None) is not None:
        return
    try:
        setattr(  # noqa: B010 - Telethon exception provenance is attached dynamically.
            error,
            "_mcp_telegram_flood_attempt",
            {
                "request_method": request_method,
                "admission_sequence": admission_sequence,
                "dispatch_at_monotonic": dispatch_at_monotonic,
                "dispatch_kind": dispatch_kind,
            },
        )
    except AttributeError, TypeError:
        return


TransientRpcErrors = (
    ServerError,
    RpcCallFailError,
    RpcMcgetFailError,
    InterdcCallErrorError,
    InterdcCallRichErrorError,
    TimedOutError,
)


class CircuitStatus(Protocol):
    @property
    def open(self) -> bool: ...

    def detail(self) -> str: ...


class _MainSender(Protocol):
    """The only raw Telethon sender surface retained by the gate."""

    auth_key: object

    def send(
        self,
        request: object,
        *,
        ordered: bool = False,
    ) -> Awaitable[object] | list[asyncio.Future[object]]: ...

    def send_owned(self, request: object, scope: TelegramRpcScope) -> asyncio.Future[object]: ...

    def application_owner(self, result: asyncio.Future[object]) -> object: ...

    @property
    def write_generation(self) -> int: ...

    def physical_connected(self) -> bool: ...

    async def connect(self, connection: object) -> object: ...

    async def disconnect(self) -> object: ...

    async def settle_shutdown(self) -> None: ...

    def is_connected(self) -> bool: ...

    @property
    def disconnected(self) -> asyncio.Future[object]: ...

    def _transport_connected(self) -> bool: ...

    def _keepalive_ping(self, random_id: int) -> None: ...


class _AdmissionAwareSender:
    """Admit each scalar attempt at Telethon's synchronous sender seam."""

    def __init__(self, gate: TelegramRpcGate, scope: TelegramRpcScope) -> None:
        self._gate = gate
        self._scope = scope
        self._dispatch_started = False

    def send(self, request: object, *, ordered: bool = False) -> Coroutine[object, object, object]:
        return self._send(request, ordered=ordered)

    async def _send(self, request: object, *, ordered: bool) -> object:
        del ordered
        self._gate._raise_if_transport_unavailable()
        self._gate._reject_exhausted_attempt_budget(self._scope)
        sender = self._selected_sender()
        raw_future = sender.send_owned(request, self._scope)
        wire_owner = self._gate._require_wire_owner(sender.application_owner(raw_future))
        if self._scope.source is TelegramRpcSource.TELETHON_RECONNECT_PROBE:
            self._gate._reconnect_wire_owner = wire_owner
        started_at = time.monotonic()
        timing = current_timing()
        try:
            async with asyncio.timeout_at(self._scope.deadline) as timeout:
                wire_owner.response_timer = timeout
                await asyncio.wait({raw_future})
            return raw_future.result()
        except TimeoutError:
            self._gate._revoke_wire_owner(wire_owner)
            raw_future.add_done_callback(self._consume_late_result, context=Context())
            if wire_owner.claimed == 0:
                raise RpcAdmissionExpiredError(self._scope, "Telegram RPC admission deadline elapsed") from None
            if self._gate._wire_fault is not None:
                raise TelegramRpcThrottled(
                    retry_after_seconds=None, latched=True, detail="Telegram outbound outcome is uncertain"
                ) from None
            raise
        except asyncio.CancelledError:
            self._gate._revoke_wire_owner(wire_owner)
            raw_future.add_done_callback(self._consume_late_result, context=Context())
            raise
        except BaseException:
            raw_future.add_done_callback(self._consume_late_result, context=Context())
            raise
        finally:
            wire_owner.response_timer = None
            self._dispatch_started = wire_owner.claimed > 0
            if timing is not None:
                ended_at = time.monotonic()
                admission_end = wire_owner.dispatch_at or ended_at
                timing.add_phase("rpc_admission", (admission_end - started_at) * 1000)
                if wire_owner.dispatch_at is not None:
                    timing.add_phase("rpc_execution", (ended_at - wire_owner.dispatch_at) * 1000)

    def _selected_sender(self) -> _MainSender:
        capability = _PHOTO_CAPABILITY.get()
        if capability is None or not capability.use_auxiliary:
            return self._gate._main_sender
        lease = capability.lease
        if lease is None or lease.sender is None or lease.revoked:
            raise RuntimeError("photo auxiliary sender is unavailable")
        return cast(_MainSender, lease.sender)

    @staticmethod
    def _raise_if_cancel_pending() -> None:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError

    @staticmethod
    def _consume_late_result(future: asyncio.Future[object]) -> None:
        if not future.cancelled():
            future.exception()


@dataclass(frozen=True, slots=True)
class _ConnectionCapability:
    """Explicit identity carried into Telethon's context-free connect task."""

    token: DemandToken
    deadline: float
    attempt_budget: RpcAttemptBudget | None
    owned_generation: int = 0


class _ConnectionDcMigrationRequestedError(Exception):
    """Unwind a registered RPC before Telethon mutates its transport."""

    def __init__(self, dc_id: int) -> None:
        self.dc_id = dc_id


class _TransportBoundaryState(StrEnum):
    """Whether the admitted main transport is reusable or unresolved."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    READY = "ready"
    DISCONNECTING = "disconnecting"
    FAILED = "failed"


class _MainSenderAdapter:
    """Narrow main-sender boundary for Telethon connection bootstrap."""

    def __init__(self, gate: TelegramRpcGate) -> None:
        self._gate = gate

    def send(self, request: object, ordered: bool = False) -> asyncio.Task[object]:
        if is_list_like(request):
            raise ValueError("transport batching is forbidden; use sequential scalar calls")
        capability, connection_owned = self._gate._sender_capability()
        return self._gate._run_with_connection_capability(
            request,
            ordered=ordered,
            capability=capability,
            connection_owned=connection_owned,
        )

    async def connect(self, connection: object) -> object:
        self._gate._begin_transport_connect()
        try:
            result = await self._gate._main_sender.connect(connection)
        except BaseException:
            self._gate._finish_transport_connect_failure()
            raise
        self._gate._finish_transport_connect()
        return result

    async def disconnect(self) -> object:
        return await self._gate._disconnect_main_sender()

    def is_connected(self) -> bool:
        return self._gate._transport_state is _TransportBoundaryState.READY and self._gate._main_sender.is_connected()

    @property
    def disconnected(self) -> asyncio.Future[object]:
        return self._gate._main_sender.disconnected

    @property
    def auth_key(self) -> object:
        return self._gate._main_sender.auth_key

    @auth_key.setter
    def auth_key(self, value: object) -> None:
        with self._gate._image_identity_change():
            self._gate._main_sender.auth_key = value

    def _transport_connected(self) -> bool:
        return (
            self._gate._transport_state is _TransportBoundaryState.READY
            and self._gate._main_sender._transport_connected()
        )

    def _keepalive_ping(self, random_id: int) -> None:
        if self._gate._transport_state is _TransportBoundaryState.READY:
            self._gate._main_sender._keepalive_ping(random_id)


@dataclass(frozen=True, slots=True)
class TelegramRpcBudget:
    """Process-wide logical RPC limiter settings."""

    max_calls_per_period: int
    period_seconds: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_calls_per_period, bool)
            or not isinstance(self.max_calls_per_period, int)
            or self.max_calls_per_period < 1
        ):
            raise ValueError("max_calls_per_period must be a positive integer")
        if (
            isinstance(self.period_seconds, bool)
            or not isinstance(self.period_seconds, (int, float))
            or not math.isfinite(self.period_seconds)
            or self.period_seconds <= 0
        ):
            raise ValueError("period_seconds must be a positive finite number")


@dataclass(frozen=True, slots=True)
class TelegramRpcSafetyStore:
    """Daemon-owned CAS storage; callbacks never expose session credentials."""

    load_record: Callable[[], str | None]
    save_record: Callable[[str | None, str], Awaitable[None]]
    load_legacy_until_utc: Callable[[], float | None]
    canonical_owners: Callable[[], tuple[int | None, int | None]]

    def __post_init__(self) -> None:
        if not all(
            callable(value)
            for value in (self.load_record, self.save_record, self.load_legacy_until_utc, self.canonical_owners)
        ):
            raise TypeError("Telegram RPC safety storage callbacks must be callable")


def _system_boot_id() -> str:
    value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    if not value:
        raise RuntimeError("system boot identity unavailable")
    return value


def _finite_safety_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _validate_safety_scope(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"stage", "dc_id", "key_id", "account_id"}:
        return False
    if type(value["dc_id"]) is not int or value["dc_id"] < 1:
        return False
    if value["key_id"] is not None and type(value["key_id"]) is not int:
        return False
    return _valid_scope_binding(value)


def _valid_scope_binding(value: dict[str, object]) -> bool:
    if value["stage"] == "key_pending":
        return value["key_id"] is None and value["account_id"] is None
    if value["stage"] == "transport_only":
        return value["key_id"] is not None and value["account_id"] is None
    return (
        value["stage"] == "verified"
        and value["key_id"] is not None
        and type(value["account_id"]) is int
        and value["account_id"] > 0
    )


def _validate_safety_record(value: object) -> bool:
    fields = {
        "version",
        "generation",
        "scope",
        "floor",
        "intents",
        "protection",
        "recovery",
        "transition",
    }
    if not isinstance(value, dict):
        return False
    if value.get("version") in {2, _SAFETY_RECEIPT_VERSION}:
        fields.add("methods")
    if set(value) != fields:
        return False
    if not _valid_safety_record_identity(value):
        return False
    if not _valid_safety_floor(value["floor"]):
        return False
    if value["protection"] is not None and not isinstance(value["protection"], str):
        return False
    intents = value["intents"]
    return all(
        (
            _validate_safety_scope(value["scope"]),
            isinstance(intents, dict)
            and all(
                _valid_safety_intent(key, item, version=cast(int, value["version"])) for key, item in intents.items()
            ),
            _valid_safety_recovery(value["recovery"], version=cast(int, value["version"])),
            _valid_safety_transition(value["transition"]),
            _valid_method_episodes(value.get("methods", {})),
        )
    )


def _valid_safety_floor(floor: object) -> bool:
    if not isinstance(floor, dict) or set(floor) != {"boot_id", "until_monotonic", "until_utc", "remaining_seconds"}:
        return False
    return isinstance(floor["boot_id"], str) and all(
        _finite_safety_number(floor[key]) for key in ("until_monotonic", "until_utc", "remaining_seconds")
    )


def _valid_safety_record_identity(value: dict[str, object]) -> bool:
    return all(
        (
            type(value["version"]) is int,
            value["version"] in {1, 2, _SAFETY_RECEIPT_VERSION},
            isinstance(value["generation"], str),
            bool(value["generation"]),
        )
    )


def _valid_method_episodes(value: object) -> bool:
    if not isinstance(value, dict) or len(value) > len(tlobjects):
        return False
    return all(_valid_method_key(method) and _valid_method_episode(episode) for method, episode in value.items())


def _valid_method_key(method: object) -> bool:
    if not isinstance(method, str) or len(method) > _CANONICAL_METHOD_MAX_LENGTH:
        return False
    name, separator, constructor = method.rpartition(":")
    if (
        not separator
        or len(constructor) != _TL_CONSTRUCTOR_HEX_WIDTH
        or any(c not in "0123456789abcdef" for c in constructor)
    ):
        return False
    kind = tlobjects.get(int(constructor, 16))
    return kind is not None and issubclass(kind, TLRequest) and name == f"{kind.__module__}.{kind.__name__}"


def _valid_method_episode(value: object) -> bool:
    fields = {"generation", "consecutive_actual_floods", "floor", "claim"}
    if not isinstance(value, dict) or set(value) not in (fields, fields | {"state"}):
        return False
    count = value["consecutive_actual_floods"]
    state = value.get("state", "live")
    if not isinstance(value["generation"], str) or not value["generation"] or type(count) is not int:
        return False
    return all(
        (
            _valid_method_state(state, count, value["claim"]),
            _valid_safety_floor(value["floor"]),
            _valid_method_claim(value["claim"]),
        )
    )


def _valid_method_state(state: object, count: int, claim: object) -> bool:
    if state == "live":
        return count >= 1
    return state == "recovered" and count == 0 and claim is None


def _valid_method_claim(value: object) -> bool:
    return value is None or (
        isinstance(value, dict)
        and set(value) == {"attempt_id", "run"}
        and all(isinstance(value[k], str) and bool(value[k]) for k in ("attempt_id", "run"))
    )


def _valid_safety_intent(key: object, value: object, *, version: int = 3) -> bool:
    return (
        isinstance(key, str)
        and bool(key)
        and isinstance(value, dict)
        and set(value)
        in (
            {"method", "run", "phase", "scope"},
            {"method", "run", "phase", "scope", "policy_recovery"},
            {"method", "run", "phase", "scope", "aux_bootstrap"},
        )
        and _valid_safety_intent_payload(value, version=version)
    )


def _valid_safety_intent_payload(value: dict[str, object], *, version: int) -> bool:
    return (
        isinstance(value["method"], str)
        and isinstance(value["run"], str)
        and value["phase"] in {"prepared", "unknown", "acknowledged_unknown"}
        and _valid_intent_scope(value["scope"])
        and _valid_policy_recovery(value.get("policy_recovery"), value["scope"])
        and (version == _SAFETY_RECEIPT_VERSION or "policy_recovery" not in value)
        and _valid_aux_bootstrap(value, version=version)
        and _valid_intent_disposition(value)
    )


def _valid_aux_bootstrap(intent: dict[str, object], *, version: int) -> bool:
    aux = intent.get("aux_bootstrap")
    if aux is None:
        return "aux_bootstrap" not in intent
    if version != _SAFETY_RECEIPT_VERSION or not isinstance(aux, dict):
        return False
    return _valid_aux_provenance(intent, aux)


def _valid_aux_provenance(intent: dict[str, object], aux: dict[str, object]) -> bool:
    fields = {
        "purpose",
        "main_scope",
        "aux_dc_id",
        "aux_key_id",
        "lease_id",
        "work_id",
        "attempt_generation",
        "source_generation",
        "contained",
    }
    if set(aux) not in (fields, fields | {"lock_scope", "lock_owner_id"}):
        return False
    if not isinstance(intent["run"], str) or not intent["run"]:
        return False
    methods = {
        "export": "telethon.tl.functions.auth.ExportAuthorizationRequest:e5bfffcd",
        "import": "telethon.tl.functions.auth.ImportAuthorizationRequest:a57a7dad",
    }
    main = aux["main_scope"]
    purpose = aux["purpose"]
    if not isinstance(purpose, str) or methods.get(purpose) != intent["method"]:
        return False
    if not _valid_intent_scope(main) or not isinstance(main, dict):
        return False
    if not _valid_aux_fields(intent, aux, main):
        return False
    binding = (
        main
        if purpose == "export"
        else {"dc_id": aux["aux_dc_id"], "key_id": aux["aux_key_id"], "account_id": main["account_id"]}
    )
    return intent["scope"] == binding


def _valid_aux_fields(intent: dict[str, object], aux: dict[str, object], main: dict[str, object]) -> bool:
    return all(
        (
            main["key_id"] is not None,
            main["account_id"] is not None,
            type(aux["aux_dc_id"]) is int and cast(int, aux["aux_dc_id"]) > 0,
            type(aux["aux_key_id"]) is int,
            aux["aux_key_id"] != main["key_id"],
            all(
                isinstance(aux[key], str) and bool(aux[key])
                for key in ("lease_id", "work_id", "attempt_generation", "source_generation")
            ),
            type(aux["contained"]) is bool,
            not aux["contained"] or intent["phase"] == "unknown",
            "lock_scope" not in aux or _valid_aux_lock_scope(aux["lock_scope"]),
            "lock_owner_id" not in aux or _valid_aux_lock_owner(aux["lock_owner_id"]),
        )
    )


def _valid_aux_lock_scope(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"version", "device", "inode"}
        and type(value["version"]) is int
        and value["version"] == 1
        and type(value["device"]) is int
        and value["device"] >= 0
        and type(value["inode"]) is int
        and value["inode"] > 0
    )


_AUX_OWNER_HEX_WIDTH = 32


def _valid_aux_lock_owner(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _AUX_OWNER_HEX_WIDTH
        and all(char in "0123456789abcdef" for char in value)
    )


def _valid_intent_disposition(value: dict[str, object]) -> bool:
    return value.get("policy_recovery") is None or (
        value["phase"] == "unknown" and rpc_effect(cast(str, value["method"])) is RpcEffect.NONMUTATING_READ
    )


def _valid_policy_recovery(value: object, scope: object) -> bool:
    if value is None:
        return True
    return (
        isinstance(value, dict)
        and set(value) == {"policy_version", "episode_id", "grant_generation", "scope"}
        and type(value["policy_version"]) is int
        and value["policy_version"] == 1
        and all(isinstance(value[key], str) and bool(value[key]) for key in ("episode_id", "grant_generation"))
        and value["scope"] == scope
        and _valid_intent_scope(value["scope"])
    )


def _is_contained_aux(item: dict[str, object]) -> bool:
    aux = item.get("aux_bootstrap")
    return isinstance(aux, dict) and aux.get("contained") is True and item["phase"] == "unknown"


def _intent_blocks(item: dict[str, object], scope: dict[str, object]) -> bool:
    aux = item.get("aux_bootstrap")
    if (
        _is_contained_aux(item)
        and isinstance(aux, dict)
        and aux["main_scope"] == {key: scope[key] for key in ("dc_id", "key_id", "account_id")}
    ):
        return False
    return item["phase"] != "acknowledged_unknown" and not (
        item["phase"] == "unknown"
        and item.get("policy_recovery") is not None
        and item["scope"] == {key: scope[key] for key in ("dc_id", "key_id", "account_id")}
    )


def _valid_intent_scope(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"dc_id", "key_id", "account_id"}
        and type(value["dc_id"]) is int
        and value["dc_id"] > 0
        and (value["key_id"] is None or type(value["key_id"]) is int)
        and (value["account_id"] is None or (type(value["account_id"]) is int and value["account_id"] > 0))
    )


def _valid_safety_recovery(value: object, *, version: int = 3) -> bool:
    if value is None:
        return True
    fields = {"generation", "state", "scope", "consumed_run", "acknowledged_protection"}
    if not isinstance(value, dict):
        return False
    automatic = value.get("automatic")
    states = {"authorized", "consumed", "succeeded", "failed"}
    if automatic is not None:
        fields.add("automatic")
        states.update({"waiting", "fencing", "exhausted"} if version == _SAFETY_RECEIPT_VERSION else {"waiting"})
        if not _valid_automatic_state(automatic, value.get("state"), version):
            return False
    return set(value) == fields and all(
        (
            isinstance(value["generation"], str),
            value["state"] in states,
            _validate_safety_scope(value["scope"]),
            value["consumed_run"] is None or isinstance(value["consumed_run"], str),
            value["acknowledged_protection"] is None or isinstance(value["acknowledged_protection"], str),
        )
    )


def _valid_automatic_state(automatic: object, state: object, version: int) -> bool:
    if not _valid_automatic_recovery(automatic, version=version):
        return False
    return not (
        version == _SAFETY_RECEIPT_VERSION
        and state == "waiting"
        and cast(dict[str, object], automatic)["scheduled_due"] is None
    )


def _valid_automatic_recovery(value: object, *, version: int = 3) -> bool:
    if not isinstance(value, dict):
        return False
    if version < _SAFETY_RECEIPT_VERSION:
        return (
            set(value) == {"attempts", "due"}
            and type(value["attempts"]) is int
            and 0 <= value["attempts"] <= len(_AUTO_RESUME_DELAYS)
            and _valid_safety_floor(value["due"])
        )
    fields = {
        "episode_id",
        "policy_version",
        "cause",
        "attempts_total",
        "attempts_by_policy",
        "failure_stages",
        "quiet_anchor",
        "scheduled_due",
        "bound_intent_ids",
        "last_transition_key",
    }
    if set(value) != fields:
        return False
    return _valid_automatic_counts(value) and _valid_automatic_metadata(value)


def _valid_automatic_counts(value: dict[str, object]) -> bool:
    policies = {"flood_wait", "transient", "read_uncertainty"}
    attempts, stages = value["attempts_by_policy"], value["failure_stages"]
    if not all(isinstance(item, dict) and set(item) == policies for item in (attempts, stages)):
        return False
    attempts = cast(dict[str, object], attempts)
    stages = cast(dict[str, object], stages)
    if not all(type(count) is int and count >= 0 for item in (attempts, stages) for count in item.values()):
        return False
    if cast(int, attempts["flood_wait"]) > len(_AUTO_RESUME_DELAYS):
        return False
    return type(value["attempts_total"]) is int and value["attempts_total"] == sum(
        cast(dict[str, int], attempts).values()
    )


def _valid_automatic_metadata(value: dict[str, object]) -> bool:
    policies = {"flood_wait", "transient", "read_uncertainty"}
    bound = value["bound_intent_ids"]
    return all(
        (
            isinstance(value["episode_id"], str) and bool(value["episode_id"]),
            type(value["policy_version"]) is int and value["policy_version"] == 1,
            isinstance(value["cause"], str) and value["cause"] in policies,
            value["quiet_anchor"] is None or _valid_safety_floor(value["quiet_anchor"]),
            value["scheduled_due"] is None or _valid_safety_floor(value["scheduled_due"]),
            _valid_bound_intents(bound),
            value["last_transition_key"] is None
            or (isinstance(value["last_transition_key"], str) and bool(value["last_transition_key"])),
        )
    )


def _valid_bound_intents(value: object) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(item, str) and bool(item) for item in value)
        and len(value) == len(set(value))
    )


def _valid_safety_transition(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict) or set(value) != {
        "purpose",
        "from",
        "to_dc",
        "phase",
        "target_key",
        "owned_generation",
        "run",
        "expected_account",
        "validated_account",
        "acknowledged_protection",
    }:
        return False
    return all(
        (
            value["purpose"] in {"dc_migration", "authentication", "logout"},
            _validate_safety_scope(value["from"]),
            type(value["to_dc"]) is int and value["to_dc"] > 0,
            _valid_transition_key(value["phase"], value["target_key"]),
            type(value["owned_generation"]) is int and value["owned_generation"] >= 0,
            isinstance(value["run"], str),
            all(
                value[key] is None or (type(value[key]) is int and value[key] > 0)
                for key in ("expected_account", "validated_account")
            ),
            value["acknowledged_protection"] is None or isinstance(value["acknowledged_protection"], str),
        )
    )


def _valid_transition_key(phase: object, key: object) -> bool:
    if phase == "prepared":
        return key is None
    if phase == "key_bound":
        return type(key) is int
    return phase == "logout_confirmed" and (key is None or type(key) is int)


@dataclass(slots=True)
class _MethodRetryCredit:
    selected: WorkRevision
    method: str
    generation: str
    accounting: RpcAttemptAccounting
    allowance: int
    granted: bool = False
    authorization: RpcRetryAuthorization | None = None

    @property
    def limit(self) -> int:
        return self.accounting.limit

    @property
    def attempts(self) -> int:
        return self.accounting.attempts

    @property
    def available(self) -> bool:
        if self.authorization is not None and time.monotonic() >= self.authorization.deadline:
            return False
        return self.attempts < self.limit or not self.granted

    def authorize(self) -> RpcRetryAuthorization | None:
        if not self.available:
            return None
        if self.authorization is None:
            self.authorization = self._new_authorization()
        return self.authorization

    def _new_authorization(self) -> RpcRetryAuthorization:
        return RpcRetryAuthorization(
            time.monotonic() + demand_contract(self.selected.key.demand_kind).admission_timeout_seconds
        )

    def debit(self, budget: RpcAttemptBudget) -> None:
        if self.attempts >= self.limit:
            if self.authorization is None:
                self.authorization = self._new_authorization()
            self.accounting.limit += self.allowance
            self.granted = True
        self.accounting.attempts += 1
        budget._accounting = self.accounting


class _MethodReceipt(dict[str, object]):
    """Durable method payload with bounded run-local retry bindings."""

    def __init__(self, payload: dict[str, object], *, credit_enabled: bool = False) -> None:
        super().__init__(payload)
        self.credit_enabled = credit_enabled
        self.credits: dict[WorkRevision, _MethodRetryCredit] = {}


def _safety_identity_projection(record: dict[str, object]) -> tuple[object, object, object]:
    protection = record.get("protection")
    identity_failures = {
        "safety_auth_scope_mismatch",
        "safety_canonical_owner_mismatch",
        "authentication_account_mismatch",
        "invalid_account_identity",
    }
    return record.get("scope"), record.get("transition"), protection if protection in identity_failures else None


class TelegramRpcSafetyLedger:
    """Keep prewrite uncertainty durable without binding it to caller cancellation."""

    def __init__(
        self,
        store: TelegramRpcSafetyStore,
        *,
        dc_id: int,
        key_id: int | None,
        boot_id: str | None = None,
        image_scope_cell: ScopeCell | None = None,
    ) -> None:
        self._store = store
        self._image_scope_cell = image_scope_cell
        self._authentication_authorized = False
        self._boot_id = boot_id if boot_id is not None else _system_boot_id()
        self._run = uuid.uuid4().hex
        self._lock = asyncio.Lock()
        self._operations: set[asyncio.Task[None]] = set()
        self._claimed: set[str] = set()
        self._storage_failed = False
        self._startup_hold: str | None = None
        self._encoded: str | None = None
        self._record: dict[str, object] = {}
        self._read_scope_valid = False
        self._auto_resume_enabled = False
        self._auto_resume_rebased = False
        try:
            self._encoded = store.load_record()
            self._load(dc_id, key_id)
        except Exception:  # noqa: BLE001 - an unreadable safety backend cannot grant outbound writes.
            self._storage_failed = True
            self._startup_hold = self._startup_hold or "invalid_safety_receipt"

    def _load(self, dc_id: int, key_id: int | None) -> None:
        if self._encoded is None:
            self._initialize_record(dc_id, key_id)
            return
        decoded = cast(object, json.loads(self._encoded))
        if not _validate_safety_record(decoded):
            raise ValueError("invalid Telegram safety receipt")
        self._record = cast(dict[str, object], decoded)
        self._migrate_safety_record()
        scope = cast(dict[str, object], self._record["scope"])
        self._startup_hold = self._scope_startup_hold(scope, dc_id, key_id)
        self._restore_startup_protection(scope)
        self._rebase_safety_record()
        self._retain_method_bindings(self._record)

    def _migrate_safety_record(self) -> None:
        version = cast(int, self._record["version"])
        if version == 1:
            self._record["methods"] = {}
        recovery = self._record["recovery"]
        if (
            version < _SAFETY_RECEIPT_VERSION
            and isinstance(recovery, dict)
            and isinstance(recovery.get("automatic"), dict)
        ):
            automatic = recovery["automatic"]
            attempts = cast(int, automatic["attempts"])
            recovery["automatic"] = {
                "episode_id": uuid.uuid4().hex,
                "policy_version": 1,
                "cause": "flood_wait",
                "attempts_total": attempts,
                "attempts_by_policy": {"flood_wait": attempts, "transient": 0, "read_uncertainty": 0},
                "failure_stages": {"flood_wait": attempts, "transient": 0, "read_uncertainty": 0},
                "quiet_anchor": None,
                "scheduled_due": automatic["due"],
                "bound_intent_ids": [],
                "last_transition_key": None,
            }
            if recovery["state"] == "failed" and self._record["protection"] == "automatic_resume_exhausted":
                recovery["state"] = "exhausted"
        self._auto_resume_rebased = version < _SAFETY_RECEIPT_VERSION
        self._record["version"] = _SAFETY_RECEIPT_VERSION
        if not _validate_safety_record(self._record):
            raise ValueError("invalid migrated Telegram safety receipt")

    def _rebase_safety_record(self) -> None:
        legacy = self._load_legacy_floor()
        floor = cast(dict[str, object], self._record["floor"])
        floor["until_utc"] = max(float(cast(float, floor["until_utc"])), float(legacy or 0))
        self._rebase_floor(floor)
        for episode in cast(dict[str, dict[str, object]], self._record["methods"]).values():
            self._rebase_floor(cast(dict[str, object], episode["floor"]))
        recovery = self._record["recovery"]
        if isinstance(recovery, dict) and isinstance(recovery.get("automatic"), dict):
            automatic = recovery["automatic"]
            for field in ("scheduled_due", "quiet_anchor"):
                floor = automatic[field]
                if isinstance(floor, dict):
                    self._auto_resume_rebased |= floor["boot_id"] != self._boot_id
                    self._rebase_floor(floor)

    def _initialize_record(self, dc_id: int, key_id: int | None) -> None:
        legacy = self._load_legacy_floor()
        owners = self._store.canonical_owners()
        self._record = {
            "version": _SAFETY_RECEIPT_VERSION,
            "generation": uuid.uuid4().hex,
            "scope": {
                "stage": "transport_only" if key_id is not None else "key_pending",
                "dc_id": dc_id,
                "key_id": key_id,
                "account_id": None,
            },
            "floor": {
                "boot_id": self._boot_id,
                "until_monotonic": 0.0,
                "until_utc": float(legacy or 0),
                "remaining_seconds": max(0.0, float(legacy or 0) - time.time()),
            },
            "intents": {},
            "protection": None,
            "recovery": None,
            "transition": None,
            "methods": {},
        }
        if not _validate_safety_record(self._record):
            raise ValueError("invalid initial transport safety scope")
        if key_id is None and any(owner is not None for owner in owners):
            self._startup_hold = "new_key_conflicts_with_owned_state"

    def _scope_startup_hold(self, scope: dict[str, object], dc_id: int, key_id: int | None) -> str | None:
        if (scope["dc_id"], scope["key_id"]) != (dc_id, key_id):
            return "safety_auth_scope_mismatch"
        if scope["stage"] == "verified" and any(
            owner is not None and owner != scope["account_id"] for owner in self._store.canonical_owners()
        ):
            return "safety_canonical_owner_mismatch"
        return None

    def _restore_startup_protection(self, scope: dict[str, object]) -> None:
        self._read_scope_valid = self._startup_hold is None and scope["stage"] == "verified"
        if self._unresolved():
            self._startup_hold = self._startup_hold or "unresolved_outbound_outcome"
        if self._record["transition"] is not None:
            self._startup_hold = self._startup_hold or "transport_transition_incomplete"
        recovery = self._record["recovery"]
        if (
            isinstance(recovery, dict)
            and recovery["state"] == "consumed"
            and self._startup_hold not in {"safety_auth_scope_mismatch", "safety_canonical_owner_mismatch"}
        ):
            self._startup_hold = "recovery_trial_incomplete"

    def _rebase_floor(self, floor: dict[str, object]) -> None:
        if floor["boot_id"] != self._boot_id:
            remaining = max(
                float(cast(float, floor["remaining_seconds"])),
                float(cast(float, floor["until_utc"])) - time.time(),
                0.0,
            )
            floor.update(
                boot_id=self._boot_id,
                until_monotonic=time.monotonic() + remaining,
                until_utc=max(float(cast(float, floor["until_utc"])), time.time() + remaining),
                remaining_seconds=remaining,
            )

    def _load_legacy_floor(self) -> float | None:
        value = self._store.load_legacy_until_utc()
        return _validate_cooldown_until_utc(value) if value is not None else None

    def _unresolved(self) -> bool:
        intents = cast(dict[str, dict[str, object]], self._record.get("intents", {}))
        scope = cast(dict[str, object], self._record["scope"])
        return any(_intent_blocks(item, scope) for item in intents.values())

    @property
    def floor_deadline(self) -> float:
        if not self._record:
            return math.inf
        return self._floor_release_at(cast(dict[str, object], self._record["floor"]))

    def _floor_release_at(self, floor: dict[str, object]) -> float:
        utc_remaining = max(0.0, float(cast(float, floor["until_utc"])) - time.time())
        if floor["boot_id"] == self._boot_id:
            return max(float(cast(float, floor["until_monotonic"])), time.monotonic() + utc_remaining)
        return time.monotonic() + max(utc_remaining, float(cast(float, floor["remaining_seconds"])))

    def method_release_at(self, method: str | None, attempt_id: str | None = None) -> float:
        if self._storage_failed:
            return math.inf
        episode = cast(dict[str, dict[str, object]], self._record.get("methods", {})).get(method or "")
        if episode is None or episode.get("state") == "recovered":
            return 0.0
        claim = episode["claim"]
        if isinstance(claim, dict) and (claim["attempt_id"] != attempt_id or claim["run"] != self._run):
            return math.inf
        return self._floor_release_at(cast(dict[str, object], episode["floor"]))

    def method_claim_valid(self, method: str, attempt_id: str, generation: str | None) -> bool:
        episode = cast(dict[str, dict[str, object]], self._record["methods"]).get(method)
        if episode is None or episode.get("state") == "recovered":
            return generation is None
        return episode["generation"] == generation and episode["claim"] == {
            "attempt_id": attempt_id,
            "run": self._run,
        }

    async def prepare_method_trial(self, attempt_id: str, method: str, *, auth_only: bool = False) -> str | None:
        """Issue the single current-episode permission before any physical claim."""
        self.require_writable(auth_only=auth_only)
        current = cast(dict[str, dict[str, object]], self._record["methods"]).get(method)
        if current is None or current.get("state") == "recovered":
            return None
        generation: str | None = None

        def reserve(record: dict[str, object]) -> None:
            nonlocal generation
            self.require_writable(auth_only=auth_only)
            episode = cast(dict[str, dict[str, object]], record["methods"]).get(method)
            if episode is None or episode.get("state") == "recovered":
                return
            generation = cast(str, episode["generation"])
            deferred = self.method_deferral(method, attempt_id)
            if deferred is not None:
                raise deferred
            if attempt_id not in cast(dict[str, object], record["intents"]):
                raise RuntimeError("method recovery lacks its durable application intent")
            episode["claim"] = {"attempt_id": attempt_id, "run": self._run}

        await self._commit(reserve)
        return generation

    def method_deferral(
        self, method: str, attempt_id: str | None = None
    ) -> TelegramRpcAdmissionDeferred | TelegramRpcMethodRecoveryPending | None:
        episode = cast(dict[str, dict[str, object]], self._record["methods"]).get(method)
        if episode is None or episode.get("state") == "recovered":
            return None
        generation = cast(str, episode["generation"])
        claim = episode["claim"]
        if isinstance(claim, dict) and (claim["attempt_id"] != attempt_id or claim["run"] != self._run):
            return TelegramRpcMethodRecoveryPending(method, generation)
        deadline = self._floor_release_at(cast(dict[str, object], episode["floor"]))
        if deadline <= time.monotonic():
            return None
        return TelegramRpcAdmissionDeferred(
            max(1, math.ceil(deadline - time.monotonic())),
            method_key=method,
            generation=generation,
            eligible_at=deadline,
            reason="method",
        )

    @property
    def protection_reason(self) -> str | None:
        if self._storage_failed:
            return "safety_storage_failure"
        return self._startup_hold or cast(str | None, self._record.get("protection"))

    @property
    def verified_account_id(self) -> int | None:
        if not self._read_scope_valid:
            return None
        if self._startup_hold in {"safety_auth_scope_mismatch", "safety_canonical_owner_mismatch"} or self._record.get(
            "protection"
        ) in {"safety_auth_scope_mismatch", "safety_canonical_owner_mismatch"}:
            return None
        transition = self._record.get("transition")
        if (
            isinstance(transition, dict)
            and transition["purpose"] == "logout"
            and transition["phase"] == "logout_confirmed"
        ):
            return None
        scope = self._record.get("scope")
        return cast(int, scope["account_id"]) if isinstance(scope, dict) and scope["stage"] == "verified" else None

    def status(self) -> dict[str, object]:
        unresolved = [
            item
            for item in cast(dict[str, dict[str, object]], self._record.get("intents", {})).values()
            if item["phase"] != "acknowledged_unknown"
        ]
        return {
            "generation": self._record.get("generation"),
            "reason": self.protection_reason,
            "retry_until_utc": time.time() + self.floor_deadline - time.monotonic()
            if time.monotonic() < self.floor_deadline < math.inf
            else None,
            "unresolved_request_count": len(unresolved),
            "unresolved_requests": [
                {"method": _safe_diagnostic_method(cast(str, item["method"])), "phase": item["phase"]}
                for item in unresolved[:_DIAGNOSTIC_REQUESTS_MAX]
            ],
            "unresolved_requests_truncated": len(unresolved) > _DIAGNOSTIC_REQUESTS_MAX,
        }

    def require_writable(self, *, auth_only: bool = False) -> None:
        if auth_only:
            if self._authentication_authorized and not self._storage_failed:
                return
            raise RuntimeError("authentication authorization is not active")
        if self.protection_reason is not None:
            raise TelegramRpcThrottled(
                retry_after_seconds=None, latched=True, detail="Telegram outbound safety protection is active"
            )

    async def _commit(self, change: Callable[[dict[str, object]], None]) -> None:
        if self._storage_failed:
            self.require_writable()
        task = asyncio.create_task(self._commit_owned(change))
        self._operations.add(task)
        task.add_done_callback(self._operation_done)
        await asyncio.shield(task)

    async def _commit_owned(self, change: Callable[[dict[str, object]], None]) -> None:
        async with self._lock:
            updated = cast(dict[str, object], json.loads(json.dumps(self._record)))
            self._retain_method_bindings(updated)
            change(updated)
            if not _validate_safety_record(updated):
                raise ValueError("invalid outbound safety mutation")
            encoded = json.dumps(updated, separators=(",", ":"), sort_keys=True, allow_nan=False)
            cell = self._image_scope_cell
            identity_changed = _safety_identity_projection(updated) != _safety_identity_projection(self._record)
            scope_change = cell.begin_change() if cell is not None and identity_changed else None
            try:
                await self._store.save_record(self._encoded, encoded)
            except Exception:
                self._storage_failed = True
                if cell is not None:
                    cell.publish(None)
                raise
            else:
                self._record, self._encoded = updated, encoded
            finally:
                if cell is not None and scope_change is not None:
                    cell.end_change(scope_change)

    def _retain_method_bindings(self, updated: dict[str, object]) -> None:
        previous = cast(dict[str, dict[str, object]], self._record["methods"])
        episodes = cast(dict[str, dict[str, object]], updated["methods"])
        for method, payload in episodes.items():
            receipt = _MethodReceipt(payload)
            prior = previous.get(method)
            if isinstance(prior, _MethodReceipt) and prior["generation"] == payload["generation"]:
                receipt.credit_enabled = prior.credit_enabled
                receipt.credits = prior.credits
            episodes[method] = receipt

    def bind_method_retry(self, budget: RpcAttemptBudget, method: str, capacity: int) -> _MethodRetryCredit | None:
        selected = budget.selected
        episode = cast(dict[str, dict[str, object]], self._record["methods"]).get(method)
        if selected is None or not isinstance(episode, _MethodReceipt):
            return None
        if selected.key.account_id != self.verified_account_id or not episode.credit_enabled:
            return None
        credit = episode.credits.get(selected)
        if credit is None:
            if episode.get("state") == "recovered" or len(episode.credits) >= capacity:
                return None
            contract = demand_contract(selected.key.demand_kind)
            allowance = contract.max_rpc_attempts_per_slice
            if allowance is None:
                return None
            credit = _MethodRetryCredit(
                selected, method, cast(str, episode["generation"]), self._revision_accounting(budget), allowance
            )
            episode.credits[selected] = credit
        budget._accounting = credit.accounting
        return credit

    def _revision_accounting(self, budget: RpcAttemptBudget) -> RpcAttemptAccounting:
        selected = budget.selected
        for receipt in cast(dict[str, dict[str, object]], self._record["methods"]).values():
            if isinstance(receipt, _MethodReceipt) and selected is not None and selected in receipt.credits:
                return receipt.credits[selected].accounting
        return budget._accounting

    def _operation_done(self, task: asyncio.Task[None]) -> None:
        self._operations.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error is None:
                self._claimed.intersection_update(cast(dict[str, object], self._record["intents"]))

    async def drain(self) -> None:
        """Finish shielded mutations before the sole SQLite owner closes."""
        cancelled = False
        while self._operations:
            batch = tuple(self._operations)
            pending = asyncio.gather(*batch, return_exceptions=True)
            while not pending.done():
                try:
                    await asyncio.wait({pending})
                except asyncio.CancelledError:
                    cancelled = True
            # Each mutation has already applied its own storage-failure fence;
            # retrieve semantic refusals without relabeling them as I/O faults.
            pending.result()
            self._operations.difference_update(batch)
        if cancelled:
            raise asyncio.CancelledError

    async def prepare(
        self,
        attempt_id: str,
        method: str,
        *,
        transport_identity: tuple[int, int | None] | None = None,
        auth_only: bool = False,
        aux_bootstrap: dict[str, object] | None = None,
    ) -> None:
        self.require_writable(auth_only=auth_only)

        def attach_aux(intent: dict[str, object], scope: dict[str, object], provenance: dict[str, object]) -> None:
            main_scope = {key: scope[key] for key in ("dc_id", "key_id", "account_id")}
            if scope["stage"] != "verified" or provenance.get("main_scope") != main_scope:
                raise ValueError("auxiliary bootstrap does not own the verified main scope")
            if provenance.get("contained") is not False:
                raise ValueError("auxiliary bootstrap must start uncontained")
            if not _valid_aux_lock_scope(provenance.get("lock_scope")):
                raise ValueError("auxiliary bootstrap requires a pinned session lock")
            if not _valid_aux_lock_owner(provenance.get("lock_owner_id")):
                raise ValueError("auxiliary bootstrap requires a session lock owner")
            intent["aux_bootstrap"] = provenance
            if not _valid_safety_intent(attempt_id, intent):
                raise ValueError("invalid auxiliary bootstrap provenance")

        def add(record: dict[str, object]) -> None:
            if self.protection_reason is not None:
                self.require_writable(auth_only=auth_only)
            intents = cast(dict[str, object], record["intents"])
            scope = cast(dict[str, object], record["scope"])
            dc_id, key_id = transport_identity or (cast(int, scope["dc_id"]), cast(int | None, scope["key_id"]))
            transition = record["transition"]
            previous = cast(dict[str, object], transition["from"]) if isinstance(transition, dict) else scope
            binding = {"dc_id": dc_id, "key_id": key_id, "account_id": previous["account_id"]}
            intent: dict[str, object] = {"method": method, "run": self._run, "phase": "prepared", "scope": binding}
            if aux_bootstrap is not None:
                provenance = cast(dict[str, object], json.loads(json.dumps(aux_bootstrap)))
                attach_aux(intent, scope, provenance)
            existing = intents.get(attempt_id)
            if aux_bootstrap is not None and existing is not None and existing != intent:
                raise ValueError("outbound attempt identity changed")
            intents.setdefault(attempt_id, intent)
            floor = cast(dict[str, object], record["floor"])
            self._rebase_floor(floor)

        await self._commit(add)

    async def _contain_aux_unknown(self, attempt_id: str, *, captured_scope: dict[str, object], lease_id: str) -> None:
        """Record gate-proven retirement without changing the unknown outcome."""

        def contain(record: dict[str, object]) -> None:
            scope = cast(dict[str, object], record["scope"])
            intent = cast(dict[str, dict[str, object]], record["intents"]).get(attempt_id)
            aux = intent.get("aux_bootstrap") if intent else None
            main = {key: scope[key] for key in ("dc_id", "key_id", "account_id")}
            if not isinstance(aux, dict) or intent is None:
                raise ValueError("auxiliary retirement lacks an owned intent")
            if not all(
                (
                    scope["stage"] == "verified",
                    captured_scope == main,
                    intent["run"] == self._run,
                    intent["phase"] == "unknown",
                    aux["main_scope"] == main,
                    aux["lease_id"] == lease_id,
                )
            ):
                raise ValueError("auxiliary retirement does not match the owned unknown intent")
            aux["contained"] = True
            if record["protection"] == "unresolved_outbound_outcome" and not any(
                _intent_blocks(item, scope) for item in cast(dict[str, dict[str, object]], record["intents"]).values()
            ):
                record["protection"] = None
            record["generation"] = uuid.uuid4().hex

        await self._commit(contain)
        if self._startup_hold == "unresolved_outbound_outcome" and not self._unresolved():
            self._startup_hold = None

    async def _reconcile_aux_after_restart(self, lease: TelegramSessionLease) -> None:
        """Contain prior auxiliary uncertainty only while the same kernel lease is pinned."""
        if type(lease) is not TelegramSessionLease or not lease.is_held():
            raise ValueError("a live Telegram session lease is required")
        pin = lease._pin()
        try:
            lock_scope = pin.identity()
            owner_id = pin.owner_id()
            if not any(
                self._restart_aux_matches(item, self._record, lock_scope, owner_id)
                for item in cast(dict[str, dict[str, object]], self._record.get("intents", {})).values()
            ):
                return

            def reconcile(record: dict[str, object]) -> None:
                if not lease.is_held() or lease.identity() != lock_scope or lease.owner_id() != owner_id:
                    raise ValueError("Telegram session lease was revoked")
                scope = cast(dict[str, object], record["scope"])
                intents = cast(dict[str, dict[str, object]], record["intents"])
                for item in intents.values():
                    if self._restart_aux_matches(item, record, lock_scope, owner_id):
                        item["phase"] = "unknown"
                        cast(dict[str, object], item["aux_bootstrap"])["contained"] = True
                if record["protection"] == "unresolved_outbound_outcome" and not any(
                    _intent_blocks(item, scope) for item in intents.values()
                ):
                    record["protection"] = None
                record["generation"] = uuid.uuid4().hex

            task = asyncio.create_task(self._commit(reconcile))
            cancelled = False
            while not task.done():
                try:
                    await asyncio.wait({task})
                except asyncio.CancelledError:
                    cancelled = True
            task.result()
            if self._startup_hold == "unresolved_outbound_outcome" and not self._unresolved():
                self._startup_hold = None
            if cancelled:
                raise asyncio.CancelledError
        finally:
            pin._release()

    def _restart_aux_matches(
        self, intent: dict[str, object], record: dict[str, object], lock_scope: dict[str, int], owner_id: str
    ) -> bool:
        scope = cast(dict[str, object], record["scope"])
        aux = intent.get("aux_bootstrap")
        if not self._read_scope_valid or scope["stage"] != "verified" or record["transition"] is not None:
            return False
        if not isinstance(aux, dict) or not _valid_aux_bootstrap(intent, version=_SAFETY_RECEIPT_VERSION):
            return False
        return all(
            (
                intent["run"] != self._run,
                intent["phase"] in {"prepared", "unknown"},
                aux["contained"] is False,
                aux.get("lock_scope") == lock_scope,
                aux.get("lock_owner_id") is not None and aux["lock_owner_id"] != owner_id,
                aux["main_scope"] == {key: scope[key] for key in ("dc_id", "key_id", "account_id")},
                all(owner in {None, scope["account_id"]} for owner in self._store.canonical_owners()),
            )
        )

    def mark_claimed(self, attempt_id: str, *, auth_only: bool = False) -> None:
        self.require_writable(auth_only=auth_only)
        if attempt_id not in cast(dict[str, object], self._record["intents"]):
            raise RuntimeError("outbound attempt lacks durable intent")
        self._claimed.add(attempt_id)

    async def complete(  # noqa: PLR0913 - one atomic terminal receipt owns account and method restrictions.
        self,
        attempt_id: str,
        *,
        known_outcome: bool,
        floor_until_utc: float | None = None,
        circuit_open: bool = False,
        actual_flood: bool = False,
        method_generation: str | None = None,
        method_success: bool = False,
        recovery_cause: RecoveryCause | None = None,
    ) -> None:
        def settle(record: dict[str, object]) -> None:
            self._settle_method(record, attempt_id, actual_flood, method_generation, method_success)
            self._settle_intent(record, attempt_id, known_outcome)
            if floor_until_utc is not None:
                self._settle_restriction(record, floor_until_utc)
            current_trial = record["recovery"]
            if circuit_open and not (isinstance(current_trial, dict) and current_trial.get("state") == "exhausted"):
                record["protection"] = "flood_wait_circuit_open"
                record["generation"] = uuid.uuid4().hex
            if (
                (not known_outcome and attempt_id in self._claimed)
                or floor_until_utc is not None
                or recovery_cause is not None
            ):
                self._invalidate_recovery_trial(
                    record, known_flood=known_outcome and floor_until_utc is not None, cause=recovery_cause
                )

        if not known_outcome and attempt_id in self._claimed:
            self._startup_hold = "unresolved_outbound_outcome"
        await self._commit(settle)
        self._claimed.discard(attempt_id)

    def _settle_restriction(self, record: dict[str, object], floor_until_utc: float) -> None:
        self._extend_floor(record, floor_until_utc)
        transition = record["transition"]
        if isinstance(transition, dict) and transition["purpose"] == "authentication":
            record["protection"] = "authentication_trial_failed"
            record["generation"] = uuid.uuid4().hex

    def _settle_method(
        self,
        record: dict[str, object],
        attempt_id: str,
        actual_flood: bool,
        generation: str | None,
        success: bool,
    ) -> None:
        intent = cast(dict[str, dict[str, object]], record["intents"]).get(attempt_id)
        if intent is None or intent["run"] != self._run:
            return
        method = cast(str, intent["method"])
        episodes = cast(dict[str, dict[str, object]], record["methods"])
        episode = episodes.get(method)
        if actual_flood and attempt_id in self._claimed and _valid_method_key(method):
            self._record_method_flood(episodes, method, episode)
        elif episode is not None and episode["claim"] == {"attempt_id": attempt_id, "run": self._run}:
            if success and episode["generation"] == generation:
                episode.update(state="recovered", consecutive_actual_floods=0, claim=None)
            else:
                episode["claim"] = None

    def _record_method_flood(
        self, episodes: dict[str, dict[str, object]], method: str, previous: dict[str, object] | None
    ) -> None:
        prior_live = previous is not None and previous.get("state", "live") == "live"
        count = (cast(int, previous["consecutive_actual_floods"]) if prior_live and previous is not None else 0) + 1
        delay = backoff_seconds(RecoveryCause.FLOOD_WAIT, count - 1)
        remaining = float(delay)
        if prior_live:
            assert previous is not None
            remaining = max(
                remaining, self._floor_release_at(cast(dict[str, object], previous["floor"])) - time.monotonic()
            )
        episodes[method] = _MethodReceipt(
            {
                "state": "live",
                "generation": uuid.uuid4().hex,
                "consecutive_actual_floods": count,
                "floor": {
                    "boot_id": self._boot_id,
                    "until_monotonic": time.monotonic() + remaining,
                    "until_utc": time.time() + remaining,
                    "remaining_seconds": remaining,
                },
                "claim": None,
            },
            credit_enabled=True,
        )

    def _settle_intent(self, record: dict[str, object], attempt_id: str, known_outcome: bool) -> None:
        intents = cast(dict[str, object], record["intents"])
        prior = intents.get(attempt_id)
        if isinstance(prior, dict) and prior["run"] != self._run:
            raise ValueError("completion does not own this outbound intent")
        if isinstance(prior, dict) and _is_contained_aux(prior):
            return
        if known_outcome or (
            attempt_id not in self._claimed and (not isinstance(prior, dict) or prior["phase"] == "prepared")
        ):
            intents.pop(attempt_id, None)
        elif attempt_id in intents:
            self._mark_unknown_intent(record, cast(dict[str, object], intents[attempt_id]))

    @staticmethod
    def _mark_unknown_intent(record: dict[str, object], intent: dict[str, object]) -> None:
        intent["phase"] = "unknown"
        trial = record["recovery"]
        if record["protection"] in {None, "unresolved_outbound_outcome"} and not (
            isinstance(trial, dict) and trial.get("state") == "exhausted"
        ):
            record["protection"] = "unresolved_outbound_outcome"
        record["generation"] = uuid.uuid4().hex

    def _invalidate_recovery_trial(
        self, record: dict[str, object], *, known_flood: bool = False, cause: RecoveryCause | None = None
    ) -> None:
        trial = record["recovery"]
        if not isinstance(trial, dict):
            return
        if "automatic" in trial:
            self._invalidate_automatic_trial(record, trial, known_flood, cause)
            return
        if trial["state"] == "authorized":
            record["generation"] = uuid.uuid4().hex
        if trial["state"] == "consumed":
            trial["state"] = "failed"
            record["protection"] = "recovery_trial_failed"
            record["generation"] = uuid.uuid4().hex

    def _invalidate_automatic_trial(
        self, record: dict[str, object], trial: dict[str, object], known_flood: bool, cause: RecoveryCause | None
    ) -> None:
        if trial["state"] in {"consumed", "succeeded"} and trial["consumed_run"] == self._run:
            self._schedule_auto_resume(
                record, cause=RecoveryCause.FLOOD_WAIT if known_flood else cause or RecoveryCause.READ_UNCERTAINTY
            )
        elif known_flood and trial["state"] in {"fencing", "waiting"}:
            self._schedule_auto_resume(record, cause=RecoveryCause.FLOOD_WAIT)

    def _extend_floor(self, record: dict[str, object], until_utc: float) -> None:
        if not _finite_safety_number(until_utc):
            raise ValueError("invalid safety floor")
        floor = cast(dict[str, object], record["floor"])
        remaining = max(0.0, until_utc - time.time())
        floor.update(
            boot_id=self._boot_id,
            until_utc=max(float(cast(float, floor["until_utc"])), until_utc),
            until_monotonic=max(self.floor_deadline, time.monotonic() + remaining),
            remaining_seconds=max(0.0, self.floor_deadline - time.monotonic(), remaining),
        )

    async def record_floor(self, until_utc: float, *, circuit_open: bool = False) -> None:
        def update(record: dict[str, object]) -> None:
            self._extend_floor(record, until_utc)
            trial = record["recovery"]
            if circuit_open and not (isinstance(trial, dict) and trial.get("state") == "exhausted"):
                record["protection"] = "flood_wait_circuit_open"
                record["generation"] = uuid.uuid4().hex
            self._invalidate_recovery_trial(record, known_flood=True)

        await self._commit(update)

    async def verify_account(self, account_id: int) -> None:
        if type(account_id) is not int or account_id < 1:
            raise ValueError("invalid verified account")

        def bind(record: dict[str, object]) -> None:
            scope = cast(dict[str, object], record["scope"])
            self._check_account_publication(scope, account_id)
            previous = scope["account_id"]
            if previous is not None and previous != account_id:
                raise RuntimeError("verified safety account changed")
            transition = record["transition"]
            if isinstance(transition, dict) and transition["expected_account"] not in {None, account_id}:
                raise RuntimeError("transport transition account changed")
            scope.update(stage="verified", account_id=account_id)
            if isinstance(transition, dict) and transition["purpose"] == "dc_migration":
                transition["validated_account"] = account_id

        await self._commit(bind)
        self._read_scope_valid = True

    def _check_account_publication(self, scope: dict[str, object], account_id: int) -> None:
        if scope["key_id"] is None:
            raise ValueError("account verification requires a bound transport key")
        owners = self._store.canonical_owners()
        verified_same = scope["stage"] == "verified" and scope["account_id"] == account_id
        compatible = all(owner is None or owner == account_id for owner in owners)
        if not compatible or (not verified_same and owners not in {(None, None), (account_id, account_id)}):
            if self._image_scope_cell is not None:
                self._image_scope_cell.publish(None)
            self._startup_hold = "safety_canonical_owner_mismatch"
            self.require_writable()

    async def bind_key(self, dc_id: int, key_id: int, *, auth_only: bool = False) -> None:
        self.require_writable(auth_only=auth_only)

        def bind(record: dict[str, object]) -> None:
            self.require_writable(auth_only=auth_only)
            scope = cast(dict[str, object], record["scope"])
            transition = record["transition"]
            if isinstance(transition, dict) and transition["to_dc"] == dc_id and transition["run"] == self._run:
                if transition["phase"] != "prepared" and transition["target_key"] != key_id:
                    raise RuntimeError("DC transition transport key changed")
                transition.update(phase="key_bound", target_key=key_id)
                scope.update(stage="transport_only", dc_id=dc_id, key_id=key_id, account_id=None)
                return
            if scope["stage"] != "key_pending" or scope["dc_id"] != dc_id:
                raise RuntimeError("unowned authentication transition")
            scope.update(stage="transport_only", key_id=key_id)

        await self._commit(bind)

    async def begin_dc_transition(self, dc_id: int, owned_generation: int) -> None:
        self.require_writable(auth_only=self._authentication_authorized)

        def begin(record: dict[str, object]) -> None:
            self.require_writable(auth_only=self._authentication_authorized)
            prior = record["transition"]
            if prior is not None:
                if isinstance(prior, dict) and prior["purpose"] == "authentication" and prior["run"] == self._run:
                    prior.update(
                        to_dc=dc_id,
                        phase="prepared",
                        target_key=None,
                        owned_generation=owned_generation,
                        validated_account=None,
                    )
                    return
                if (
                    isinstance(prior, dict)
                    and prior["purpose"] == "dc_migration"
                    and prior["run"] == self._run
                    and prior["owned_generation"] == owned_generation
                ):
                    prior.update(to_dc=dc_id, phase="prepared", target_key=None, validated_account=None)
                    return
                raise RuntimeError("another Telegram transport transition is unresolved")
            record["transition"] = {
                "purpose": "dc_migration",
                "from": dict(cast(dict[str, object], record["scope"])),
                "to_dc": dc_id,
                "phase": "prepared",
                "target_key": None,
                "owned_generation": owned_generation,
                "run": self._run,
                "expected_account": cast(dict[str, object], record["scope"])["account_id"],
                "validated_account": None,
                "acknowledged_protection": None,
            }

        await self._commit(begin)

    async def finish_dc_transition(self, account_id: int) -> None:
        if type(account_id) is not int or account_id < 1:
            raise ValueError("invalid DC transition account")

        def finish(record: dict[str, object]) -> None:
            self.require_writable()
            transition = record["transition"]
            scope = cast(dict[str, object], record["scope"])
            self._check_dc_completion(transition, scope, account_id)
            if any(owner is not None and owner != account_id for owner in self._store.canonical_owners()):
                raise RuntimeError("DC transition canonical account changed")
            scope.update(stage="verified", account_id=account_id)
            record["transition"] = None

        await self._commit(finish)

    def _check_dc_completion(self, transition: object, scope: dict[str, object], account_id: int) -> None:
        if not isinstance(transition, dict) or transition["run"] != self._run or transition["phase"] != "key_bound":
            raise RuntimeError("DC transition identity is not current")
        previous = cast(dict[str, object], transition["from"])
        expected = previous["account_id"]
        if expected is not None and expected != account_id:
            raise RuntimeError("DC transition changed Telegram account")
        if scope["dc_id"] != transition["to_dc"] or scope["key_id"] != transition["target_key"]:
            raise RuntimeError("DC transition transport binding changed")

    async def reject_identity_result(self, *, mismatched_account: bool) -> None:
        def reject(record: dict[str, object]) -> None:
            record["protection"] = (
                "safety_canonical_owner_mismatch" if mismatched_account else "invalid_account_identity"
            )
            record["generation"] = uuid.uuid4().hex

        await self._commit(reject)
        self._authentication_authorized = False

    async def begin_authentication(
        self, dc_id: int, key_id: int | None, *, acknowledged: bool, replace_credentials: bool = False
    ) -> bool:
        """Authorize bounded AUTH; return whether the owned local key must clear."""
        if self._storage_failed or not self._record:
            raise RuntimeError("authentication safety storage is unavailable")
        clear_key = False

        def begin(record: dict[str, object]) -> None:
            nonlocal clear_key
            if self.floor_deadline > time.monotonic():
                raise ValueError("authentication cannot override a known Telegram restriction")
            scope = cast(dict[str, object], record["scope"])
            prior = record["transition"]
            origin = cast(dict[str, object], prior["from"]) if isinstance(prior, dict) else scope
            expected = self._authentication_expected_account(origin, prior)
            clear_key = self._authentication_clear_key(
                record, (dc_id, key_id), acknowledged=acknowledged, replace_credentials=replace_credentials
            )
            record["transition"] = {
                "purpose": "authentication",
                "from": dict(origin),
                "to_dc": dc_id,
                "phase": "prepared" if clear_key or key_id is None else "key_bound",
                "target_key": None if clear_key else key_id,
                "owned_generation": 0,
                "run": self._run,
                "expected_account": expected,
                "validated_account": None,
                "acknowledged_protection": self.protection_reason,
            }
            if clear_key:
                scope.update(stage="key_pending", dc_id=dc_id, key_id=None, account_id=None)

        await self._commit(begin)
        self._authentication_authorized = True
        return clear_key

    def _authentication_clear_key(
        self,
        record: dict[str, object],
        identity: tuple[int, int | None],
        *,
        acknowledged: bool,
        replace_credentials: bool,
    ) -> bool:
        scope = cast(dict[str, object], record["scope"])
        prior = record["transition"]
        known_logout, pending_clear = self._authentication_prior_options(prior)
        self._require_authentication_ack(record, prior, known_logout, acknowledged)
        identity_matches = (scope["dc_id"], scope["key_id"]) == identity
        clear_key = replace_credentials or pending_clear or known_logout
        if not identity_matches and not clear_key:
            raise RuntimeError("authentication transport binding does not match")
        return clear_key

    @staticmethod
    def _authentication_prior_options(prior: object) -> tuple[bool, bool]:
        if not isinstance(prior, dict):
            return False, False
        return (
            prior["purpose"] == "logout" and prior["phase"] == "logout_confirmed",
            prior["purpose"] == "authentication" and prior["phase"] == "prepared",
        )

    def _require_authentication_ack(
        self,
        record: dict[str, object],
        prior: object,
        known_logout: bool,
        acknowledged: bool,
    ) -> None:
        requires_ack = self._unresolved() or record["protection"] not in {None, "authentication_required"}
        if (requires_ack or (prior is not None and not known_logout)) and not acknowledged:
            raise ValueError("authentication requires acknowledgement of retained protection")

    def _authentication_expected_account(self, origin: dict[str, object], prior: object) -> int | None:
        expected = cast(int | None, origin["account_id"])
        if isinstance(prior, dict) and prior["expected_account"] is not None:
            expected = cast(int, prior["expected_account"])
        owners = self._store.canonical_owners()
        if expected is None and owners != (None, None):
            if owners[0] is None or owners[0] != owners[1]:
                raise RuntimeError("authentication canonical owners disagree")
            expected = owners[0]
        if any(owner is not None and owner != expected for owner in owners):
            raise RuntimeError("authentication canonical account changed")
        return expected

    async def begin_logout(self, owned_generation: int) -> None:
        self.require_writable()
        if self.verified_account_id is None:
            raise ValueError("logout requires the currently verified account")

        def begin(record: dict[str, object]) -> None:
            self.require_writable()
            if self.floor_deadline > time.monotonic() or record["transition"] is not None:
                raise ValueError("logout is not eligible")
            scope = cast(dict[str, object], record["scope"])
            record["transition"] = {
                "purpose": "logout",
                "from": dict(scope),
                "to_dc": scope["dc_id"],
                "phase": "key_bound",
                "target_key": scope["key_id"],
                "owned_generation": owned_generation,
                "run": self._run,
                "expected_account": scope["account_id"],
                "validated_account": scope["account_id"],
                "acknowledged_protection": None,
            }

        await self._commit(begin)
        self._authentication_authorized = True

    async def confirm_logout(self) -> None:
        def confirm(record: dict[str, object]) -> None:
            transition = record["transition"]
            if not isinstance(transition, dict) or transition["purpose"] != "logout" or transition["run"] != self._run:
                raise RuntimeError("logout transition is not owned")
            transition["phase"] = "logout_confirmed"
            record["protection"] = record["protection"] or "authentication_required"
            record["generation"] = uuid.uuid4().hex

        await self._commit(confirm)
        self._authentication_authorized = False

    async def clear_logged_out_key(self) -> None:
        def clear(record: dict[str, object]) -> None:
            transition = record["transition"]
            if (
                not isinstance(transition, dict)
                or transition["purpose"] != "logout"
                or transition["phase"] != "logout_confirmed"
            ):
                raise RuntimeError("logout has not been confirmed")
            scope = cast(dict[str, object], record["scope"])
            scope.update(stage="key_pending", key_id=None, account_id=None)
            transition["target_key"] = None

        await self._commit(clear)

    async def validate_authentication_account(self, account_id: int, *, finish: bool = False) -> bool:
        if type(account_id) is not int or account_id < 1:
            raise ValueError("invalid authentication account")

        accepted = False

        def validate(record: dict[str, object]) -> None:
            nonlocal accepted
            self.require_writable(auth_only=True)
            transition = record["transition"]
            if (
                not isinstance(transition, dict)
                or transition["purpose"] != "authentication"
                or transition["run"] != self._run
            ):
                raise RuntimeError("authentication transition is not owned")
            if transition["expected_account"] not in {None, account_id} or any(
                owner is not None and owner != account_id for owner in self._store.canonical_owners()
            ):
                self._authentication_authorized = False
                record["protection"] = "authentication_account_mismatch"
                record["generation"] = uuid.uuid4().hex
                return
            accepted = True
            transition["validated_account"] = account_id
            if finish:
                self._finish_authentication_binding(record, transition, account_id)

        await self._commit(validate)
        if finish and accepted:
            self._read_scope_valid = True
            self._authentication_authorized = False
            if self._startup_hold in {
                "new_key_conflicts_with_owned_state",
                "transport_transition_incomplete",
                "safety_auth_scope_mismatch",
                "safety_canonical_owner_mismatch",
            }:
                self._startup_hold = None
        return accepted

    @staticmethod
    def _finish_authentication_binding(
        record: dict[str, object], transition: dict[str, object], account_id: int
    ) -> None:
        scope = cast(dict[str, object], record["scope"])
        if transition["phase"] != "key_bound" or scope["key_id"] != transition["target_key"]:
            raise RuntimeError("authentication transport key is not bound")
        scope.update(stage="verified", account_id=account_id)
        record["transition"] = None
        if record["protection"] in {
            "authentication_required",
            "authentication_account_mismatch",
            "invalid_account_identity",
            "safety_auth_scope_mismatch",
            "safety_canonical_owner_mismatch",
        }:
            record["protection"] = None

    async def authorize_recovery(self, expected_generation: str, *, acknowledged: bool) -> str:
        if not acknowledged or self.verified_account_id is None or self._storage_failed:
            raise ValueError("recovery requires acknowledged verified safety scope")
        if self.protection_reason not in {
            "unresolved_outbound_outcome",
            "recovery_trial_incomplete",
            "recovery_trial_failed",
            "flood_wait_circuit_open",
            "transport_unavailable",
            "invalid_account_identity",
            "authentication_trial_failed",
            "automatic_resume_exhausted",
            "automatic_resume_budget_spent",
        }:
            raise ValueError("account safety recovery is not available")
        if self.floor_deadline > time.monotonic():
            raise ValueError("known Telegram restriction has not elapsed")
        grant = uuid.uuid4().hex

        def authorize(record: dict[str, object]) -> None:
            if self.floor_deadline > time.monotonic():
                raise ValueError("known Telegram restriction has not elapsed")
            if self.verified_account_id is None or self._storage_failed:
                raise ValueError("account recovery scope is not valid")
            if record["generation"] != expected_generation:
                raise ValueError("stale account protection generation")
            prior = record["recovery"]
            if (
                isinstance(prior, dict)
                and prior["state"] == "authorized"
                and prior["generation"] == record["generation"]
            ):
                raise ValueError("account recovery is already authorized")
            record["generation"] = grant
            record["recovery"] = {
                "generation": grant,
                "state": "authorized",
                "scope": dict(cast(dict[str, object], record["scope"])),
                "consumed_run": None,
                "acknowledged_protection": self.protection_reason,
            }

        await self._commit(authorize)
        return grant

    async def consume_recovery(self) -> bool:
        recovery = self._record.get("recovery")
        if not isinstance(recovery, dict) or "automatic" in recovery or recovery["state"] != "authorized":
            return False
        if self._storage_failed or self.verified_account_id is None or self.floor_deadline > time.monotonic():
            self.require_writable()

        def consume(record: dict[str, object]) -> None:
            if self._storage_failed or self.verified_account_id is None or self.floor_deadline > time.monotonic():
                raise ValueError("recovery cannot override a current restriction or invalid scope")
            trial = cast(dict[str, object], record["recovery"])
            if (
                trial["state"] != "authorized"
                or trial["scope"] != record["scope"]
                or trial["generation"] != record["generation"]
            ):
                raise ValueError("invalid recovery authorization")
            trial.update(state="consumed", consumed_run=self._run)
            record["generation"] = uuid.uuid4().hex
            for intent in cast(dict[str, dict[str, object]], record["intents"]).values():
                intent["phase"] = "acknowledged_unknown"
            for episode in cast(dict[str, dict[str, object]], record["methods"]).values():
                episode["claim"] = None
            record["protection"] = None

        await self._commit(consume)
        self._startup_hold = None
        return True

    def configure_auto_resume(self, *, enabled: bool) -> None:
        self._auto_resume_enabled = enabled

    def _automatic_recovery(self) -> dict[str, object] | None:
        recovery = self._record.get("recovery")
        return recovery if isinstance(recovery, dict) and "automatic" in recovery else None

    def _auto_resume_scope_safe(self, record: dict[str, object], *, allow_claimed: bool = False) -> bool:
        if self._storage_failed or self.verified_account_id is None:
            return False
        scope = cast(dict[str, object], record.get("scope", {}))
        allowed = {
            None,
            "unresolved_outbound_outcome",
            "recovery_trial_incomplete",
            "recovery_trial_failed",
            "flood_wait_circuit_open",
            "transport_unavailable",
        }
        return (
            (allow_claimed or not self._claimed)
            and self._startup_hold in allowed
            and record["protection"] in allowed
            and record["transition"] is None
            and all(owner in {None, scope["account_id"]} for owner in self._store.canonical_owners())
            and self._auto_resume_outcomes_settled(record)
        )

    @staticmethod
    def _auto_resume_outcomes_settled(record: dict[str, object]) -> bool:
        scope = cast(dict[str, object], record["scope"])
        return all(
            not _intent_blocks(item, scope)
            or (
                item["scope"] == {key: scope[key] for key in ("dc_id", "key_id", "account_id")}
                and rpc_effect(cast(str, item["method"])) is RpcEffect.NONMUTATING_READ
            )
            for item in cast(dict[str, dict[str, object]], record["intents"]).values()
        )

    def _schedule_auto_resume(
        self,
        record: dict[str, object],
        *,
        cause: RecoveryCause = RecoveryCause.FLOOD_WAIT,
        observed_flood: bool = True,
    ) -> None:
        previous = record["recovery"]
        automatic = previous.get("automatic") if isinstance(previous, dict) else None
        if isinstance(previous, dict) and previous.get("state") == "exhausted":
            return
        if isinstance(automatic, dict):
            assert isinstance(previous, dict)
            if not self._advance_auto_episode(record, previous, automatic, cause):
                return
        else:
            automatic = self._new_auto_episode(cause)
        generation = uuid.uuid4().hex
        record["generation"] = generation
        if self._exhaust_auto_if_needed(record, previous, automatic, cause, observed_flood):
            return
        record["protection"] = {
            RecoveryCause.FLOOD_WAIT: "flood_wait_circuit_open",
            RecoveryCause.TRANSIENT: "transport_unavailable",
            RecoveryCause.READ_UNCERTAINTY: "unresolved_outbound_outcome",
        }[cause]
        record["recovery"] = {
            "generation": generation,
            "state": "fencing",
            "scope": dict(cast(dict[str, object], record["scope"])),
            "consumed_run": None,
            "acknowledged_protection": None,
            "automatic": automatic,
        }

    @staticmethod
    def _exhaust_auto_if_needed(
        record: dict[str, object],
        previous: object,
        automatic: dict[str, object],
        cause: RecoveryCause,
        observed_flood: bool,
    ) -> bool:
        counts = cast(dict[str, int], automatic["attempts_by_policy"])
        if cause is not RecoveryCause.FLOOD_WAIT or counts[cause.value] < len(_AUTO_RESUME_DELAYS):
            return False
        assert isinstance(previous, dict)
        previous["state"] = "exhausted"
        record["protection"] = "automatic_resume_exhausted" if observed_flood else "automatic_resume_budget_spent"
        return True

    def _advance_auto_episode(
        self,
        record: dict[str, object],
        previous: dict[str, object],
        automatic: dict[str, object],
        cause: RecoveryCause,
    ) -> bool:
        if previous["state"] not in {"consumed", "succeeded"}:
            if cause is RecoveryCause.FLOOD_WAIT:
                self._strengthen_auto_flood(record, previous, automatic)
            return False
        stages = cast(dict[str, int], automatic["failure_stages"])
        family = cast(str, automatic["cause"])
        if previous["state"] == "consumed" and self._auto_resume_scope_safe(record, allow_claimed=True):
            stages[family] += 1
        automatic.update(cause=cause.value, quiet_anchor=None, bound_intent_ids=[])
        return True

    def _strengthen_auto_flood(
        self, record: dict[str, object], trial: dict[str, object], automatic: dict[str, object]
    ) -> None:
        cause = RecoveryCause.FLOOD_WAIT
        automatic["cause"] = cause.value
        if self._exhaust_auto_if_needed(record, trial, automatic, cause, True):
            return
        if trial["state"] == "waiting":
            due = cast(dict[str, object], automatic["scheduled_due"])
            stage = cast(dict[str, int], automatic["failure_stages"])[cause.value]
            delay = max(float(backoff_seconds(cause, stage)), self._floor_release_at(due) - time.monotonic())
            due.update(
                boot_id=self._boot_id,
                until_monotonic=time.monotonic() + delay,
                until_utc=time.time() + delay,
                remaining_seconds=delay,
            )

    @staticmethod
    def _new_auto_episode(cause: RecoveryCause) -> dict[str, object]:
        counts = {item.value: 0 for item in RecoveryCause}
        return {
            "episode_id": uuid.uuid4().hex,
            "policy_version": 1,
            "cause": cause.value,
            "attempts_total": 0,
            "attempts_by_policy": dict(counts),
            "failure_stages": dict(counts),
            "quiet_anchor": None,
            "scheduled_due": None,
            "bound_intent_ids": [],
            "last_transition_key": None,
        }

    async def maintain_auto_resume(self, *, cause: RecoveryCause | None = None, quiesced: bool = True) -> bool:
        if not self._auto_resume_enabled or not self._auto_resume_scope_safe(self._record, allow_claimed=not quiesced):
            return False
        if self._auto_resume_rebased:
            await self._commit(lambda _record: None)
            self._auto_resume_rebased = False
        recovery = self._record["recovery"]
        if isinstance(recovery, dict) and "automatic" not in recovery and recovery["state"] != "succeeded":
            return False
        if self._automatic_recovery() is None:
            await self._commit(lambda record: self._begin_auto_fencing(record, cause, quiesced))
        await self._advance_interrupted_auto(quiesced)
        return self.auto_resume_due()

    async def _advance_interrupted_auto(self, quiesced: bool) -> None:
        recovery = self._automatic_recovery()
        if recovery is not None and recovery["state"] == "consumed" and recovery["consumed_run"] != self._run:
            automatic = cast(dict[str, object], recovery["automatic"])
            interrupted_cause = RecoveryCause(cast(str, automatic["cause"]))
            await self._commit(
                lambda record: self._schedule_auto_resume(record, cause=interrupted_cause, observed_flood=False)
            )
        recovery = self._automatic_recovery()
        if recovery is not None and recovery["state"] == "fencing" and quiesced:
            await self._commit(self._settle_auto_fencing)

    def _begin_auto_fencing(self, record: dict[str, object], cause: RecoveryCause | None, quiesced: bool) -> None:
        if (
            self._auto_resume_enabled
            and self._auto_resume_scope_safe(record, allow_claimed=not quiesced)
            and self._automatic_recovery() is None
        ):
            selected = cause or (
                RecoveryCause.FLOOD_WAIT
                if self.protection_reason == "flood_wait_circuit_open"
                else RecoveryCause.READ_UNCERTAINTY
            )
            self._schedule_auto_resume(record, cause=selected)

    def _settle_auto_fencing(self, record: dict[str, object]) -> None:
        trial = cast(dict[str, object], record["recovery"])
        if trial["state"] != "fencing" or not self._auto_resume_scope_safe(record):
            return
        automatic = cast(dict[str, object], trial["automatic"])
        cause = RecoveryCause(cast(str, automatic["cause"]))
        stage = cast(dict[str, int], automatic["failure_stages"])[cause.value]
        delay = float(backoff_seconds(cause, stage))
        prior = automatic["scheduled_due"]
        if isinstance(prior, dict):
            delay = max(delay, self._floor_release_at(prior) - time.monotonic())
        now_mono, now_utc = time.monotonic(), time.time()
        automatic["quiet_anchor"] = {
            "boot_id": self._boot_id,
            "until_monotonic": now_mono,
            "until_utc": now_utc,
            "remaining_seconds": 0.0,
        }
        automatic["scheduled_due"] = {
            "boot_id": self._boot_id,
            "until_monotonic": now_mono + delay,
            "until_utc": now_utc + delay,
            "remaining_seconds": delay,
        }
        trial["state"] = "waiting"

    def auto_resume_due(self) -> bool:
        recovery = self._automatic_recovery()
        if recovery is None:
            return False
        if (
            not self._auto_resume_enabled
            or not self._auto_resume_scope_safe(self._record)
            or recovery["state"] != "waiting"
            or recovery["scope"] != self._record["scope"]
        ):
            return False
        automatic = cast(dict[str, object], recovery["automatic"])
        counts = cast(dict[str, int], automatic["attempts_by_policy"])
        if automatic["cause"] == RecoveryCause.FLOOD_WAIT.value and counts[RecoveryCause.FLOOD_WAIT.value] >= len(
            _AUTO_RESUME_DELAYS
        ):
            return False
        return (
            max(self.floor_deadline, self._floor_release_at(cast(dict[str, object], automatic["scheduled_due"])))
            <= time.monotonic()
        )

    async def consume_auto_resume(self) -> bool:
        if self._auto_resume_rebased:
            await self._commit(lambda _record: None)
            self._auto_resume_rebased = False
        if not self.auto_resume_due():
            return False
        consumed = False

        def consume(record: dict[str, object]) -> None:
            nonlocal consumed
            if not self.auto_resume_due():
                return
            recovery = cast(dict[str, object], record["recovery"])
            automatic = cast(dict[str, object], recovery["automatic"])
            counts = cast(dict[str, int], automatic["attempts_by_policy"])
            cause = cast(str, automatic["cause"])
            counts[cause] += 1
            automatic["attempts_total"] = cast(int, automatic["attempts_total"]) + 1
            grant = cast(str, recovery["generation"])
            bound = []
            for intent_id, intent in cast(dict[str, dict[str, object]], record["intents"]).items():
                if _intent_blocks(intent, cast(dict[str, object], record["scope"])):
                    intent["phase"] = "unknown"
                    intent["policy_recovery"] = {
                        "policy_version": 1,
                        "episode_id": automatic["episode_id"],
                        "grant_generation": grant,
                        "scope": dict(cast(dict[str, object], intent["scope"])),
                    }
                    bound.append(intent_id)
            automatic["bound_intent_ids"] = bound
            for episode in cast(dict[str, dict[str, object]], record["methods"]).values():
                episode["claim"] = None
            recovery.update(state="consumed", consumed_run=self._run)
            record["generation"] = uuid.uuid4().hex
            record["protection"] = None
            consumed = True

        await self._commit(consume)
        if consumed:
            self._startup_hold = None
        return consumed

    async def reset_auto_resume(self) -> None:
        def reset(record: dict[str, object]) -> None:
            recovery = self._automatic_recovery()
            if recovery is None:
                return
            if (
                self._auto_resume_enabled
                and recovery["state"] == "succeeded"
                and self._auto_resume_scope_safe(record)
                and self.protection_reason is None
                and self.floor_deadline <= time.monotonic()
            ):
                record["recovery"] = None

        await self._commit(reset)

    def auto_resume_status(self) -> dict[str, object]:
        recovery = self._automatic_recovery()
        automatic = cast(dict[str, object], recovery["automatic"]) if recovery is not None else None
        counts = (
            cast(dict[str, int], automatic["attempts_by_policy"])
            if automatic
            else {item.value: 0 for item in RecoveryCause}
        )
        state, eligible, phase, reason, due = self._auto_resume_projection(recovery, automatic)
        return {
            "enabled": self._auto_resume_enabled,
            "state": state,
            "eligible": eligible,
            "phase": phase,
            "cause": automatic["cause"] if automatic else None,
            "block_reason": reason,
            "attempts_total": automatic["attempts_total"] if automatic else 0,
            "attempts_by_policy": counts,
            "attempts": counts[RecoveryCause.FLOOD_WAIT.value],
            "max_attempts": len(_AUTO_RESUME_DELAYS),
            "next_attempt_at_utc": due,
            "next_trial_at_utc": due,
            "known_restriction_until_utc": time.time() + self.floor_deadline - time.monotonic()
            if time.monotonic() < self.floor_deadline < math.inf
            else None,
        }

    def _auto_resume_projection(
        self, recovery: dict[str, object] | None, automatic: dict[str, object] | None
    ) -> tuple[str, bool, str | None, str | None, float | None]:
        state = "manual"
        due = None
        eligible = self._auto_resume_enabled and self._auto_resume_scope_safe(self._record)
        phase = cast(str, recovery["state"]) if recovery is not None else None
        reason = None if eligible else (self.protection_reason or "unsafe_scope_or_outcome")
        if phase == "exhausted":
            eligible = False
            reason = self._auto_resume_exhaustion_reason()
        if not self._auto_resume_enabled:
            state = "disabled"
        elif recovery is not None:
            state = {"waiting": "waiting", "consumed": "trial", "succeeded": "stability"}.get(
                cast(str, recovery["state"]), "manual"
            )
            if state == "waiting":
                state, due = self._auto_resume_waiting_projection(eligible, automatic)
        return state, eligible, phase, reason, due

    def _auto_resume_waiting_projection(
        self, eligible: bool, automatic: dict[str, object] | None
    ) -> tuple[str, float | None]:
        if not eligible:
            return "manual", None
        assert automatic is not None
        deadline = max(self.floor_deadline, self._floor_release_at(cast(dict[str, object], automatic["scheduled_due"])))
        return "waiting", time.time() + max(0.0, deadline - time.monotonic())

    def _auto_resume_exhaustion_reason(self) -> str:
        return (
            "flood_budget_spent" if self._record["protection"] == "automatic_resume_budget_spent" else "flood_exhausted"
        )

    async def finish_recovery(self, account_id: int) -> None:
        def finish(record: dict[str, object]) -> None:
            if self.protection_reason is not None or self._unresolved() or self.floor_deadline > time.monotonic():
                raise ValueError("recovery remains restricted or uncertain")
            trial = record["recovery"]
            scope = cast(dict[str, object], record["scope"])
            if (
                not isinstance(trial, dict)
                or trial["state"] != "consumed"
                or trial["consumed_run"] != self._run
                or scope["account_id"] != account_id
            ):
                raise ValueError("recovery trial identity is not current")
            trial["state"] = "succeeded"
            record["protection"] = None

        await self._commit(finish)

    async def fail_recovery(self) -> None:
        def fail(record: dict[str, object]) -> None:
            trial = record["recovery"]
            if isinstance(trial, dict) and trial["state"] == "consumed" and trial["consumed_run"] == self._run:
                trial["state"] = "failed"
                record["protection"] = "recovery_trial_failed"
                record["generation"] = uuid.uuid4().hex

        await self._commit(fail)


_COOLDOWN_DEADLINE = 0.0
_OBSERVED_FLOOD_IDS: set[int] = set()


def account_cooldown_deadline() -> float:
    """Return the current process-wide monotonic cooldown deadline."""
    return _COOLDOWN_DEADLINE


def _validate_cooldown_until_utc(deadline_utc: float) -> float:
    if (
        isinstance(deadline_utc, bool)
        or not isinstance(deadline_utc, (int, float))
        or not math.isfinite(deadline_utc)
        or deadline_utc < 0
    ):
        raise ValueError("persisted Telegram RPC cooldown deadline must be a finite UTC timestamp")
    return float(deadline_utc)


def _cooldown_until_utc_ms() -> int:
    """Project the process cooldown's monotonic deadline onto the UTC clock."""
    return int((time.time() + max(0.0, _COOLDOWN_DEADLINE - time.monotonic())) * 1_000)


@dataclass(slots=True)
class _ActiveDelivery:
    envelope: DeliveryEnvelope
    contexts: dict[int, DeliveredUpdate]
    remaining: set[int]
    done: asyncio.Event


@dataclass(slots=True, eq=False)
class _PhotoWireCapability:
    issuer: object
    scope: TelegramAuthScope
    owner_task: asyncio.Task[object]
    request_body: bytes
    budget: PhotoBudget
    source_fence: PhotoSourceFence | None
    setup: bool
    revoked: bool = False
    lease: _AuxPhotoLease | None = None
    use_auxiliary: bool = False
    aux_bootstrap: dict[str, object] | None = None
    wire_owner: _WireAttempt | None = None
    photo_session: _GuardedPhotoSession | None = None


_PHOTO_CAPABILITY: ContextVar[_PhotoWireCapability | None] = ContextVar("telegram_photo_capability", default=None)


@dataclass(slots=True, eq=False)
class _AuxPhotoLease:
    gate: TelegramRpcGate
    scope: TelegramAuthScope
    owner_task: asyncio.Task[object]
    rpc_scope: TelegramRpcScope
    budget: PhotoBudget
    source_fence: PhotoSourceFence
    dc_id: int
    lease_id: str
    work: PhotoWork
    locator: PhotoLocator
    sender: GuardedMTProtoSender | None = None
    revoked: bool = False
    control_budget: RpcAttemptBudget = field(default_factory=lambda: RpcAttemptBudget(PHOTO_MAX_LEASE_CONTROLS))


@dataclass(slots=True, eq=False)
class _WireAttempt:
    issuer: object
    attempt_id: str
    method: str
    scope: TelegramRpcScope
    claimed: int = 0
    dispatch_at: float | None = None
    incident_claimed_at_ms: int | None = None
    incident_recorded: bool = False
    admission: RpcAdmission | None = None
    receipt: asyncio.Task[None] | None = None
    terminal: bool = False
    self_lookup: bool = False
    revoked: bool = False
    preparing_task: asyncio.Task[object] | None = None
    timing: DaemonRequestTiming | None = None
    method_generation: str | None = None
    observed_write_returns: int = 0
    observed_result: bool = False
    response_timer: asyncio.Timeout | None = None
    observation_ordinal: int | None = None
    draft_mutation: bool = False
    scheduled_check: ScheduledMutationCheck | None = None
    scheduled_mutation: bool = False
    photo_capability: _PhotoWireCapability | None = None
    photo_lease: _AuxPhotoLease | None = None


def _application_request(request: object) -> TLRequest:
    wrappers = (
        functions.InvokeWithLayerRequest,
        functions.InitConnectionRequest,
        functions.InvokeWithoutUpdatesRequest,
        functions.InvokeAfterMsgRequest,
        functions.InvokeAfterMsgsRequest,
    )
    for _ in range(4):
        if type(request) not in wrappers:
            break
        request = getattr(request, "query", None)
    if isinstance(getattr(request, "query", None), TLRequest):
        raise RuntimeError("unsupported nested Telegram RPC wrapper")
    kind = type(request)
    constructor = getattr(kind, "CONSTRUCTOR_ID", None)
    if (
        not isinstance(request, TLRequest)
        or type(constructor) is not int
        or not kind.__module__.startswith("telethon.tl.functions")
    ):
        raise RuntimeError("unclassified Telegram application method")
    return request


def _canonical_wire_method(request: object) -> str:
    actual = _application_request(request)
    kind = type(actual)
    return f"{kind.__module__}.{kind.__name__}:{actual.CONSTRUCTOR_ID:08x}"


def _safe_diagnostic_method(method: str) -> str:
    """Only disclose names matching installed Telegram request constructors."""
    _, _, constructor = method.rpartition(":")
    if len(constructor) != _TL_CONSTRUCTOR_HEX_CHARS or any(char not in "0123456789abcdef" for char in constructor):
        return "unrecognized"
    kind = tlobjects.get(int(constructor, 16))
    if kind is not None and issubclass(kind, TLRequest):
        canonical = f"{kind.__module__}.{kind.__name__}:{kind.CONSTRUCTOR_ID:08x}"
        if method == canonical:
            return canonical
    return "unrecognized"


@dataclass(slots=True)
class _WirePermit:
    owner: _WireAttempt
    admission: RpcAdmission
    generation: tuple[int, int]
    method_generation: str | None = None
    scheduled_token: ScheduledMutationToken | None = None
    released: bool = False
    photo_token: PhotoSourceToken | None = None
    auxiliary_generation: int | None = None


class _AuxPhotoAuthority:
    """Bind the existing physical authority to one gate-owned auxiliary sender."""

    def __init__(self, lease: _AuxPhotoLease) -> None:
        self._lease = lease
        self._gate = lease.gate

    def register_application(self, request: object, body: bytes, scope: TelegramRpcScope) -> object:
        owner = self._gate._require_wire_owner(self._gate.register_application(request, body, scope))
        owner.photo_lease = self._lease
        return owner

    def register_protocol(self, request: object, body: bytes, origin: ProtocolOrigin) -> object:
        return self._gate._register_aux_photo_protocol(self._lease, request, body, origin)

    async def prepare_write(self, owner: object) -> object:
        return await self._gate.prepare_write(owner)

    def claim_write(self, owner: object, permit: object) -> bool:
        return self._gate.claim_write(owner, permit)

    def observe_write_return(self, owner: object) -> None:
        self._gate.observe_write_return(owner)

    def defer_write(self, owner: object, permit: object) -> None:
        self._gate.defer_write(owner, permit)

    async def observe_result(
        self, owner: object, error: BaseException | None, result: object = None
    ) -> BaseException | None:
        return await self._gate.observe_result(owner, error, result)

    def observe_error_now(self, owner: object, error: BaseException) -> None:
        self._gate.observe_error_now(owner, error)

    def observe_response_now(self, owner: object) -> None:
        self._gate.observe_response_now(owner)

    def protect_transport(self, error: BaseException) -> None:
        self._lease.revoked = True
        if not isinstance(error, (TransportConnectError, TransportWriteError)):
            self._gate.protect_transport(error)

    def transport_terminated(self, error: BaseException | None) -> BaseException | None:
        self._lease.revoked = True
        return error


class _GuardedPhotoSession:
    """Private, one-owner capability passed only to the pinned photo iterator."""

    def __init__(  # noqa: PLR0913, PLR0917 - the capability binds these separate owner facts.
        self,
        gate: TelegramRpcGate,
        locator: PhotoLocator,
        scope: TelegramAuthScope,
        work: PhotoWork,
        budget: PhotoBudget,
        source_fence: PhotoSourceFence,
        owner_task: asyncio.Task[object],
    ) -> None:
        self.gate = gate
        self.locator = locator
        self.scope = scope
        self.work = work
        self.budget = budget
        self.source_fence = source_fence
        self.owner_task = owner_task
        self.dc_id = locator.dc_id
        self.redirected = False
        self.closed = False
        self.slicing = False
        self._pending_migration: int | None = None
        self.slice_reservations = 0
        self.slice_evidence: RpcAttemptEvidence | None = None
        self._close_task: asyncio.Task[None] | None = None

    def _require_owner(self) -> None:
        if self.closed or asyncio.current_task() is not self.owner_task or self.gate._photo_session is not self:
            raise UnclassifiedTelegramRpcError("photo session is no longer owned")

    async def chunks(self, *, offset: int) -> AsyncIterator[bytes]:
        self._require_owner()
        if self.slicing:
            raise RuntimeError("photo slice is already running")
        self.slicing = True
        evidence = current_demand_token().attempt_evidence
        if self.slice_evidence is not evidence:
            self.slice_reservations = 0
            self.slice_evidence = evidence
        try:
            await self.gate._ensure_photo_transport(self)
            async for chunk in iter_photo_chunks(self.locator, offset=offset, dispatch=self, log=self.gate._log):
                yield chunk
        finally:
            self._pending_migration = None
            self.slicing = False

    async def get_file(self, request: functions.upload.GetFileRequest) -> object:
        self._require_owner()
        if not self.slicing:
            raise RuntimeError("photo file dispatch requires an active slice")
        self._pending_migration = None
        try:
            return await self.gate._photo_file_request(self, request)
        except FileMigrateError as exc:
            self._pending_migration = exc.new_dc
            raise

    async def migrate(self, dc_id: int) -> None:
        self._require_owner()
        if (
            not self.slicing
            or self.redirected
            or type(dc_id) is not int
            or dc_id <= 0
            or self._pending_migration != dc_id
        ):
            raise RuntimeError("photo migration is unavailable")
        self._pending_migration = None
        await self.budget.reserve_redirect(dc_id)
        self.redirected = True
        await self.gate._close_photo_aux_lease()
        self.dc_id = dc_id
        await self.gate._ensure_photo_transport(self)

    async def close(self) -> None:
        if self._close_task is None:
            self._require_owner()
            self.closed = True
            self._close_task = asyncio.create_task(self._close_owned(), context=Context())
        cancelled = await self.gate._join_vendor_disconnect(self._close_task)
        if cancelled:
            raise asyncio.CancelledError

    async def _close_owned(self) -> None:
        await self.gate._close_photo_aux_lease()
        if self.gate._photo_session is self:
            self.gate._photo_session = None


class TelegramRpcGate(TelegramClient):
    """A Telethon client with account-global admission and transient retry.

    Every admitted attempt consumes one limiter acquisition. Flood errors
    extend the account cooldown atomically and are re-raised immediately;
    application retry is limited to the configured server-transient taxonomy.
    An RPC already admitted or in flight may finish after a later FloodWait is
    observed: this gate makes no stronger claim about cancellation of work.
    """

    _catch_up: bool
    _updates_handle: asyncio.Task[object] | None

    def __init__(  # noqa: PLR0913, PLR0915 - Telethon constructor plus explicit account policy
        self,
        *args: object,
        rpc_budget: TelegramRpcBudget,
        circuit_status: Callable[[], CircuitStatus],
        fallback_wait_seconds: int,
        cooldown_buffer_seconds: float,
        transient_retry_delays_seconds: tuple[float, ...],
        scheduler_policy: TelegramRpcSchedulerPolicy,
        safety_store: TelegramRpcSafetyStore,
        admission_observer: AdmissionObserver | None = None,
        rpc_request_observer: Callable[..., None] | None = None,
        flood_observer: Callable[..., None] | None = None,
        **kwargs: object,
    ) -> None:
        requested_connection = kwargs.pop("connection", GuardedConnectionTcpFull)
        if requested_connection is not GuardedConnectionTcpFull:
            raise ValueError("only the owned Telegram connection is supported")
        kwargs["connection"] = GuardedConnectionTcpFull
        kwargs["request_retries"] = 0
        kwargs["flood_sleep_threshold"] = 0
        kwargs["raise_last_call_error"] = True
        kwargs.setdefault("auto_reconnect", True)
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._reconnect_event = asyncio.Event()
        if fallback_wait_seconds < 1:
            raise ValueError("fallback_wait_seconds must be >= 1")
        if cooldown_buffer_seconds < 0:
            raise ValueError("cooldown_buffer_seconds must be >= 0")
        if any(delay < 0 for delay in transient_retry_delays_seconds):
            raise ValueError("transient retry delays must be >= 0")
        self._rpc_circuit_status = circuit_status
        self._fallback_wait_seconds = fallback_wait_seconds
        self._cooldown_buffer_seconds = cooldown_buffer_seconds
        self._transient_retry_delays = transient_retry_delays_seconds
        self._flood_observer = flood_observer
        self._flood_event_observer: Callable[[FloodWaitObservation], None] | None = None
        self._rpc_request_observer = rpc_request_observer
        self._initialize_wire_authority(safety_store)
        self._scheduler_policy = scheduler_policy
        self._limiter = AsyncLimiter(rpc_budget.max_calls_per_period, rpc_budget.period_seconds)
        self._admission_scheduler = TelegramRpcAdmissionScheduler(
            policy=self._scheduler_policy,
            limiter=self._limiter,
            minimum_spacing_seconds=rpc_budget.period_seconds / rpc_budget.max_calls_per_period,
            release_at=self._wire_release_at,
            deferral=self._difference_admission_deferral,
            capacity_released=self._notify_rpc_capacity_released,
            observer=admission_observer,
            readiness=RpcTransportReadiness(
                probe=self._scheduler_transport_ready,
                wait=self._wait_for_scheduler_transport,
            ),
        )
        unused_sender = cast(_MainSender, object.__getattribute__(self, "_sender"))
        self._main_sender = cast(
            _MainSender,
            GuardedMTProtoSender(
                unused_sender.auth_key,
                authority=self,
                identity_change=self._image_identity_change,
                loggers=self._log,
                retries=self._connection_retries,
                delay=self._retry_delay,
                auto_reconnect=self._auto_reconnect,
                connect_timeout=self._timeout,
                auth_key_callback=self._auth_key_callback,
                updates_queue=self._updates_queue,
                auto_reconnect_callback=self._handle_auto_reconnect,
            ),
        )
        self._sender = _MainSenderAdapter(self)
        self._connect_owner: asyncio.Task[object] | object | None = None
        self._connection_capability: _ConnectionCapability | None = None
        self._connection_retry_delays: Iterator[float] | None = None
        self._connection_mutation_started = False
        self._connection_update_loop: asyncio.Task[object] | None = None
        self._connection_rpc_tasks: set[asyncio.Task[object]] = set()
        self._transport_state = _TransportBoundaryState.DISCONNECTED
        self._delivery_repository: UpdateDeliveryRepository | None = None
        self._delivery_closed = False
        self._delivery_ready = asyncio.Event()
        self._delivery_failure = asyncio.Event()
        self._delivery_safe: ProtocolSnapshot | None = None
        self._delivery_account_id: int | None = None
        self._active_delivery: _ActiveDelivery | None = None
        self._delivery_callbacks: dict[object, object] = {}
        self._scheduled_continuity_recovery: Callable[[int | None, str], Awaitable[None]] | None = None

    def set_scheduled_continuity_recovery(self, callback: Callable[[int | None, str], Awaitable[None]] | None) -> None:
        """Bind the durable owner for Telegram's explicit DifferenceTooLong result."""
        self._scheduled_continuity_recovery = callback

    def _initialize_wire_authority(self, safety_store: TelegramRpcSafetyStore) -> None:
        """Initialize one account-owned physical-attempt authority."""
        self._safety_store = safety_store
        self._safety: TelegramRpcSafetyLedger | None = None
        self._wire_issuer = object()
        self._image_scope_cell = ScopeCell()
        self._photo_issuer = object()
        self._session_lease: TelegramSessionLease | None = None
        self._session_pin: TelegramSessionLeasePin | None = None
        self._photo_aux_lease: _AuxPhotoLease | None = None
        self._photo_session: _GuardedPhotoSession | None = None
        self._photo_locators: dict[int, tuple[PhotoLocator, ScopeToken]] = {}
        self._photo_wire_fault_owner: str | None = None
        self._vendor_call_tasks: set[asyncio.Task[object]] = set()
        self._dc_transition_target: int | None = None
        self._owned_disconnect: asyncio.Task[None] | None = None
        self._scheduler_shutdown: asyncio.Task[None] | None = None
        self._main_disconnect: asyncio.Task[object] | None = None
        self._reconnect_probe: asyncio.Task[None] | None = None
        self._reconnect_wire_owner: _WireAttempt | None = None
        self._reconnect_quiescing = False
        self._authentication_mode = False
        self._logout_mode = False
        self._authentication_token: DemandToken | None = None
        self._wire_generation = 0
        self._connection_budgets: WeakKeyDictionary[RpcAttemptEvidence, RpcAttemptBudget] = WeakKeyDictionary()
        self._wire_budgets: WeakKeyDictionary[RpcAttemptEvidence, RpcAttemptBudget] = WeakKeyDictionary()
        self._wire_fault: str | None = None
        self._recovery_cause: RecoveryCause | None = None
        self._auto_resume_stable_since: float | None = None
        self._auto_resume_acquired = False
        self._auto_resume_progress = False
        self._auto_resume_self_claimed = False
        self._auto_resume_identity_owner: asyncio.Task[object] | None = None
        self._startup_safety_prepared = False
        self._auto_resume_stopped: Callable[[], bool] = lambda: False
        self._pending_method_receipts: dict[str, int] = {}
        self._rpc_eligibility_observer: Callable[[str | None], None] | None = None
        self._update_loop_owner: asyncio.Task[object] | None = None
        previous = cast(MessageBox, object.__getattribute__(self, "_message_box"))
        self._message_box = GateMessageBox(previous, self.rpc_eligibility, _canonical_wire_method)
        if self._offline_wire_identity()[0] > 0:
            self._ensure_safety_ledger()
        self._cooldown_save_tasks: dict[int, asyncio.Task[None]] = {}

    def configure_update_delivery(self, repository: UpdateDeliveryRepository) -> None:
        """Hold the vendor loop before connect; auth RPC remains available."""
        if self._delivery_repository is not None or self.is_connected():
            raise RuntimeError("update delivery must be configured once before connect")
        self._delivery_repository = repository

    def close_update_delivery(self) -> None:
        """Fence shutdown before callbacks detach; admitted SQLite writes still settle."""
        self._delivery_closed = True
        self._delivery_ready.clear()
        active = self._active_delivery
        if active is not None:
            for context in active.contexts.values():
                context.failed = True
            active.done.set()

    def _require_delivery_open(self) -> None:
        if self._delivery_closed:
            raise asyncio.CancelledError

    async def wait_update_delivery_failure(self) -> None:
        await self._delivery_failure.wait()
        raise RuntimeError("required Telegram update delivery failed; retained package needs recovery")

    def add_event_handler(self, callback: Callable[..., Awaitable[object]], event: object = None) -> None:
        @wraps(callback)
        async def retained_callback(*args: object) -> object:
            try:
                return await callback(*args)
            except BaseException:
                fail_delivered_update()
                raise

        self._delivery_callbacks[callback] = retained_callback
        super().add_event_handler(retained_callback, cast(EventBuilder, event))

    def remove_event_handler(self, callback: object, event: object = None) -> int:
        return cast(
            int,
            super().remove_event_handler(
                cast(Callable[..., Awaitable[object]], self._delivery_callbacks.pop(callback, callback)),
                cast(EventBuilder, event),
            ),
        )

    def _capture_delivery_snapshot(self, chats: list[object]) -> ProtocolSnapshot:
        account, channels = self._message_box.session_state()
        hashes = {}
        supplied = {getattr(chat, "id", None): chat for chat in chats}
        for channel_id in channels:
            entity = self._mb_entity_cache.get(channel_id)
            chat = supplied.get(channel_id)
            access_hash = None if getattr(chat, "min", False) else getattr(chat, "access_hash", None)
            if access_hash is None:
                access_hash = getattr(entity, "hash", None)
            if access_hash is not None:
                hashes[channel_id] = access_hash
        return ProtocolSnapshot(
            pts=cast(int, account["pts"]),
            qts=cast(int, account["qts"]),
            date=cast(datetime, account["date"]),
            seq=cast(int, account["seq"]),
            channels=channels,
            channel_hashes=hashes,
        )

    async def start_update_delivery(self, account_id: int) -> None:
        """Replay local obligations after domain owners/account validation, then release intake."""
        self._require_delivery_open()
        repository = self._delivery_repository
        if repository is None:
            raise RuntimeError("update delivery repository is not configured")
        state = await repository.bind(account_id, self._capture_delivery_snapshot([]).model_dump_json())
        self._delivery_account_id = account_id
        self._delivery_safe = load_protocol_snapshot(state.safe_snapshot)
        if state.pending is not None:
            envelope = load_delivery_envelope(state.pending)
            updates, users, chats = envelope.unpack()
            if len(updates) != len(state.orders) or len(state.orders) != len(state.reaction_sequences):
                raise RuntimeError("retained Telegram update tokens are incomplete")
            self._install_delivery(envelope, updates, tuple(zip(state.orders, state.reaction_sequences, strict=True)))
            await super()._preprocess_updates(updates, users, chats)  # type: ignore[misc]
            if updates:
                await asyncio.gather(*(self._dispatch_update(update) for update in updates))
            else:
                await self._complete_update_delivery()
            if self._delivery_failure.is_set():
                raise RuntimeError("retained Telegram update replay failed")
        self._require_delivery_open()
        assert self._delivery_safe is not None
        self._restore_delivery_message_box(account_id)
        await self._save_states_and_entities()
        assert self.session is not None
        self.session.save()
        self._delivery_ready.set()

    def _restore_delivery_message_box(self, account_id: int) -> None:
        snapshot = self._delivery_safe
        assert snapshot is not None and self.session is not None
        for channel_id, access_hash in snapshot.channel_hashes.items():
            self.session.process_entities(
                types.contacts.ResolvedPeer(
                    types.PeerChannel(channel_id), [types.ChannelForbidden(channel_id, access_hash, "")], []
                )
            )
        self._message_box.load(
            SessionState(
                account_id,
                self.session.dc_id,
                bool(self._mb_entity_cache.self_bot),
                snapshot.pts,
                snapshot.qts,
                int(snapshot.date.timestamp()),
                snapshot.seq,
                None,
            ),
            [ChannelState(channel_id, pts) for channel_id, pts in snapshot.channels.items()],
        )
        for channel_id in snapshot.channels:
            peer = self.session.get_input_entity(types.PeerChannel(channel_id))
            # MessageBox needs the access hash in its own cache before channel difference.
            self._mb_entity_cache.extend(
                [],
                [
                    types.Channel(
                        id=channel_id,
                        title="",
                        photo=types.ChatPhotoEmpty(),
                        date=None,
                        access_hash=peer.access_hash,
                    )
                ],
            )

    def _install_delivery(
        self, envelope: DeliveryEnvelope, updates: list[object], tokens: tuple[tuple[int, int], ...]
    ) -> None:
        contexts = {
            id(update): DeliveredUpdate(order, envelope.captured_at, sequence)
            for update, (order, sequence) in zip(updates, tokens, strict=True)
        }
        self._active_delivery = _ActiveDelivery(envelope, contexts, set(contexts), asyncio.Event())

    async def _preprocess_updates(
        self, updates: list[object], users: list[object], chats: list[object]
    ) -> list[object]:
        if self._authentication_mode:
            return self._authentication_token_updates(updates)
        repository = self._delivery_repository
        if repository is None:
            return cast(list[object], await super()._preprocess_updates(updates, users, chats))
        self._require_delivery_open()
        # Capture before either await: the vendor head can change while the prior callbacks finish.
        envelope = DeliveryEnvelope.capture(self._capture_delivery_snapshot(chats), time.time(), updates, users, chats)
        prior = self._active_delivery
        if prior is not None:
            await prior.done.wait()
        self._require_delivery_open()
        if self._delivery_failure.is_set() or not self._delivery_ready.is_set():
            raise RuntimeError("Telegram update admission is closed")
        try:
            tokens = await repository.admit(envelope.model_dump_json().encode("utf-8"), len(updates))
            self._install_delivery(envelope, updates, tokens)
            result = await super()._preprocess_updates(updates, users, chats)  # type: ignore[misc]
            if not updates:
                await self._complete_update_delivery()
            return cast(list[object], result)
        except BaseException:
            self._delivery_failure.set()
            raise

    @staticmethod
    def _authentication_token_updates(updates: list[object]) -> list[object]:
        selected: list[object] = [update for update in updates if type(update) is types.UpdateLoginToken]
        for update in selected:
            vars(update)["_entities"] = {}
        return selected

    async def _complete_update_delivery(self) -> None:
        active = self._active_delivery
        repository = self._delivery_repository
        assert active is not None and repository is not None and self.session is not None
        try:
            if self._delivery_closed:
                return
            if any(context.failed for context in active.contexts.values()):
                self._delivery_failure.set()
                return
            snapshot = active.envelope.checkpoint_after
            for channel_id, access_hash in snapshot.channel_hashes.items():
                try:
                    peer = self.session.get_input_entity(types.PeerChannel(channel_id))
                except ValueError:
                    peer = None
                if getattr(peer, "access_hash", None) == access_hash:
                    continue
                self.session.process_entities(
                    types.contacts.ResolvedPeer(
                        types.PeerChannel(channel_id), [types.ChannelForbidden(channel_id, access_hash, "")], []
                    )
                )
            for channel_id in snapshot.channels:
                self.session.get_input_entity(types.PeerChannel(channel_id))
            # Entity/access-hash durability precedes authoritative completion across the two databases.
            self.session.save()
            await repository.complete(snapshot.model_dump_json())
            self._delivery_safe = snapshot
            self._active_delivery = None
        except BaseException:
            self._delivery_failure.set()
            raise
        finally:
            active.done.set()

    async def _save_states_and_entities(self) -> None:
        if self._authentication_mode or self._logout_mode:
            return
        if self.session is None:
            return
        if self._delivery_repository is None:
            await super()._save_states_and_entities()  # type: ignore[misc]
            return
        if self._mb_entity_cache.self_id:
            self.session.process_entities(
                types.contacts.ResolvedPeer(
                    types.PeerUser(0), [], [types.User(0, access_hash=self._mb_entity_cache.self_id)]
                )
            )
        snapshot = self._delivery_safe
        if snapshot is None:
            return
        self.session.set_update_state(
            0, types.updates.State(snapshot.pts, snapshot.qts, snapshot.date, snapshot.seq, 0)
        )
        for channel_id, pts in snapshot.channels.items():
            self.session.set_update_state(channel_id, types.updates.State(pts, 0, snapshot.date, 0, 0))

    @staticmethod
    def rpc_scope(
        source: TelegramRpcSource,
        *,
        deadline: float | None = None,
        timeout_seconds: float | None = None,
        response_timeout_seconds: float | None = None,
        acquisition_kind: AcquisitionKind | None = None,
    ) -> AbstractContextManager[TelegramRpcScope]:
        """Return a source scope for application helpers using this client."""
        return rpc_scope(
            source,
            deadline=deadline,
            timeout_seconds=timeout_seconds,
            response_timeout_seconds=response_timeout_seconds,
            acquisition_kind=acquisition_kind,
        )

    def _close_owned_session(self) -> None:
        if self.session is not None:
            self.session.close()

    @staticmethod
    async def _join_vendor_disconnect(teardown: asyncio.Future[None]) -> bool:
        cancelled = False
        while not teardown.done():
            try:
                await asyncio.wait({teardown})
            except asyncio.CancelledError:
                cancelled = True
        teardown.result()
        return cancelled

    async def close_rpc_scheduler(self) -> None:
        """Join all physical and persistence owners before releasing storage."""
        self._reconnect_quiescing = True
        if self._scheduler_shutdown is None:
            self._scheduler_shutdown = asyncio.create_task(self._close_rpc_scheduler_owned(), context=Context())
        cancelled = False
        while not self._scheduler_shutdown.done():
            try:
                await asyncio.wait({self._scheduler_shutdown})
            except asyncio.CancelledError:
                cancelled = True
        self._scheduler_shutdown.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _close_rpc_scheduler_owned(self) -> None:
        operations = [
            self._quiesce_reconnect_probe,
            self._shutdown_photo_session,
            self._disconnect_main_sender,
            self._admission_scheduler.close,
            self._main_sender.settle_shutdown,
            self._drain_cooldown_saves,
        ]
        if self._safety is not None:
            operations.append(self._safety.drain)
        primary: BaseException | None = None
        for operation in operations:
            try:
                await operation()
            except BaseException as error:  # noqa: BLE001 - finish every ownership barrier before re-raising.
                primary = primary or error
        try:
            if self.session is not None:
                self.session.close()
        except Exception as error:  # noqa: BLE001 - retain the original teardown failure.
            primary = primary or error
        if primary is not None:
            raise primary
        self._release_session_pin()

    def _release_session_pin(self) -> None:
        if self._photo_aux_lease is not None:
            raise RuntimeError("photo auxiliary lease remains uncontained")
        if self._session_pin is not None:
            self._session_pin._release()
            self._session_pin = None

    async def _shutdown_photo_session(self) -> None:
        session = self._photo_session
        if session is None:
            await self._close_photo_aux_lease()
            return
        session.closed = True
        if session._close_task is None:
            session._close_task = asyncio.create_task(session._close_owned(), context=Context())
        await self._join_vendor_disconnect(session._close_task)

    async def _drain_cooldown_saves(self) -> None:
        """Keep durable FloodWait receipts owned until shutdown can close storage."""
        cancelled = False
        while self._cooldown_save_tasks:
            batch = dict(self._cooldown_save_tasks)
            receipts = asyncio.gather(*batch.values())
            while not receipts.done():
                try:
                    await asyncio.wait({receipts})
                except asyncio.CancelledError:
                    cancelled = True
            for key, task in batch.items():
                if self._cooldown_save_tasks.get(key) is task:
                    self._cooldown_save_tasks.pop(key)
            receipts.result()
        if cancelled:
            raise asyncio.CancelledError

    def set_rpc_admission_observer(self, observer: AdmissionObserver | None) -> None:
        """Attach the daemon-owned operational telemetry sink."""
        self._admission_scheduler.set_observer(observer)

    @staticmethod
    def rpc_observation_methods() -> frozenset[str]:
        """Supply content-free constructor names from the existing vendor owner."""
        return frozenset(
            f"{kind.__module__}.{kind.__name__}:{kind.CONSTRUCTOR_ID:08x}"
            for kind in tlobjects.values()
            if issubclass(kind, TLRequest) or kind in {types.MsgsAck, types.MsgsStateInfo}
        )

    def set_rpc_request_observer(self, observer: Callable[..., None] | None) -> None:
        """Attach bounded physical phase and causal-sample telemetry."""
        self._rpc_request_observer = observer

    def set_rpc_eligibility_observer(self, observer: Callable[[str | None], None] | None) -> None:
        """Wake the existing demand owner when cached gate eligibility changes."""
        self._rpc_eligibility_observer = observer

    def prepare_durable_retry(self, budget: RpcAttemptBudget, method_key: str | None) -> RpcRetryAuthorization | None:
        """Bind live-episode credit; only final physical claim can spend it."""
        if method_key is None or budget.selected is None:
            return None
        ledger = self._ensure_safety_ledger()
        if self.account_safety_status() is not None:
            return None
        capacity = sum(self._admission_scheduler._capacities.values())
        credit = ledger.bind_method_retry(budget, method_key, capacity)
        if credit is None:
            return None
        budget._retry_candidate = credit
        return credit.authorize() if self.rpc_eligibility(method_key) is None else None

    def _retry_credit(self, budget: RpcAttemptBudget) -> _MethodRetryCredit | None:
        credit = budget._retry_candidate
        if not isinstance(credit, _MethodRetryCredit) or credit.selected != budget.selected:
            return None
        ledger = self._ensure_safety_ledger()
        episode = cast(dict[str, dict[str, object]], ledger._record["methods"]).get(credit.method)
        if not isinstance(episode, _MethodReceipt) or episode.credits.get(credit.selected) is not credit:
            return None
        if episode["generation"] != credit.generation or credit.selected.key.account_id != ledger.verified_account_id:
            return None
        return credit

    def rpc_eligibility(
        self, method_key: str | None = None
    ) -> TelegramRpcThrottled | TelegramRpcMethodRecoveryPending | None:
        """Read current application eligibility; this hint never grants a write."""
        ledger = self._ensure_safety_ledger()
        if self.account_safety_status() is not None or self._rpc_circuit_status().open:
            return TelegramRpcThrottled(latched=True, detail="Telegram account acquisition remains protected")
        deadline = max(account_cooldown_deadline(), ledger.floor_deadline)
        if deadline > time.monotonic():
            return TelegramRpcAdmissionDeferred(
                max(1, math.ceil(deadline - time.monotonic())),
                method_key=method_key,
                eligible_at=deadline,
                reason="server",
            )
        return None if method_key is None else ledger.method_deferral(method_key)

    def _notify_rpc_eligibility_changed(self, method: str | None = None) -> None:
        self._admission_scheduler.notify_eligibility_changed()
        box = self._message_box
        if isinstance(box, GateMessageBox) and not box.wakeup_queued:
            box.wakeup_queued = True
            self._updates_queue.put_nowait(box.wakeup)
        self._notify_rpc_eligibility_observer(method)

    def _notify_rpc_capacity_released(self) -> None:
        """Wake durable capacity waiters without recursively dispatching the arbiter."""
        self._notify_rpc_eligibility_observer(None)

    def _notify_rpc_eligibility_observer(self, method: str | None) -> None:
        if self._rpc_eligibility_observer is not None:
            try:
                self._rpc_eligibility_observer(method)
            except Exception:
                logger.exception("rpc_eligibility_observer_failed")

    def set_flood_event_observer(self, observer: Callable[[FloodWaitObservation], None] | None) -> None:
        """Attach content-free FloodWait telemetry owned by the daemon."""
        self._flood_event_observer = observer

    @property
    def reconnect_event(self) -> asyncio.Event:
        """Signal Telethon's internal reconnect handler to daemon recovery."""
        return self._reconnect_event

    def _observe_rpc_request(  # noqa: PLR0913 - passive fields from the existing physical owner
        self,
        scope: TelegramRpcScope,
        *,
        phase: str = "write_claim",
        admission_sequence: int | None = None,
        replay_ordinal: int | None = None,
        request_method: str | None = None,
        reason: str | None = None,
        attempt_ordinal: int | None = None,
    ) -> None:
        observer = self._rpc_request_observer
        if observer is None:
            return
        try:
            observer(
                scope=scope,
                phase=phase,
                request_method=request_method or scope.request_method,
                admission_sequence=admission_sequence,
                replay_ordinal=replay_ordinal,
                reason=reason,
                attempt_ordinal=attempt_ordinal,
            )
        except Exception:  # noqa: BLE001 - diagnostic callbacks cannot alter RPC behavior
            logger.debug("telegram_rpc_request_observation_failed")

    def check_circuit(self) -> None:
        if (self._authentication_mode or self._logout_mode) and self._ensure_safety_ledger()._authentication_authorized:
            return
        status = self._rpc_circuit_status()
        if status.open:
            raise TelegramRpcThrottled(
                retry_after_seconds=None,
                latched=True,
                detail=status.detail(),
            )

    def _offline_wire_identity(self) -> tuple[int, int | None]:
        session = self.session
        if session is None:
            raise RuntimeError("Telegram session identity is unavailable")
        dc_id = cast(object, session.dc_id)
        key = cast(object, session.auth_key)
        key_id = cast(object, getattr(key, "key_id", None))
        if type(dc_id) is not int or dc_id < 0 or (key_id is not None and type(key_id) is not int):
            raise RuntimeError("invalid offline Telegram transport identity")
        return dc_id, cast(int | None, key_id)

    def retained_draft_peer(self, dialog_id: int) -> object | None:
        """Resolve only retained Session entities; never ask Telegram for a peer."""
        if type(dialog_id) is not int or dialog_id == 0:
            raise ValueError("draft dialog id must be a nonzero integer")
        if self.session is None:
            return None
        try:
            return self.session.get_input_entity(dialog_id)
        except ValueError:
            return None

    def _ensure_safety_ledger(self) -> TelegramRpcSafetyLedger:
        if self._safety is None:
            dc_id, key_id = self._offline_wire_identity()
            if dc_id < 1:
                # Establish the vendor's ordinary initial DC before key-pending
                # persistence; this is local session setup, not a network grant.
                assert self.session is not None
                self.session.set_dc(DEFAULT_DC_ID, DEFAULT_IPV6_IP if self._use_ipv6 else DEFAULT_IPV4_IP, DEFAULT_PORT)
                dc_id, key_id = self._offline_wire_identity()
            self._safety = TelegramRpcSafetyLedger(
                self._safety_store, dc_id=dc_id, key_id=key_id, image_scope_cell=self._image_scope_cell
            )
            global _COOLDOWN_DEADLINE
            if math.isfinite(self._safety.floor_deadline):
                _COOLDOWN_DEADLINE = max(_COOLDOWN_DEADLINE, self._safety.floor_deadline)
        return self._safety

    def _wire_token(self) -> tuple[int, int]:
        return self._wire_generation, self._main_sender.write_generation

    def _wire_budget(self, scope: TelegramRpcScope) -> RpcAttemptBudget:
        if scope.attempt_budget is not None:
            return scope.attempt_budget
        evidence = scope.attempt_evidence
        if evidence is None:
            raise RuntimeError("Telegram application attempt lacks a causal root")
        budget = self._wire_budgets.get(evidence)
        if budget is None:
            contract = demand_contract(scope.demand_kind or current_demand_token().kind)
            budget = RpcAttemptBudget(
                limit=contract.max_rpc_attempts_per_slice or CONNECTION_BOOTSTRAP_MAX_RPC_ATTEMPTS
            )
            self._wire_budgets[evidence] = budget
        return budget

    def register_application(self, request: object, body: bytes, scope: TelegramRpcScope) -> object:
        if not isinstance(body, bytes) or scope.deadline is None or not math.isfinite(scope.deadline):
            raise RuntimeError("invalid owned Telegram application scope")
        capability = self._photo_application_capability(request, scope)
        method = _canonical_wire_method(request)
        self._check_draft_application(request, scope)
        self._check_operator_application(request, scope)
        bootstrap = self._connection_capability is not None and (
            self._connect_owner is asyncio.current_task() or asyncio.current_task() in self._connection_rpc_tasks
        )
        self._check_unverified_application(request, bootstrap)
        actual = _application_request(request)
        scheduled_check = self._check_scheduled_application(actual, scope)
        attempt_id = uuid.uuid4().hex
        bound = replace(
            scope,
            request_method=method,
            owned_attempt_id=attempt_id,
            attempt_budget=self._wire_budget(scope),
            transport_control=False,
            transport_bootstrap=bootstrap,
            transport_auth=self._authentication_mode or self._logout_mode,
        )
        owner = _WireAttempt(self._wire_issuer, attempt_id, method, bound, timing=current_timing())
        owner.photo_capability = capability
        if capability is not None:
            capability.wire_owner = owner
        owner.self_lookup = self._is_self_lookup(actual)
        owner.draft_mutation = type(actual) is functions.messages.SaveDraftRequest
        owner.scheduled_check = scheduled_check
        owner.scheduled_mutation = scheduled_check is not None
        if scheduled_check is not None:
            self._require_scheduled_mutation_budget(bound)
        return owner

    def _photo_application_capability(self, request: object, scope: TelegramRpcScope) -> _PhotoWireCapability | None:
        actual = _application_request(request)
        capability = _PHOTO_CAPABILITY.get()
        restricted = type(actual) in (
            functions.upload.GetFileRequest,
            functions.auth.ExportAuthorizationRequest,
            functions.auth.ImportAuthorizationRequest,
        )
        if capability is None:
            if restricted or scope.demand_kind is DemandKind.MESSAGE_IMAGE_ACQUISITION:
                raise UnclassifiedTelegramRpcError("photo transport requires a gate-owned capability")
            return None
        self._validate_photo_capability(capability, actual, scope)
        return capability

    def _validate_photo_capability(
        self, capability: _PhotoWireCapability, actual: TLRequest, scope: TelegramRpcScope
    ) -> None:
        if self._session_lease is None or not self._session_lease.is_held():
            raise UnclassifiedTelegramRpcError("photo transport requires the held session lease")
        if (
            capability.issuer is not self._photo_issuer
            or capability.revoked
            or capability.owner_task is not asyncio.current_task()
        ):
            raise UnclassifiedTelegramRpcError("photo transport capability is invalid")
        self._validate_photo_capability_binding(capability, actual, scope)

    def _validate_photo_capability_binding(
        self, capability: _PhotoWireCapability, actual: TLRequest, scope: TelegramRpcScope
    ) -> None:
        if (
            scope.demand_kind is not DemandKind.MESSAGE_IMAGE_ACQUISITION
            or self.retained_auth_scope() != capability.scope
            or bytes(actual) != capability.request_body
        ):
            raise UnclassifiedTelegramRpcError("photo transport capability is invalid")
        if capability.lease is not None and (self._photo_aux_lease is not capability.lease or capability.lease.revoked):
            raise UnclassifiedTelegramRpcError("photo auxiliary lease is revoked")

    @staticmethod
    def _draft_budget_valid(scope: TelegramRpcScope) -> bool:
        budget = scope.attempt_budget
        return (
            budget is not None
            and budget.limit == 1
            and budget.selected is None
            and budget._retry_candidate is None
            and scope.attempt_evidence is not None
        )

    def _check_draft_application(self, request: object, scope: TelegramRpcScope) -> None:
        actual = _application_request(request)
        mutation = type(actual) is functions.messages.SaveDraftRequest
        if (
            not mutation
            and scope.demand_kind is not DemandKind.DRAFT_MUTATION
            and scope.source is not TelegramRpcSource.DRAFT_MUTATION
        ):
            return
        self._require_draft_mutation_root(scope)
        expected = None if mutation else AcquisitionKind.DRAFT_SNAPSHOT
        if scope.acquisition_kind is not expected:
            raise UnclassifiedTelegramRpcError("draft operation has incompatible acquisition classification")
        if not mutation and type(actual) is not functions.messages.GetAllDraftsRequest:
            raise UnclassifiedTelegramRpcError("draft mutation preflight permits only GetAllDrafts")

    def _require_draft_mutation_root(self, scope: TelegramRpcScope) -> None:
        if scope.demand_kind is not DemandKind.DRAFT_MUTATION or scope.source is not TelegramRpcSource.DRAFT_MUTATION:
            raise UnclassifiedTelegramRpcError("SaveDraft requires its explicit draft mutation root")
        if not self._draft_budget_valid(scope):
            raise ValueError("draft operations require an explicit single-attempt budget without retry credit")

    @staticmethod
    def _is_scheduled_native_mutation(request: object) -> bool:
        return type(request) in {
            functions.messages.SendMessageRequest,
            functions.messages.EditMessageRequest,
            functions.messages.DeleteScheduledMessagesRequest,
            functions.messages.SendScheduledMessagesRequest,
        }

    @staticmethod
    def _scheduled_budget_valid(scope: TelegramRpcScope) -> bool:
        budget = scope.attempt_budget
        return (
            budget is not None
            and budget.limit == 1
            and budget.selected is None
            and budget._retry_candidate is None
            and scope.attempt_evidence is not None
        )

    def _require_scheduled_mutation_budget(self, scope: TelegramRpcScope) -> None:
        if not self._scheduled_budget_valid(scope):
            raise ValueError("scheduled mutations require an explicit single-attempt budget without retry credit")

    @staticmethod
    def _scheduled_peer_dialog_id(peer: object, account_id: int) -> int | None:
        if type(peer) is types.InputPeerSelf:
            return account_id
        if type(peer) not in {types.InputPeerUser, types.InputPeerChannel, types.InputPeerChat}:
            return None
        try:
            return int(get_peer_id(peer))
        except TypeError, ValueError:
            return None

    @staticmethod
    def _scheduled_date_timestamp(value: object) -> int | None:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            return None
        return int(value.timestamp())

    def _check_scheduled_application(
        self, request: TLRequest, scope: TelegramRpcScope
    ) -> ScheduledMutationCheck | None:
        root_selected = (
            scope.demand_kind is DemandKind.SCHEDULED_MUTATION or scope.source is TelegramRpcSource.SCHEDULED_MUTATION
        )
        method_selected = self._is_scheduled_native_mutation(request)
        check = current_scheduled_mutation_check()
        if not root_selected and not method_selected and check is None:
            return None
        request = self._require_scheduled_application_root(request, scope)
        if check is None:
            raise UnclassifiedTelegramRpcError("scheduled mutation requires its bound operation intent")
        intent = check.intent
        if intent.account_id != self._ensure_safety_ledger().verified_account_id:
            raise UnclassifiedTelegramRpcError("scheduled mutation account does not match the authenticated account")
        if self._scheduled_peer_dialog_id(request.peer, intent.account_id) != intent.dialog_id:
            raise ScheduledWriteRejected("scheduled_target_mismatch")
        self._validate_scheduled_request(request, intent)
        if scope.attempt_budget is not None:
            self._require_scheduled_mutation_budget(scope)
        return check

    @staticmethod
    def _require_scheduled_application_root(
        request: TLRequest, scope: TelegramRpcScope
    ) -> (
        functions.messages.SendMessageRequest
        | functions.messages.EditMessageRequest
        | functions.messages.DeleteScheduledMessagesRequest
    ):
        if (
            scope.demand_kind is not DemandKind.SCHEDULED_MUTATION
            or scope.source is not TelegramRpcSource.SCHEDULED_MUTATION
            or scope.acquisition_kind is not None
        ):
            raise UnclassifiedTelegramRpcError("scheduled mutation requires its explicit inline root")
        if not isinstance(
            request,
            (
                functions.messages.SendMessageRequest,
                functions.messages.EditMessageRequest,
                functions.messages.DeleteScheduledMessagesRequest,
            ),
        ):
            raise UnclassifiedTelegramRpcError("scheduled mutation permits only send/edit/delete scheduled methods")
        return request

    def _validate_scheduled_request(self, request: TLRequest, intent: ScheduledMutationIntent) -> None:
        if intent.action == "create":
            self._validate_scheduled_create(request, intent)
        elif intent.action == "update":
            self._validate_scheduled_update(request, intent)
        else:
            self._validate_scheduled_cancel(request, intent)

    def _validate_scheduled_create(self, request: TLRequest, intent: ScheduledMutationIntent) -> None:
        if type(request) is not functions.messages.SendMessageRequest:
            raise UnclassifiedTelegramRpcError("create permits only messages.sendMessage")
        actual_date = self._scheduled_date_timestamp(request.schedule_date)
        if (
            not self._scheduled_date_matches(actual_date, cast(float, intent.schedule_at_utc))
            or type(request.random_id) is not int
            or request.random_id != intent.random_id
            or request.clear_draft is not None
        ):
            raise ScheduledWriteRejected("scheduled_create_request_mismatch")

    def _validate_scheduled_update(self, request: TLRequest, intent: ScheduledMutationIntent) -> None:
        if type(request) is not functions.messages.EditMessageRequest:
            raise UnclassifiedTelegramRpcError("update permits only messages.editMessage")
        actual_date = self._scheduled_date_timestamp(request.schedule_date)
        if request.id != intent.scheduled_message_id or not self._scheduled_date_matches(
            actual_date, cast(float, intent.schedule_at_utc)
        ):
            raise ScheduledWriteRejected("scheduled_update_request_mismatch")

    @staticmethod
    def _validate_scheduled_cancel(request: TLRequest, intent: ScheduledMutationIntent) -> None:
        if type(request) is not functions.messages.DeleteScheduledMessagesRequest or request.id != [
            intent.scheduled_message_id
        ]:
            raise UnclassifiedTelegramRpcError("cancel permits only the selected scheduled queue ID")

    @staticmethod
    def _scheduled_date_matches(actual: int | None, expected: float) -> bool:
        return actual is not None and actual != _SCHEDULED_ONLINE_SEND_TIMESTAMP and actual == int(expected)

    def _check_operator_application(self, request: object, scope: TelegramRpcScope) -> None:
        if self._logout_mode and (
            type(_application_request(request)) is not functions.auth.LogOutRequest
            or scope.demand_kind is not DemandKind.OPERATOR_LOGOUT
        ):
            raise RuntimeError("ordinary acquisition is unavailable during logout")
        if self._authentication_mode and not self._authentication_method_allowed(request):
            raise RuntimeError("ordinary Telegram acquisition is unavailable during authentication")

    def _check_unverified_application(self, request: object, bootstrap: bool) -> None:
        if (
            self._ensure_safety_ledger().verified_account_id is None
            and not bootstrap
            and not self._authentication_mode
            and not self._logout_mode
            and not self._is_self_lookup(request)
        ):
            raise RuntimeError("unverified Telegram transport permits only owned startup identity")

    @staticmethod
    def _is_self_lookup(request: object) -> bool:
        if type(request) is not functions.users.GetUsersRequest:
            return False
        actual = cast(functions.users.GetUsersRequest, request)
        return len(actual.id) == 1 and type(actual.id[0]) is types.InputUserSelf

    def register_protocol(self, request: object, body: bytes, origin: ProtocolOrigin) -> object:
        allowed: dict[str, tuple[type[object], ...]] = {
            "ack": (types.MsgsAck,),
            "ping": (functions.PingRequest,),
            "state_info": (types.MsgsStateInfo,),
            "auth": (functions.ReqPqMultiRequest, functions.ReqDHParamsRequest, functions.SetClientDHParamsRequest),
        }
        if origin not in allowed or type(request) not in allowed[origin] or not isinstance(body, bytes):
            raise RuntimeError("unclassified Telegram protocol origin")
        if origin == "auth":
            capability = self._connection_capability
            if capability is None:
                raise RuntimeError("authentication lacks an owned lifecycle capability")
            scope = self._scope_for_connection_capability(capability)
        else:
            token = create_demand_token(DemandKind.TELETHON_RECONNECT_PROBE)
            contract = demand_contract(token.kind)
            scope = TelegramRpcScope(
                source=token.source,
                service_class=token.service_class,
                deadline=token.admission_deadline,
                owner_task=token.owner_task,
                demand_kind=token.kind,
                acquisition_kind=token.acquisition_kind,
                source_outstanding_limit=contract.source_outstanding_limit,
                attempt_evidence=token.attempt_evidence,
            )
        kind = type(request)
        constructor_id = getattr(kind, "CONSTRUCTOR_ID", None)
        if type(constructor_id) is not int:
            raise RuntimeError("invalid Telegram protocol constructor")
        method = f"{kind.__module__}.{kind.__name__}:{constructor_id:08x}"
        attempt_id = uuid.uuid4().hex
        bound = replace(
            scope,
            request_method=method,
            owned_attempt_id=attempt_id,
            attempt_budget=self._wire_budget(scope),
            transport_control=origin != "auth",
            transport_bootstrap=origin == "auth",
            transport_auth=self._authentication_mode and origin == "auth",
        )
        return _WireAttempt(self._wire_issuer, attempt_id, method, bound)

    def _register_aux_photo_protocol(
        self, lease: _AuxPhotoLease, request: object, body: bytes, origin: ProtocolOrigin
    ) -> _WireAttempt:
        allowed: dict[str, tuple[type[object], ...]] = {
            "ack": (types.MsgsAck,),
            "ping": (functions.PingRequest,),
            "state_info": (types.MsgsStateInfo,),
            "auth": (functions.ReqPqMultiRequest, functions.ReqDHParamsRequest, functions.SetClientDHParamsRequest),
        }
        if (
            self._photo_aux_lease is not lease
            or lease.revoked
            or origin not in allowed
            or type(request) not in allowed[origin]
            or not isinstance(body, bytes)
        ):
            raise RuntimeError("invalid auxiliary photo protocol request")
        kind = type(request)
        constructor = getattr(kind, "CONSTRUCTOR_ID", None)
        if type(constructor) is not int:
            raise RuntimeError("invalid auxiliary photo constructor")
        attempt_id = uuid.uuid4().hex
        method = f"{kind.__module__}.{kind.__name__}:{constructor:08x}"
        control = origin != "auth"
        bound = replace(
            lease.rpc_scope,
            request_method=method,
            owned_attempt_id=attempt_id,
            attempt_budget=lease.control_budget if control else self._wire_budget(lease.rpc_scope),
            transport_control=control,
            transport_bootstrap=not control,
            transport_auth=False,
        )
        owner = _WireAttempt(self._wire_issuer, attempt_id, method, bound)
        owner.photo_lease = lease
        if not control:
            owner.photo_capability = _PhotoWireCapability(
                self._photo_issuer,
                lease.scope,
                lease.owner_task,
                body,
                lease.budget,
                lease.source_fence,
                True,
                lease=lease,
                use_auxiliary=True,
            )
        return owner

    def _require_wire_owner(self, value: object) -> _WireAttempt:
        if not isinstance(value, _WireAttempt) or value.issuer is not self._wire_issuer:
            raise RuntimeError("unowned Telegram transport attempt")
        return value

    def _wire_release_at(self, scope: TelegramRpcScope) -> float:
        if not self._connection_write_ready(scope):
            return math.inf
        if scope.transport_control:
            return 0.0 if self._control_scope_valid() else math.inf
        if self._wire_fault is not None or self._safety is None:
            return math.inf
        if self._application_holds(scope):
            return math.inf
        if scope.request_method in self._pending_method_receipts:
            return math.inf
        return max(
            account_cooldown_deadline(),
            self._safety.floor_deadline,
            self._safety.method_release_at(scope.request_method, scope.owned_attempt_id),
        )

    def _difference_admission_deferral(
        self, scope: TelegramRpcScope
    ) -> TelegramRpcThrottled | TelegramRpcMethodRecoveryPending | None:
        if scope.source is not TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE or scope.transport_bootstrap:
            return None
        if scope.owner_task is not self._update_loop_owner:
            return None
        global_hold = self.rpc_eligibility()
        if global_hold is not None or scope.request_method is None:
            return global_hold
        return self._ensure_safety_ledger().method_deferral(scope.request_method, scope.owned_attempt_id)

    def _application_holds(self, scope: TelegramRpcScope) -> bool:
        assert self._safety is not None
        if self._automatic_trial_blocked():
            return True
        auth_blocked = scope.transport_auth and (
            not self._safety._authentication_authorized or self._authentication_parent_cancelled()
        )
        ordinary_blocked = not scope.transport_auth and self._ordinary_application_blocked()
        return (
            auth_blocked
            or ordinary_blocked
            or (self._transport_state is not _TransportBoundaryState.READY and not scope.transport_bootstrap)
        )

    def _ordinary_application_blocked(self) -> bool:
        assert self._safety is not None
        return self._logout_mode or self._rpc_circuit_status().open or self._safety.protection_reason is not None

    def _automatic_trial_blocked(self) -> bool:
        assert self._safety is not None
        recovery = self._safety._automatic_recovery()
        return (
            recovery is not None
            and recovery["state"] == "consumed"
            and (not self._safety._auto_resume_enabled or self._auto_resume_stopped())
        )

    def _connection_write_ready(self, scope: TelegramRpcScope) -> bool:
        return (
            self._main_sender.physical_connected() and scope.deadline is not None and scope.deadline > time.monotonic()
        )

    def _authentication_parent_cancelled(self) -> bool:
        if self._authentication_token is None:
            return False
        parent = self._authentication_token.owner_task
        return parent is not None and (parent.done() or bool(parent.cancelling()))

    def _control_scope_valid(self) -> bool:
        ledger = self._safety
        if ledger is None or ledger._storage_failed:
            return False
        identity_failures = {
            "safety_auth_scope_mismatch",
            "safety_canonical_owner_mismatch",
            "authentication_account_mismatch",
        }
        if ledger._startup_hold in identity_failures or ledger._record.get("protection") in identity_failures:
            return False
        if self._transport_state in {_TransportBoundaryState.FAILED, _TransportBoundaryState.DISCONNECTING}:
            return False
        scope = ledger._record.get("scope")
        return (
            isinstance(scope, dict)
            and scope["key_id"] is not None
            and (scope["dc_id"], scope["key_id"]) == self._offline_wire_identity()
        )

    async def prepare_write(self, owner: object) -> object:
        owner = self._require_wire_owner(owner)
        task = asyncio.current_task()
        owner.preparing_task = task
        try:
            return await self._prepare_wire_write(owner)
        finally:
            if owner.preparing_task is task:
                owner.preparing_task = None

    async def _prepare_wire_write(self, owner: _WireAttempt) -> _WirePermit:
        scope = owner.scope
        if owner.revoked:
            raise asyncio.CancelledError
        if owner.terminal and not scope.transport_control:
            raise RuntimeError("Telegram application attempt is terminal")
        if scope.owner_task is not None and (scope.owner_task.done() or scope.owner_task.cancelling()):
            raise asyncio.CancelledError
        self._reject_exhausted_attempt_budget(scope)
        self._check_auto_trial_prepare(owner)
        await self._prepare_application_intent(owner)
        if owner.admission is not None:
            # A vendor replay starts a new physical admission, retaining the
            # same unresolved durable operation and original budget/deadline.
            self._admission_scheduler.complete(owner.admission)
            owner.admission = None
        generation = self._wire_token()
        admission = await self._admission_scheduler.admit(scope)
        try:
            method_generation = await self._prepare_method_permission(owner)
            scheduled_token = await self._revalidate_scheduled_write(owner)
            photo_token = await self._prepare_photo_write(owner)
        except BaseException:
            self._admission_scheduler.complete(admission)
            raise
        if owner.claimed == 0:
            owner.method_generation = method_generation
        return _WirePermit(
            owner,
            admission,
            generation,
            method_generation,
            scheduled_token,
            photo_token=photo_token,
            auxiliary_generation=self._auxiliary_generation(owner),
        )

    @staticmethod
    def _auxiliary_generation(owner: _WireAttempt) -> int | None:
        lease = owner.photo_lease
        return lease.sender.write_generation if lease is not None and lease.sender is not None else None

    async def _prepare_photo_write(self, owner: _WireAttempt) -> PhotoSourceToken | None:
        capability = owner.photo_capability
        if capability is None:
            return None
        if capability.revoked or self.retained_auth_scope() != capability.scope:
            raise UnclassifiedTelegramRpcError("photo capability has been revoked")
        token = await capability.source_fence.prepare() if capability.source_fence is not None else None
        session = capability.photo_session
        if session is not None and not capability.setup:
            if session.slice_reservations >= PHOTO_MAX_SLICE_REQUESTS:
                raise RuntimeError("photo slice physical allowance exhausted")
            session.slice_reservations += 1
        await capability.budget.reserve(setup=capability.setup)
        return token

    def _check_auto_trial_prepare(self, owner: _WireAttempt) -> None:
        if not self._automatic_trial_claim_allowed(owner):
            raise TelegramRpcAdmissionDeferred(
                retry_after_seconds=self._scheduler_policy.admission_retry_seconds,
                detail="Telegram recovery identity is still in progress",
            )

    @staticmethod
    async def _revalidate_scheduled_write(owner: _WireAttempt) -> ScheduledMutationToken | None:
        if owner.scheduled_check is None:
            return None
        token = await owner.scheduled_check.revalidate()
        if not token.is_valid_for(owner.scheduled_check.intent):
            raise ScheduledWriteRejected("scheduled_version_changed")
        return token

    async def _prepare_method_permission(self, owner: _WireAttempt) -> str | None:
        if owner.scope.transport_control:
            return None
        generation = await self._ensure_safety_ledger().prepare_method_trial(
            owner.attempt_id, owner.method, auth_only=owner.scope.transport_auth
        )
        if generation is not None:
            self._notify_rpc_eligibility_changed(owner.method)
        return generation

    async def _prepare_application_intent(self, owner: _WireAttempt) -> None:
        scope = owner.scope
        ledger = self._ensure_safety_ledger()
        if not scope.transport_control:
            ledger.require_writable(auth_only=scope.transport_auth)
            self._check_method_eligibility(owner)
            lease = owner.photo_lease
            if lease is None:
                dc_id, key_id = self._offline_wire_identity()
            else:
                dc_id = lease.dc_id
                key_id = lease.sender.auth_key.key_id if lease.sender is not None and lease.sender.auth_key else None
            recorded_scope = cast(dict[str, object], ledger._record["scope"])
            if (
                lease is None
                and key_id is not None
                and (
                    recorded_scope["stage"] == "key_pending"
                    or (recorded_scope["dc_id"], recorded_scope["key_id"]) != (dc_id, key_id)
                )
            ):
                await ledger.bind_key(dc_id, key_id, auth_only=scope.transport_auth)
            await ledger.prepare(
                owner.attempt_id,
                owner.method,
                transport_identity=(dc_id, key_id),
                auth_only=scope.transport_auth,
                aux_bootstrap=owner.photo_capability.aux_bootstrap if owner.photo_capability is not None else None,
            )
            self._check_method_eligibility(owner)

    def _check_method_eligibility(self, owner: _WireAttempt) -> None:
        deferred = self._ensure_safety_ledger().method_deferral(owner.method, owner.attempt_id)
        if deferred is not None:
            raise deferred

    def _revoke_wire_owner(self, owner: _WireAttempt) -> None:
        owner.revoked = True
        preparing = owner.preparing_task
        if preparing is not None and preparing is not asyncio.current_task() and not preparing.done():
            preparing.cancel()

    def claim_write(self, owner: object, permit: object) -> bool:
        owner = self._require_wire_owner(owner)
        if not isinstance(permit, _WirePermit) or permit.owner is not owner or permit.released:
            raise RuntimeError("unowned Telegram write permission")
        self._check_scheduled_claim(owner, permit)
        if not self._permit_still_eligible(owner, permit):
            return False
        budget = owner.scope.attempt_budget
        if budget is None:
            raise RuntimeError("physical Telegram attempt lacks its original budget")
        ledger = self._ensure_safety_ledger()
        self._validate_claim_intent(owner, ledger)
        if not self._admission_scheduler.record_dispatch(permit.admission):
            return False
        self._record_claim_debit(owner, budget, ledger)
        self._record_wire_dispatch(owner, permit)
        return True

    def _record_claim_debit(
        self, owner: _WireAttempt, budget: RpcAttemptBudget, ledger: TelegramRpcSafetyLedger
    ) -> None:
        credit = self._retry_credit(budget)
        if credit is not None:
            credit.debit(budget)
        else:
            budget.debit()
        if not owner.scope.transport_control:
            ledger._claimed.add(owner.attempt_id)
        if self.automatic_trial_active() and owner.self_lookup and not owner.scope.transport_bootstrap:
            self._auto_resume_self_claimed = True

    @staticmethod
    def _check_scheduled_claim(owner: _WireAttempt, permit: _WirePermit) -> None:
        if not owner.scheduled_mutation:
            return
        check = owner.scheduled_check
        token = permit.scheduled_token
        if check is None or token is None or not token.is_valid_for(check.intent):
            raise ScheduledWriteRejected("scheduled_version_changed")
        if check.intent.schedule_at_utc is not None and (
            check.intent.schedule_at_utc - time.time() < _SCHEDULED_MINIMUM_LEAD_SECONDS
        ):
            raise ScheduledWriteRejected("scheduled_window_expired")
        evidence = owner.scope.attempt_evidence
        if owner.claimed or evidence is None or evidence.scheduled_mutation_dispatched:
            raise ScheduledWriteRejected("scheduled_attempt_already_dispatched")

    @staticmethod
    def _validate_claim_intent(owner: _WireAttempt, ledger: TelegramRpcSafetyLedger) -> None:
        if owner.scope.transport_control:
            return
        ledger.require_writable(auth_only=owner.scope.transport_auth)
        if owner.attempt_id not in cast(dict[str, object], ledger._record["intents"]):
            raise RuntimeError("outbound attempt lacks durable intent")

    def _record_wire_dispatch(self, owner: _WireAttempt, permit: _WirePermit) -> None:
        owner.claimed += 1
        owner.dispatch_at = time.monotonic()
        self._arm_response_timeout(owner)
        if owner.claimed == 1 and not owner.scope.transport_control:
            try:
                owner.incident_claimed_at_ms = int(time.time() * 1000)
            except Exception:  # noqa: BLE001 - telemetry cannot affect the physical claim
                pass
        owner.admission = permit.admission
        self._record_draft_dispatch(owner)
        self._record_scheduled_dispatch(owner)
        if owner.scope.attempt_budget is not None:
            owner.scope.attempt_budget.last_method = owner.method
            owner.observation_ordinal = owner.scope.attempt_budget.attempts
        permit.released = True
        if owner.timing is not None:
            owner.timing.record_rpc_attempt()
        self._observe_rpc_request(
            owner.scope,
            admission_sequence=permit.admission.sequence,
            replay_ordinal=owner.claimed,
            reason="replay" if owner.claimed > 1 else None,
            attempt_ordinal=owner.observation_ordinal,
        )

    @staticmethod
    def _arm_response_timeout(owner: _WireAttempt) -> None:
        if owner.claimed == 1 and owner.response_timer is not None and owner.scope.response_timeout_seconds is not None:
            assert owner.dispatch_at is not None
            deadline = owner.dispatch_at + owner.scope.response_timeout_seconds
            if owner.scope.deadline is not None:
                deadline = min(deadline, owner.scope.deadline)
            owner.response_timer.reschedule(deadline)

    @staticmethod
    def _record_draft_dispatch(owner: _WireAttempt) -> None:
        if owner.scope.demand_kind is not DemandKind.DRAFT_MUTATION:
            return
        evidence = owner.scope.attempt_evidence
        assert evidence is not None
        if owner.draft_mutation:
            evidence.draft_mutation_dispatched = True
        else:
            evidence.draft_preflight_dispatched = True

    @staticmethod
    def _record_scheduled_dispatch(owner: _WireAttempt) -> None:
        if owner.scope.demand_kind is DemandKind.SCHEDULED_MUTATION:
            evidence = owner.scope.attempt_evidence
            assert evidence is not None
            evidence.scheduled_mutation_dispatched = True

    def observe_write_return(self, owner: object) -> None:
        """Observe local writer acceptance, without claiming a provider receipt."""
        try:
            attempt = self._require_wire_owner(owner)
            if attempt.observed_write_returns >= attempt.claimed:
                return
            attempt.observed_write_returns = attempt.claimed
            self._observe_rpc_request(
                attempt.scope,
                phase="write_return",
                replay_ordinal=attempt.claimed,
                admission_sequence=None if attempt.admission is None else attempt.admission.sequence,
                attempt_ordinal=attempt.observation_ordinal,
            )
        except Exception:  # noqa: BLE001 - observation cannot fail an already completed local write
            logger.debug("telegram_rpc_write_observation_failed")

    def _permit_still_eligible(self, owner: _WireAttempt, permit: _WirePermit) -> bool:
        if owner.revoked:
            return False
        if not self._photo_claim_current(owner, permit):
            return False
        if self._reconnect_quiescing and owner.scope.source is TelegramRpcSource.TELETHON_RECONNECT_PROBE:
            return False
        if not self._automatic_trial_claim_allowed(owner):
            return False
        if permit.generation != self._wire_token() or self._wire_release_at(owner.scope) > time.monotonic():
            return False
        return self._permit_method_and_owner_eligible(owner, permit)

    def _photo_claim_current(self, owner: _WireAttempt, permit: _WirePermit) -> bool:
        lease = owner.photo_lease
        if lease is not None and not self._aux_photo_claim_current(lease, permit):
            return False
        capability = owner.photo_capability
        if capability is None:
            return True
        return self._photo_capability_claim_current(capability, permit)

    def _photo_capability_claim_current(self, capability: _PhotoWireCapability, permit: _WirePermit) -> bool:
        if capability.lease is not None and (self._photo_aux_lease is not capability.lease or capability.lease.revoked):
            return False
        return (
            not capability.revoked
            and self._session_lease is not None
            and self._session_lease.is_held()
            and self.retained_auth_scope() == capability.scope
            and (permit.photo_token is None or permit.photo_token.is_current())
        )

    def _aux_photo_claim_current(self, lease: _AuxPhotoLease, permit: _WirePermit) -> bool:
        sender = lease.sender
        if self._photo_aux_lease is not lease or lease.revoked or sender is None:
            return False
        if sender.write_generation != permit.auxiliary_generation or not sender.physical_connected():
            return False
        return (
            self.retained_auth_scope() == lease.scope
            and self._session_lease is not None
            and self._session_lease.is_held()
        )

    def _permit_method_and_owner_eligible(self, owner: _WireAttempt, permit: _WirePermit) -> bool:
        if not owner.scope.transport_control and not self._ensure_safety_ledger().method_claim_valid(
            owner.method, owner.attempt_id, permit.method_generation
        ):
            return False
        task = owner.scope.owner_task
        if task is not None and (task.done() or task.cancelling()):
            return False
        return self._budget_can_claim(owner)

    def _automatic_trial_claim_allowed(self, owner: _WireAttempt) -> bool:
        ledger = self._ensure_safety_ledger()
        recovery = ledger._automatic_recovery()
        if recovery is None or recovery["state"] != "consumed" or owner.scope.transport_control:
            return True
        if not self.automatic_trial_active() or not ledger._auto_resume_enabled or self._auto_resume_stopped():
            return False
        capability = self._connection_capability
        bootstrap = (
            owner.scope.transport_bootstrap
            and capability is not None
            and owner.scope.attempt_budget is capability.attempt_budget
        )
        return bootstrap or self._automatic_trial_identity_claim(owner)

    def _automatic_trial_identity_claim(self, owner: _WireAttempt) -> bool:
        return (
            owner.self_lookup
            and not owner.scope.transport_bootstrap
            and not self._auto_resume_self_claimed
            and owner.claimed == 0
            and owner.scope.demand_kind is DemandKind.SELF_PROFILE_MAINTENANCE
            and owner.scope.owner_task is self._auto_resume_identity_owner
            and self._auto_resume_identity_owner is not None
            and owner.scope.attempt_budget is not None
            and owner.scope.attempt_budget.limit == 1
        )

    def _budget_can_claim(self, owner: _WireAttempt) -> bool:
        if owner.scope.demand_kind is DemandKind.DRAFT_MUTATION:
            return self._draft_budget_can_claim(owner)
        if owner.scope.demand_kind is DemandKind.SCHEDULED_MUTATION:
            return self._scheduled_budget_can_claim(owner)
        budget = owner.scope.attempt_budget
        if budget is None:
            return True
        credit = self._retry_credit(budget)
        if credit is not None:
            return credit.available and (
                credit.attempts < credit.limit or credit.granted or owner.method == credit.method
            )
        if budget._retry_candidate is not None:
            return False
        return not budget.exhausted

    def _draft_budget_can_claim(self, owner: _WireAttempt) -> bool:
        if not self._draft_budget_valid(owner.scope):
            return False
        budget = owner.scope.attempt_budget
        evidence = owner.scope.attempt_evidence
        assert budget is not None and evidence is not None
        dispatched = evidence.draft_mutation_dispatched if owner.draft_mutation else evidence.draft_preflight_dispatched
        return not dispatched and owner.claimed == 0 and not budget.exhausted

    def _scheduled_budget_can_claim(self, owner: _WireAttempt) -> bool:
        if not self._scheduled_budget_valid(owner.scope):
            return False
        evidence = owner.scope.attempt_evidence
        budget = owner.scope.attempt_budget
        assert evidence is not None and budget is not None
        return not evidence.scheduled_mutation_dispatched and owner.claimed == 0 and not budget.exhausted

    def defer_write(self, owner: object, permit: object) -> None:
        owner = self._require_wire_owner(owner)
        if not isinstance(permit, _WirePermit) or permit.owner is not owner:
            raise RuntimeError("unowned deferred Telegram write")
        if not permit.released:
            permit.released = True
            self._admission_scheduler.complete(permit.admission)

    def observe_error_now(self, owner: object, error: BaseException) -> None:
        owner = self._require_wire_owner(owner)
        if isinstance(error, (FloodWaitError, FloodPremiumWaitError, FloodTestPhoneWaitError)):
            _annotate_flood_attempt(
                error,
                request_method=owner.method,
                admission_sequence=owner.admission.sequence if owner.admission is not None else None,
                dispatch_at_monotonic=owner.dispatch_at,
                dispatch_kind="actual_send" if owner.claimed else "never_dispatched",
            )
            _seconds, receipt = self._record_flood(error, scope=owner.scope, wire_owner=owner)
            owner.receipt = receipt
            self._notify_rpc_eligibility_changed()
        elif not isinstance(error, RPCError) and owner.claimed and not owner.scope.transport_control:
            self._note_photo_fault_owner(owner)
            self._wire_fault = "unresolved_outbound_outcome"
            self._recovery_cause = (
                RecoveryCause.TRANSIENT
                if isinstance(error, (ConnectionError, TimeoutError, OSError))
                else RecoveryCause.READ_UNCERTAINTY
            )
            self._notify_rpc_eligibility_changed()

    def observe_response_now(self, owner: object) -> None:
        owner = self._require_wire_owner(owner)
        timer = owner.response_timer
        if owner.claimed and timer is not None and not timer.expired():
            timer.reschedule(owner.scope.deadline)

    def _note_photo_fault_owner(self, owner: _WireAttempt) -> None:
        if self._wire_fault is None and owner.photo_capability is not None and owner.photo_capability.aux_bootstrap:
            self._photo_wire_fault_owner = owner.attempt_id
        elif self._photo_wire_fault_owner != owner.attempt_id:
            self._photo_wire_fault_owner = None

    def _observe_result_phase(self, owner: _WireAttempt, error: BaseException | None) -> None:
        if (
            owner.claimed
            and not owner.observed_result
            and (
                not owner.scope.transport_control
                or error is not None
                or owner.method.startswith("telethon.tl.functions.PingRequest:")
            )
        ):
            owner.observed_result = True
            self._observe_rpc_request(
                owner.scope,
                phase="known_response" if error is None or isinstance(error, RPCError) else "unknown_outcome",
            )

    async def observe_result(
        self, owner: object, error: BaseException | None, result: object = None
    ) -> BaseException | None:
        owner = self._require_wire_owner(owner)
        self._observe_result_phase(owner, error)
        if owner.scope.transport_control:
            if owner.admission is not None:
                self._admission_scheduler.complete(owner.admission)
                owner.admission = None
            return None
        if owner.terminal:
            if owner.receipt is not None:
                await asyncio.shield(owner.receipt)
            if self._retired_photo_export(owner, error):
                return RuntimeError("retired photo authorization response discarded")
            return None
        method_affected = owner.method in cast(dict[str, object], self._ensure_safety_ledger()._record["methods"])
        try:
            rejected = (
                RuntimeError("retired photo authorization response discarded")
                if self._retired_photo_export(owner, error)
                else await self._guard_identity_result(owner, error, result)
            )
            await self._complete_wire_result(owner, error, rejected)
            owner.terminal = True
            self._note_successful_acquisition(owner, error, rejected)
            return rejected
        finally:
            if owner.admission is not None:
                self._admission_scheduler.complete(owner.admission)
                owner.admission = None
            if method_affected:
                self._notify_rpc_eligibility_changed(owner.method)

    async def _complete_wire_result(
        self, owner: _WireAttempt, error: BaseException | None, rejected: BaseException | None
    ) -> None:
        if owner.receipt is not None:
            await asyncio.shield(owner.receipt)
            return
        known = self._wire_outcome_known(owner, error)
        ledger = self._ensure_safety_ledger()
        await ledger.complete(
            owner.attempt_id,
            known_outcome=known,
            method_generation=owner.method_generation,
            method_success=error is None and rejected is None,
            recovery_cause=RecoveryCause.TRANSIENT
            if owner.claimed and self._transient_terminal_error(error) and self.automatic_trial_active()
            else None,
        )
        if not known:
            self._observe_unknown_incident(owner, error, ledger)

    def _wire_outcome_known(self, owner: _WireAttempt, error: BaseException | None) -> bool:
        return not self._retired_photo_export(owner, error) and (
            error is None
            or isinstance(error, RPCError)
            or (isinstance(error, ScheduledWriteRejected) and owner.claimed == 0)
        )

    @staticmethod
    def _retired_photo_export(owner: _WireAttempt, error: BaseException | None) -> bool:
        capability = owner.photo_capability
        return (
            error is None
            and owner.revoked
            and capability is not None
            and isinstance(capability.aux_bootstrap, dict)
            and capability.aux_bootstrap.get("purpose") == "export"
        )

    @staticmethod
    def _transient_terminal_error(error: BaseException | None) -> bool:
        code = getattr(error, "code", None) if isinstance(error, RPCError) else None
        return isinstance(error, (ConnectionError, TimeoutError, OSError)) or (
            type(code) is int and _BACKEND_ERROR_CODE_MIN <= code <= _BACKEND_ERROR_CODE_MAX
        )

    def _observe_unknown_incident(
        self, owner: _WireAttempt, error: BaseException | None, ledger: TelegramRpcSafetyLedger
    ) -> None:
        if not owner.claimed or owner.incident_recorded:
            return
        owner.incident_recorded = True
        try:
            observer = self._rpc_request_observer
            if observer is not None:
                cause = (
                    "shutdown"
                    if self._transport_state is _TransportBoundaryState.DISCONNECTING
                    else "cancelled"
                    if isinstance(error, asyncio.CancelledError)
                    else "transport_lost"
                    if isinstance(error, (ConnectionError, TimeoutError, OSError))
                    else "unspecified"
                )
                observer(
                    scope=owner.scope,
                    phase="incident_unknown",
                    request_method=owner.method,
                    incident_run=ledger._run,
                    incident_claimed_at_ms=owner.incident_claimed_at_ms,
                    incident_settled_at_ms=int(time.time() * 1000),
                    incident_cause=cause,
                )
        except Exception:  # noqa: BLE001 - observations cannot alter settled safety
            logger.debug("telegram_rpc_incident_observation_failed")

    def _note_successful_acquisition(
        self, owner: _WireAttempt, error: BaseException | None, rejected: BaseException | None
    ) -> None:
        if owner.draft_mutation or owner.scheduled_mutation:
            return
        if (
            error is None
            and rejected is None
            and owner.claimed
            and not owner.scope.transport_bootstrap
            and owner.scope.source is not TelegramRpcSource.TELETHON_RECONNECT_PROBE
        ):
            self._auto_resume_acquired = self._auto_resume_stable_since is not None

    async def _guard_identity_result(
        self, owner: _WireAttempt, error: BaseException | None, result: object
    ) -> BaseException | None:
        if error is not None:
            return None
        user = self._identity_result_user(owner, result)
        if isinstance(user, RuntimeError):
            await self._ensure_safety_ledger().reject_identity_result(mismatched_account=False)
            return user
        if user is None:
            return None
        ledger = self._ensure_safety_ledger()
        if owner.scope.transport_auth:
            if not await ledger.validate_authentication_account(user.id):
                return RuntimeError("Telegram authentication returned a different account")
        else:
            transition = ledger._record["transition"]
            scope = cast(dict[str, object], ledger._record["scope"])
            expected = transition["expected_account"] if isinstance(transition, dict) else scope["account_id"]
            if await self._reject_conflicting_identity(expected, user.id):
                return RuntimeError("DC transition returned a different Telegram account")
            await ledger.verify_account(user.id)
        return None

    async def _reject_conflicting_identity(self, expected: object, account_id: int) -> bool:
        ledger = self._ensure_safety_ledger()
        if expected not in {None, account_id} or any(
            owner is not None and owner != account_id for owner in ledger._store.canonical_owners()
        ):
            await ledger.reject_identity_result(mismatched_account=True)
            return True
        return False

    @staticmethod
    def _identity_result_user(owner: _WireAttempt, result: object) -> types.User | RuntimeError | None:
        if owner.self_lookup:
            if isinstance(result, list) and len(result) == 1 and type(result[0]) is types.User:
                return result[0]
            return RuntimeError("Telegram self identity response is invalid")
        if not owner.scope.transport_auth:
            return None
        if type(result) is types.auth.LoginTokenSuccess:
            result = cast(object, cast(types.auth.LoginTokenSuccess, result).authorization)
            if type(result) is not types.auth.Authorization:
                return RuntimeError("Telegram authentication response is invalid")
        if type(result) is types.auth.Authorization:
            authorization = cast(types.auth.Authorization, result)
            return (
                authorization.user
                if type(authorization.user) is types.User
                else RuntimeError("Telegram authentication response has no account")
            )
        return None

    def protect_transport(self, error: BaseException) -> None:
        self._reset_auto_resume_stability()
        self._photo_wire_fault_owner = None
        if isinstance(error, (TransportConnectError, TransportWriteError)):
            self._wire_fault = "transport_unavailable"
            self._recovery_cause = RecoveryCause.TRANSIENT
        else:
            self._wire_fault = "transport_integrity_failure"
            self._recovery_cause = None
        self._notify_rpc_eligibility_changed()

    def transport_terminated(self, error: BaseException | None) -> BaseException | None:
        self._reset_auto_resume_stability()
        expected = self._transport_state is _TransportBoundaryState.DISCONNECTING
        self._wire_generation += 1
        if not expected:
            self._photo_wire_fault_owner = None
            self._transport_state = _TransportBoundaryState.FAILED
            self._wire_fault = self._wire_fault or "transport_terminated"
            if self._wire_fault == "transport_terminated":
                self._recovery_cause = RecoveryCause.TRANSIENT
            error = error or RuntimeError("Telegram transport terminated unexpectedly")
        self._notify_rpc_eligibility_changed()
        return error

    def account_safety_status(self) -> dict[str, object] | None:
        """Expose only safe protection state, never session binding material."""
        ledger = self._ensure_safety_ledger()
        reason = (
            self._wire_fault
            or ledger.protection_reason
            or (
                "transport_terminated"
                if self._transport_state is _TransportBoundaryState.FAILED
                else "operator_logout"
                if self._logout_mode
                else None
            )
        )
        if reason is None:
            return None
        status = ledger.status()
        automatic = ledger.auto_resume_status()
        if self._wire_fault == "transport_integrity_failure":
            automatic.update(
                eligible=False,
                block_reason="transport_integrity_failure",
                state="manual",
                next_attempt_at_utc=None,
                next_trial_at_utc=None,
            )
        return {
            "generation": status["generation"],
            "reason": reason,
            "known_retry_until_utc": status["retry_until_utc"],
            "unresolved_request_count": status["unresolved_request_count"],
            "unresolved_requests": status["unresolved_requests"],
            "unresolved_requests_truncated": status["unresolved_requests_truncated"],
            "automatic_resume": automatic,
        }

    def configure_auto_resume(self, *, enabled: bool) -> None:
        """Configure local recovery policy without granting network permission."""
        self._ensure_safety_ledger().configure_auto_resume(enabled=enabled)
        logger.info(
            "telegram_automatic_resume_configured enabled=%s max_attempts=%d", enabled, len(_AUTO_RESUME_DELAYS)
        )
        if not enabled:
            self._reset_auto_resume_stability()

    def bind_session_lease(self, lease: TelegramSessionLease) -> None:
        """Bind the actual live session-lock capability before startup admission."""
        if type(lease) is not TelegramSessionLease or not lease.is_held() or self._session_lease is not None:
            raise RuntimeError("a held Telegram session lease is required")
        self._session_pin = lease._pin()
        self._session_lease = lease

    def set_auto_resume_stop(self, stopped: Callable[[], bool]) -> None:
        """Use the daemon's existing stop signal at the physical trial boundary."""
        self._auto_resume_stopped = stopped

    def automatic_trial_active(self) -> bool:
        """Report only a trial consumed by this lifecycle."""
        ledger = self._ensure_safety_ledger()
        recovery = ledger._automatic_recovery()
        return bool(recovery and recovery["state"] == "consumed" and recovery["consumed_run"] == ledger._run)

    def bind_auto_resume_identity(self) -> None:
        """Bind the sole identity probe to the startup owner of this trial."""
        if not self.automatic_trial_active() or self._auto_resume_identity_owner is not None:
            raise RuntimeError("automatic recovery identity owner is unavailable")
        self._auto_resume_identity_owner = asyncio.current_task()
        if self._auto_resume_identity_owner is None:
            raise RuntimeError("automatic recovery identity has no task")

    def _reset_auto_resume_stability(self) -> None:
        self._auto_resume_stable_since = None
        self._auto_resume_acquired = False
        self._auto_resume_progress = False

    def note_canonical_progress(self) -> None:
        """Called only after an accepted canonical demand revision commits."""
        if self._auto_resume_stable_since is not None and self._auto_resume_acquired:
            self._auto_resume_progress = True

    async def maintain_auto_resume(self) -> bool:
        """Observe local receipts; only the normal next lifecycle may consume a trial."""
        ledger = self._ensure_safety_ledger()
        if not self._startup_safety_prepared or not self._automatic_lifecycle_available():
            self._reset_auto_resume_stability()
            return False
        if self.account_safety_status() is not None or self._rpc_circuit_status().open:
            return await self._maintain_auto_failure(ledger)
        await self._maintain_auto_resume_stability(ledger)
        return False

    async def _maintain_auto_failure(self, ledger: TelegramRpcSafetyLedger) -> bool:
        self._reset_auto_resume_stability()
        if self._cooldown_save_tasks or self._pending_method_receipts or ledger._operations:
            return False
        cause = self._recovery_cause
        if self._wire_fault == "transport_integrity_failure":
            return False
        if self._rpc_circuit_status().open or ledger.protection_reason == "flood_wait_circuit_open":
            cause = RecoveryCause.FLOOD_WAIT
        await ledger.maintain_auto_resume(cause=cause, quiesced=False)
        recovery = ledger._automatic_recovery()
        if recovery is not None and recovery["state"] == "fencing":
            await self._disconnect_main_sender()
        return await ledger.maintain_auto_resume(cause=cause, quiesced=True)

    def _automatic_lifecycle_available(self) -> bool:
        if self._auto_resume_stopped():
            return False
        return not (
            self._logout_mode
            or self._authentication_mode
            or self._connect_owner is not None
            or self._transport_state is _TransportBoundaryState.DISCONNECTING
        )

    async def _maintain_auto_resume_stability(self, ledger: TelegramRpcSafetyLedger) -> None:
        recovery = ledger._automatic_recovery()
        if recovery is None or recovery["state"] != "succeeded" or not ledger._auto_resume_enabled:
            return
        if not self.is_connected() or self.rpc_eligibility() is not None:
            self._reset_auto_resume_stability()
            return
        if self._auto_resume_stable_since is None:
            self._auto_resume_stable_since = time.monotonic()
        if (
            self._auto_resume_progress
            and time.monotonic() >= self._auto_resume_stable_since + _AUTO_RESUME_STABILITY_SECONDS
        ):
            await ledger.reset_auto_resume()
            self._reset_auto_resume_stability()

    async def finish_startup_auto_resume(self) -> None:
        """Confirm replay restoration before beginning the observed stability interval."""
        ledger = self._ensure_safety_ledger()
        recovery = ledger._automatic_recovery()
        if recovery is not None and recovery["state"] == "consumed":
            account_id = ledger.verified_account_id
            if account_id is None:
                raise RuntimeError("automatic recovery lacks verified identity")
            await ledger.finish_recovery(account_id)
        if recovery is not None and recovery["state"] in {"consumed", "succeeded"}:
            self._reset_auto_resume_stability()
            self._auto_resume_stable_since = time.monotonic()

    def _authentication_method_allowed(self, request: object) -> bool:
        actual = _application_request(request)
        if type(actual) is functions.users.GetUsersRequest:
            return len(actual.id) == 1 and type(actual.id[0]) is types.InputUserSelf
        if type(actual) is functions.updates.GetDifferenceRequest:
            transition = self._ensure_safety_ledger()._record["transition"]
            return isinstance(transition, dict) and transition["validated_account"] is not None
        method = _canonical_wire_method(request)
        allowed = (
            functions.auth.ExportLoginTokenRequest,
            functions.auth.ImportLoginTokenRequest,
            functions.auth.CheckPasswordRequest,
            functions.account.GetPasswordRequest,
            functions.updates.GetStateRequest,
            functions.updates.GetDifferenceRequest,
            functions.help.GetConfigRequest,
            functions.users.GetUsersRequest,
        )
        return any(method == f"{kind.__module__}.{kind.__name__}:{kind.CONSTRUCTOR_ID:08x}" for kind in allowed)

    async def begin_authentication(
        self, *, acknowledge_unknown_outcome: bool = False, replace_credentials: bool = False
    ) -> None:
        token = current_demand_token()
        if token.kind is not DemandKind.OPERATOR_AUTH:
            raise RuntimeError("authentication requires an explicit operator demand")
        ledger = self._ensure_safety_ledger()
        dc_id, key_id = self._offline_wire_identity()
        clear_key = await ledger.begin_authentication(
            dc_id, key_id, acknowledged=acknowledge_unknown_outcome, replace_credentials=replace_credentials
        )
        if clear_key:
            assert self.session is not None
            with self._image_identity_change():
                self.session.auth_key = None
                cast(AuthKey, self._main_sender.auth_key).key = None
                self.session.save()
        self._authentication_token = token
        self._authentication_mode = True

    async def _on_login(self, user: object) -> object:
        if not isinstance(user, types.User):
            raise RuntimeError("login result lacks an owned authentication purpose")
        ledger = self._ensure_safety_ledger()
        if self._authentication_mode:
            if not await ledger.validate_authentication_account(user.id):
                raise RuntimeError("Telegram authentication returned a different account")
        elif self._connection_capability is None or self._connect_owner is not asyncio.current_task():
            raise RuntimeError("login callback lacks an owned startup purpose")
        elif ledger.verified_account_id != user.id:
            raise RuntimeError("startup identity was not verified before login mutation")
        return cast(object, await super()._on_login(user))

    async def finish_authentication(self) -> types.User:
        if not self._authentication_mode:
            raise RuntimeError("authentication is not active")
        profile = cast(object, await self.get_me())
        if not isinstance(profile, types.User):
            raise RuntimeError("authentication did not return an account identity")
        if not await self._ensure_safety_ledger().validate_authentication_account(profile.id, finish=True):
            raise RuntimeError("Telegram authentication returned a different account")
        # The client remains AUTH-only through teardown: it must not save the
        # vendor message-box head or resume ordinary acquisition after binding.
        return profile

    async def logout_account(self) -> None:
        if current_demand_token().kind is not DemandKind.OPERATOR_LOGOUT or not self.is_connected():
            raise ValueError("logout requires the connected owned account")
        ledger = self._ensure_safety_ledger()
        ledger.require_writable()
        self._logout_mode = True
        self._admission_scheduler.notify_eligibility_changed()
        prepared = False
        try:
            await ledger.begin_logout(self._wire_generation)
            prepared = True
            result = await self(functions.auth.LogOutRequest())
            if type(result) is not types.auth.LoggedOut:
                raise RuntimeError("Telegram logout was not confirmed")
            await ledger.confirm_logout()
            self.close_update_delivery()
            assert self.session is not None
            with self._image_identity_change():
                self.session.auth_key = None
                cast(AuthKey, self._main_sender.auth_key).key = None
                self.session.save()
            await ledger.clear_logged_out_key()
        except BaseException:
            if not prepared:
                self._logout_mode = False
                self._admission_scheduler.notify_eligibility_changed()
            raise

    def retained_auth_scope(self) -> TelegramAuthScope | None:
        """Previously verified matching offline scope authorizes retained reads."""
        ledger = self._ensure_safety_ledger()
        account_id = ledger.verified_account_id
        if account_id is None:
            return None
        dc_id, key_id = self._offline_wire_identity()
        if key_id is None:
            return None
        return TelegramAuthScope(AUTH_SCOPE_VERSION, account_id, dc_id, key_id)

    def capture_image_scope(self) -> ScopeToken | None:
        """Capture a verified scope generation on the daemon event loop."""
        asyncio.get_running_loop()
        scope = self._verified_image_scope()
        if not self._image_scope_cell.publish(scope) or scope is None:
            return None
        return self._image_scope_cell.capture()

    def _verified_image_scope(self) -> TelegramAuthScope | None:
        ledger = self._safety
        session = self.session
        lease = self._session_lease
        if ledger is None or ledger._storage_failed or session is None or lease is None or not lease.is_held():
            return None
        return self._scope_from_initialized_identity(ledger, session)

    @staticmethod
    def _scope_from_initialized_identity(ledger: TelegramRpcSafetyLedger, session: Session) -> TelegramAuthScope | None:
        key = session.auth_key
        persisted = ledger._record.get("scope")
        if ledger._record.get("protection") in {"invalid_account_identity", "authentication_account_mismatch"}:
            return None
        if (
            not isinstance(persisted, dict)
            or key is None
            or key.key_id is None
            or ledger._record.get("transition") is not None
        ):
            return None
        account = ledger.verified_account_id
        if account is None or (persisted.get("dc_id"), persisted.get("key_id"), persisted.get("account_id")) != (
            session.dc_id,
            key.key_id,
            account,
        ):
            return None
        return TelegramAuthScope(AUTH_SCOPE_VERSION, account, session.dc_id, key.key_id)

    def claim_image_scope(self, token: ScopeToken) -> bool:
        """Atomically authorize the captured generation without database access."""
        return self._image_scope_cell.claim(token)

    @contextmanager
    def _image_identity_change(self) -> Iterator[None]:
        change = self._image_scope_cell.begin_change()
        try:
            yield
        finally:
            self._image_scope_cell.end_change(change)

    async def authorize_account_recovery(self, expected_generation: str, acknowledged: bool) -> None:
        await self._ensure_safety_ledger().authorize_recovery(expected_generation, acknowledged=acknowledged)

    async def prepare_startup_safety(self) -> None:
        """Consume an explicit one-use grant before any connection/bootstrap write."""
        ledger = self._ensure_safety_ledger()
        try:
            if self._session_lease is not None and self._session_lease.is_held():
                await ledger._reconcile_aux_after_restart(self._session_lease)
            await ledger.consume_auto_resume()
            await ledger.consume_recovery()
            ledger.require_writable()
        finally:
            self._startup_safety_prepared = True

    async def verify_startup_account(self, account_id: int) -> None:
        ledger = self._ensure_safety_ledger()
        await ledger.verify_account(account_id)
        trial = ledger._record["recovery"]
        if isinstance(trial, dict) and "automatic" not in trial and trial["state"] == "consumed":
            await ledger.finish_recovery(account_id)

    async def fail_startup_recovery(self, error: BaseException | None = None) -> None:
        ledger = self._ensure_safety_ledger()
        trial = ledger._record.get("recovery")
        if self._settled_auto_failure(trial, error):
            return
        if error is not None:
            self.protect_transport(error)
            if not isinstance(error, (TransportConnectError, TransportWriteError)):
                await ledger._commit(self._protect_startup_integrity)
                return
        if ledger._storage_failed or not isinstance(trial, dict) or trial["state"] != "consumed":
            return
        if "automatic" in trial:
            cause = self._recovery_cause or RecoveryCause(
                cast(str, cast(dict[str, object], trial["automatic"])["cause"])
            )
            await ledger._commit(lambda record: ledger._schedule_auto_resume(record, cause=cause, observed_flood=False))
            return
        await ledger.fail_recovery()

    @staticmethod
    def _settled_auto_failure(trial: object, error: BaseException | None) -> bool:
        return (
            isinstance(trial, dict)
            and "automatic" in trial
            and trial["state"] in {"fencing", "exhausted"}
            and (error is None or isinstance(error, (TransportConnectError, TransportWriteError)))
        )

    @staticmethod
    def _protect_startup_integrity(record: dict[str, object]) -> None:
        if record["protection"] in {
            None,
            "unresolved_outbound_outcome",
            "transport_unavailable",
            "flood_wait_circuit_open",
        }:
            record["protection"] = "transport_integrity_failure"
            record["generation"] = uuid.uuid4().hex

    async def __call__(
        self, request: object, ordered: bool = False, flood_sleep_threshold: int | None = None
    ) -> object:
        """Admit one scalar logical RPC and invoke Telethon."""
        del flood_sleep_threshold  # The gate always uses the client-level zero threshold.
        if request is not None and is_list_like(request):
            raise ValueError("transport batching is forbidden; use sequential scalar calls")
        capability = self._connection_capability
        if capability is not None and self._connect_owner is asyncio.current_task():
            return await self._run_with_connection_capability(
                request,
                ordered=ordered,
                capability=capability,
                connection_owned=True,
            )
        if capability is not None and asyncio.current_task() in self._connection_rpc_tasks:
            return await self._call_registered(request, ordered=ordered, connection_owned=True)
        return await self._call_classified(request, ordered=ordered)

    async def _call_classified(self, request: object, *, ordered: bool) -> object:
        """Dispatch after a demand context has been installed explicitly."""
        if self._authentication_mode:
            return await self._call_registered(request, ordered=ordered)
        if isinstance(request, (GetDifferenceRequest, GetChannelDifferenceRequest)):
            return await self._call_update_difference(request, ordered=ordered)
        return await self._call_registered(request, ordered=ordered)

    async def _photo_owned_request(  # noqa: PLR0913 - explicit claim inputs bind the photo request.
        self,
        request: TLRequest,
        *,
        scope: TelegramAuthScope,
        budget: PhotoBudget,
        source_fence: PhotoSourceFence | None,
        setup: bool,
        photo_session: _GuardedPhotoSession | None = None,
    ) -> object:
        task = asyncio.current_task()
        if task is None or self.retained_auth_scope() != scope:
            raise UnclassifiedTelegramRpcError("photo account scope is unavailable")
        root = current_demand_token()
        if root.kind is not DemandKind.MESSAGE_IMAGE_ACQUISITION:
            raise UnclassifiedTelegramRpcError("photo acquisition requires its owned demand")
        if photo_session is None:
            if type(request) not in (functions.messages.GetMessagesRequest, functions.channels.GetMessagesRequest):
                raise UnclassifiedTelegramRpcError("photo observation permits only the exact message request")
        else:
            self._validate_photo_file_request(photo_session, request)
        capability = _PhotoWireCapability(self._photo_issuer, scope, task, bytes(request), budget, source_fence, setup)
        capability.photo_session = photo_session
        if photo_session is not None and photo_session.dc_id != scope.dc_id:
            capability.lease = self._photo_aux_lease
            capability.use_auxiliary = True
        context = _PHOTO_CAPABILITY.set(capability)
        try:
            return await self(request)
        finally:
            capability.revoked = True
            _PHOTO_CAPABILITY.reset(context)

    async def _retire_uncertain_photo_bootstrap(self, capability: _PhotoWireCapability, lease: _AuxPhotoLease) -> None:
        owner = capability.wire_owner
        provenance = capability.aux_bootstrap
        if owner is None or owner.claimed == 0 or not isinstance(provenance, dict):
            return
        self._revoke_wire_owner(owner)
        ledger = self._ensure_safety_ledger()
        if not await self._photo_unknown_intent_owned(owner, ledger):
            return
        if self._wire_fault is None:
            self._wire_fault = "unresolved_outbound_outcome"
            self._photo_wire_fault_owner = owner.attempt_id
        if not owner.terminal:
            await ledger.complete(owner.attempt_id, known_outcome=False, method_generation=owner.method_generation)
            owner.terminal = True
        if provenance["purpose"] == "import":
            await self._close_photo_aux_lease()
        await ledger._contain_aux_unknown(
            owner.attempt_id, captured_scope=cast(dict[str, object], provenance["main_scope"]), lease_id=lease.lease_id
        )
        self._release_photo_unknown_wire_fault(owner, ledger)

    @staticmethod
    async def _photo_unknown_intent_owned(owner: _WireAttempt, ledger: TelegramRpcSafetyLedger) -> bool:
        if owner.receipt is not None:
            await asyncio.shield(owner.receipt)
        prior = cast(dict[str, dict[str, object]], ledger._record["intents"]).get(owner.attempt_id)
        return prior is not None and (not owner.terminal or prior["phase"] == "unknown")

    def _release_photo_unknown_wire_fault(self, owner: _WireAttempt, ledger: TelegramRpcSafetyLedger) -> None:
        if (
            self._photo_wire_fault_owner == owner.attempt_id
            and self._wire_fault == "unresolved_outbound_outcome"
            and not ledger._unresolved()
            and ledger.protection_reason is None
        ):
            self._wire_fault = None
            self._recovery_cause = None
            self._photo_wire_fault_owner = None
            self._notify_rpc_eligibility_changed()

    def _validate_photo_file_request(self, session: _GuardedPhotoSession, request: TLRequest) -> None:
        session._require_owner()
        if type(request) is not functions.upload.GetFileRequest or not session.slicing:
            raise UnclassifiedTelegramRpcError("photo download request is not owned")
        if (
            bytes(request.location) != bytes(cast(types.InputPhotoFileLocation, session.locator.location))
            or request.cdn_supported
            or request.precise
            or request.limit != PHOTO_CHUNK_BYTES
        ):
            raise UnclassifiedTelegramRpcError("photo download request differs from the approved locator")
        if request.offset < 0 or request.offset % PHOTO_CHUNK_BYTES or request.offset >= session.locator.size:
            raise UnclassifiedTelegramRpcError("photo download request differs from the approved locator")

    async def _photo_file_request(
        self, session: _GuardedPhotoSession, request: functions.upload.GetFileRequest
    ) -> object:
        return await self._photo_owned_request(
            request,
            scope=session.scope,
            budget=session.budget,
            source_fence=session.source_fence,
            setup=False,
            photo_session=session,
        )

    async def _ensure_photo_transport(self, session: _GuardedPhotoSession) -> None:
        session._require_owner()
        if self.retained_auth_scope() != session.scope:
            raise UnclassifiedTelegramRpcError("photo account scope changed")
        if session.dc_id != session.scope.dc_id:
            await self._open_photo_aux_lease(session)

    async def _close_photo_aux_lease(self) -> None:
        lease = self._photo_aux_lease
        if lease is None:
            return
        lease.revoked = True
        sender = lease.sender
        if sender is not None:
            await sender.disconnect_for_containment()
            await sender.settle_shutdown()
            sender.auth_key.key = None
        self._photo_aux_lease = None

    async def _open_photo_aux_lease(self, session: _GuardedPhotoSession) -> None:
        if self._photo_aux_lease is not None:
            if self._photo_aux_lease.dc_id == session.dc_id and not self._photo_aux_lease.revoked:
                return
            raise RuntimeError("another photo auxiliary lease is active")
        task = asyncio.current_task()
        if task is None or self._session_pin is None or self.retained_auth_scope() != session.scope:
            raise RuntimeError("photo auxiliary ownership is unavailable")
        lease = _AuxPhotoLease(
            self,
            session.scope,
            task,
            current_rpc_scope(),
            session.budget,
            session.source_fence,
            session.dc_id,
            uuid.uuid4().hex,
            session.work,
            session.locator,
        )
        self._photo_aux_lease = lease
        try:
            option = await self._photo_dc_option(session, lease)
            await self._connect_aux_photo_sender(lease, option)
            await self._authorize_aux_photo_sender(session, lease)
        except BaseException:
            cleanup = asyncio.create_task(self._close_photo_aux_lease(), context=Context())
            await self._join_vendor_disconnect(cleanup)
            raise

    async def _connect_aux_photo_sender(self, lease: _AuxPhotoLease, option: types.DcOption) -> None:
        sender = GuardedMTProtoSender(
            None,
            authority=_AuxPhotoAuthority(lease),
            loggers=self._log,
            retries=0,
            delay=self._retry_delay,
            auto_reconnect=False,
            connect_timeout=self._timeout,
            auth_key_callback=None,
            updates_queue=None,
            auto_reconnect_callback=None,
        )
        lease.sender = sender
        connection = GuardedConnectionTcpFull(
            option.ip_address,
            option.port,
            option.id,
            loggers=self._log,
            proxy=self._proxy,
            local_addr=self._local_addr,
        )
        await sender.connect(connection)

    async def _authorize_aux_photo_sender(self, session: _GuardedPhotoSession, lease: _AuxPhotoLease) -> None:
        exported = await self._photo_bootstrap_request(
            session,
            lease,
            functions.auth.ExportAuthorizationRequest(lease.dc_id),
            use_auxiliary=False,
        )
        if type(exported) is not types.auth.ExportedAuthorization:
            await self._ensure_safety_ledger().reject_identity_result(mismatched_account=False)
            raise RuntimeError("invalid exported photo authorization")
        exported = cast(types.auth.ExportedAuthorization, exported)
        import_request = functions.auth.ImportAuthorizationRequest(exported.id, exported.bytes)
        init_request = copy.copy(self._init_request)
        init_request.query = import_request
        wrapped = functions.InvokeWithLayerRequest(LAYER, init_request)
        authorized = await self._photo_bootstrap_request(session, lease, wrapped, use_auxiliary=True)
        if type(authorized) is not types.auth.Authorization:
            await self._ensure_safety_ledger().reject_identity_result(mismatched_account=False)
            raise RuntimeError("invalid auxiliary photo authorization")
        authorized = cast(types.auth.Authorization, authorized)
        if type(authorized.user) is not types.User:
            await self._ensure_safety_ledger().reject_identity_result(mismatched_account=False)
            raise RuntimeError("invalid auxiliary photo authorization")
        if authorized.user.id != session.scope.account_id:
            await self._ensure_safety_ledger().reject_identity_result(mismatched_account=True)
            raise RuntimeError("auxiliary photo authorization returned a different account")

    async def _photo_dc_option(self, session: _GuardedPhotoSession, lease: _AuxPhotoLease) -> types.DcOption:
        config = type(self)._config
        if config is None:
            config = await self._photo_bootstrap_request(
                session, lease, functions.help.GetConfigRequest(), use_auxiliary=False
            )
        if type(config) is not types.Config:
            raise RuntimeError("Telegram DC configuration is unavailable")
        candidates = [option for option in config.dc_options if option.id == lease.dc_id and not option.cdn]
        if not candidates:
            raise RuntimeError("photo media DC is unavailable")
        return next((option for option in candidates if bool(option.ipv6) == self._use_ipv6), candidates[0])

    async def _photo_bootstrap_request(
        self, session: _GuardedPhotoSession, lease: _AuxPhotoLease, request: TLRequest, *, use_auxiliary: bool
    ) -> object:
        session._require_owner()
        actual = _application_request(request)
        if type(actual) not in (
            functions.help.GetConfigRequest,
            functions.auth.ExportAuthorizationRequest,
            functions.auth.ImportAuthorizationRequest,
        ):
            raise UnclassifiedTelegramRpcError("unsupported photo bootstrap method")
        if self._photo_aux_lease is not lease or lease.revoked or self.retained_auth_scope() != lease.scope:
            raise UnclassifiedTelegramRpcError("photo auxiliary lease is unavailable")
        capability = _PhotoWireCapability(
            self._photo_issuer,
            lease.scope,
            lease.owner_task,
            bytes(actual),
            lease.budget,
            lease.source_fence,
            True,
            lease=lease,
            use_auxiliary=use_auxiliary,
        )
        capability.photo_session = session
        if type(actual) in (functions.auth.ExportAuthorizationRequest, functions.auth.ImportAuthorizationRequest):
            capability.aux_bootstrap = self._photo_bootstrap_provenance(
                lease, type(actual) is functions.auth.ExportAuthorizationRequest
            )
        context = _PHOTO_CAPABILITY.set(capability)
        try:
            return await self(request)
        except BaseException as error:
            if (
                capability.aux_bootstrap is not None
                and not isinstance(error, RPCError)
                and not isinstance(error.__cause__, RPCError)
            ):
                cleanup = asyncio.create_task(self._retire_uncertain_photo_bootstrap(capability, lease))
                await self._join_vendor_disconnect(cleanup)
            raise
        finally:
            capability.revoked = True
            _PHOTO_CAPABILITY.reset(context)

    def _photo_bootstrap_provenance(self, lease: _AuxPhotoLease, export: bool) -> dict[str, object]:
        sender = lease.sender
        if sender is None or sender.auth_key is None or sender.auth_key.key_id is None or self._session_pin is None:
            raise RuntimeError("photo auxiliary key is unavailable")
        return {
            "purpose": "export" if export else "import",
            "main_scope": {
                "dc_id": lease.scope.dc_id,
                "key_id": lease.scope.auth_key_id,
                "account_id": lease.scope.account_id,
            },
            "aux_dc_id": lease.dc_id,
            "aux_key_id": sender.auth_key.key_id,
            "lease_id": lease.lease_id,
            "work_id": lease.work.key,
            "attempt_generation": lease.work.attempt_generation,
            "source_generation": lease.work.source_generation,
            "contained": False,
            "lock_scope": self._session_pin.identity(),
            "lock_owner_id": self._session_pin.owner_id(),
        }

    def _photo_message_request(self, target: PhotoTarget) -> TLRequest:
        if not self._photo_target_valid(target):
            raise ValueError("invalid photo message target")
        if self.session is None:
            raise ValueError("photo dialog identity is unavailable")
        peer = self.session.get_input_entity(target.dialog_id)
        if type(peer) is types.InputPeerSelf:
            scope = self.retained_auth_scope()
            if scope is None or target.dialog_id != scope.account_id:
                raise ValueError("photo dialog identity differs from the verified account")
        elif get_peer_id(peer) != target.dialog_id:
            raise ValueError("photo dialog identity does not match the requested dialog")
        ids: list[types.TypeInputMessage] = [types.InputMessageID(target.message_id)]
        if type(peer) is types.InputPeerChannel:
            channel = get_input_channel(peer)
            if channel is None:
                raise ValueError("photo channel identity is unavailable")
            return functions.channels.GetMessagesRequest(channel, ids)
        return functions.messages.GetMessagesRequest(ids)

    @staticmethod
    def _photo_target_valid(target: PhotoTarget) -> bool:
        return (
            type(target.dialog_id) is int
            and target.dialog_id != 0
            and type(target.message_id) is int
            and target.message_id > 0
        )

    async def resolve_photo(
        self,
        target: PhotoTarget,
        *,
        scope: TelegramAuthScope,
        observation_order: int,
        budget: PhotoBudget,
        source_fence: PhotoSourceFence,
    ) -> PhotoObservation:
        """Resolve one exact photo message through the existing physical gateway."""
        if type(observation_order) is not int or observation_order < 0 or self.retained_auth_scope() != scope:
            raise ValueError("photo observation binding is invalid")
        token = self.capture_image_scope()
        if token is None or token.scope != scope:
            raise ValueError("photo observation scope is unavailable")
        request = self._photo_message_request(target)
        response = await self._photo_owned_request(
            request, scope=scope, budget=budget, source_fence=source_fence, setup=True
        )
        message = self._photo_message_from_response(response, target)
        locator = photo_locator_from_message(message)
        if not self.claim_image_scope(token):
            raise ValueError("photo observation scope changed")
        self._photo_locators.clear()
        self._photo_locators[id(locator)] = (locator, token)
        return PhotoObservation(message, locator)

    @staticmethod
    def _photo_message_from_response(response: object, target: PhotoTarget) -> object:
        messages = getattr(response, "messages", None)
        if not isinstance(messages, list) or len(messages) != 1:
            raise ValueError("photo message is unavailable")
        message = messages[0]
        peer = cast(object, getattr(message, "peer_id", None))
        if getattr(message, "id", None) != target.message_id or peer is None or get_peer_id(peer) != target.dialog_id:
            raise ValueError("photo response does not match the requested message")
        return cast(object, message)

    async def open_photo_download(
        self,
        locator: PhotoLocator,
        *,
        scope: TelegramAuthScope,
        work: PhotoWork,
        budget: PhotoBudget,
        source_fence: PhotoSourceFence,
    ) -> PhotoDownloadSession:
        """Bind one gate-issued locator to one live photo job owner."""
        task = asyncio.current_task()
        issued_token = self._require_issued_photo_locator(locator, scope)
        if task is None:
            raise UnclassifiedTelegramRpcError("photo acquisition has no owner")
        if self._photo_session is not None or self._session_lease is None or not self._session_lease.is_held():
            raise TelegramRpcAdmissionDeferred(
                retry_after_seconds=self._scheduler_policy.admission_retry_seconds,
                detail="photo acquisition owner is unavailable",
            )
        if current_demand_token().kind is not DemandKind.MESSAGE_IMAGE_ACQUISITION:
            raise UnclassifiedTelegramRpcError("photo acquisition requires its owned demand")
        session = _GuardedPhotoSession(self, locator, scope, work, budget, source_fence, task)
        self._photo_session = session
        try:
            await self._restore_photo_session(session, issued_token)
            self._photo_locators.pop(id(locator))
        except BaseException:
            if self._photo_session is session:
                self._photo_session = None
            raise
        return session

    async def _restore_photo_session(self, session: _GuardedPhotoSession, token: ScopeToken) -> None:
        redirect_dc = await session.budget.current_redirect()
        if redirect_dc is not None:
            if type(redirect_dc) is not int or redirect_dc <= 0:
                raise RuntimeError("persisted photo migration destination is invalid")
            session.dc_id = redirect_dc
            session.redirected = True
        if (
            self.retained_auth_scope() != session.scope
            or not self.claim_image_scope(token)
            or self._photo_session is not session
        ):
            raise UnclassifiedTelegramRpcError("photo session binding changed")

    def _require_issued_photo_locator(self, locator: PhotoLocator, scope: TelegramAuthScope) -> ScopeToken:
        issued = self._photo_locators.get(id(locator))
        if issued is None or issued[0] is not locator or issued[1].scope != scope:
            raise UnclassifiedTelegramRpcError("photo locator is not owned by this account")
        if not self.claim_image_scope(issued[1]) or self.retained_auth_scope() != scope:
            raise UnclassifiedTelegramRpcError("photo locator scope changed")
        return issued[1]

    async def _call(
        self,
        sender: object,
        request: object,
        ordered: bool = False,
        flood_sleep_threshold: int | None = None,
    ) -> Never:
        """Reject unbound Telethon dispatch; the gate owns its sole super call."""
        del sender, request, ordered, flood_sleep_threshold
        raise RuntimeError("unbound Telethon _call bypasses Telegram RPC admission")

    async def _borrow_exported_sender(self, dc_id: int) -> Never:
        """Reject multi-sender transport paths outside the admitted main sender."""
        del dc_id
        raise RuntimeError("borrowed Telethon senders are unsupported by Telegram RPC admission")

    async def _create_exported_sender(self, dc_id: int) -> Never:
        """Reject exported sender creation before any remote side effect."""
        del dc_id
        raise RuntimeError("exported Telethon senders are unsupported by Telegram RPC admission")

    async def _get_cdn_client(self, cdn_redirect: object) -> Never:
        """Reject CDN clients before they create an unadmitted transport."""
        del cdn_redirect
        raise RuntimeError("Telethon CDN clients are unsupported by Telegram RPC admission")

    async def connect(self) -> None:
        """Run Telethon bootstrap with one explicit, bounded RPC capability."""
        current_task = asyncio.current_task()
        if self._connect_owner is not None:
            if self._connect_owner is current_task:
                await self._reconnect_connection_owner()
                return
            raise self._connection_owner_deferred()
        self.check_circuit()
        if self._transport_state is _TransportBoundaryState.READY:
            if self._main_sender.is_connected():
                return
            raise self._transport_unavailable()
        await self._connect_with_retry_slots()

    def disconnect(self) -> asyncio.Future[None]:
        self._reset_auto_resume_stability()
        self._reconnect_quiescing = True
        if self._owned_disconnect is None:
            self._owned_disconnect = self.loop.create_task(self._disconnect_owned())
            self._owned_disconnect.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        return asyncio.shield(self._owned_disconnect)

    async def _disconnect_owned(self) -> None:
        """Keep vendor teardown, late receipts and Session close owned on failure."""
        cancelled = await self._quiesce_reconnect_probe()
        teardown = super().disconnect()
        if teardown is None:
            if cancelled:
                raise asyncio.CancelledError
            return
        primary: BaseException | None = None
        try:
            cancelled = await self._join_vendor_disconnect(teardown) or cancelled
        except BaseException as error:  # noqa: BLE001 - Session close and receipt drain must still run.
            primary = error
        primary = await self._drain_disconnect_safety(primary)
        try:
            self._close_owned_session()
        except Exception as error:  # noqa: BLE001 - preserve the original transport failure.
            primary = primary or error
        if primary is not None:
            raise primary
        if cancelled:
            raise asyncio.CancelledError

    async def _drain_disconnect_safety(self, primary: BaseException | None) -> BaseException | None:
        try:
            if self._safety is not None:
                await self._safety.drain()
        except BaseException as error:  # noqa: BLE001 - preserve the first failure after all owners settle.
            return primary or error
        return primary

    async def _connect_with_retry_slots(
        self,
        *,
        initial_dc: int | None = None,
        retry_delays: Iterator[float] | None = None,
        initial_delay: float = 0.0,
    ) -> None:
        if self._connect_owner is not None:
            raise self._connection_owner_deferred()
        self._raise_if_transport_unavailable(allow_disconnected=True)
        capability = self._capture_connection_capability()
        scope = self._scope_for_connection_capability(capability)
        initially_ready = self._transport_state is _TransportBoundaryState.READY
        self._connection_capability = capability
        self._connection_retry_delays = iter(self._transient_retry_delays) if retry_delays is None else retry_delays
        self._connection_mutation_started = False
        self._connection_update_loop = self._capture_connection_update_loop(initial_dc)
        reservation = object()
        self._connect_owner = reservation
        owner: asyncio.Task[object] | None = None
        release_reservation = False
        try:
            await self._check_connection_admission(scope)
            owner = self._start_connection_owner(scope, initial_dc=initial_dc, initial_delay=initial_delay)
            self._connect_owner = owner
            await owner
            release_reservation = True
        except BaseException as bootstrap_error:
            try:
                release_reservation, cancellation_arrived = await self._finish_connection_failure(
                    owner,
                    bootstrap_error,
                    disconnect_transport=not (initially_ready and not self._connection_mutation_started),
                )
            finally:
                if isinstance(bootstrap_error, OSError):
                    await self.fail_startup_recovery(bootstrap_error)
            if cancellation_arrived:
                raise asyncio.CancelledError from None
            raise
        finally:
            if release_reservation and (self._connect_owner is reservation or self._connect_owner is owner):
                self._connect_owner = None
                self._connection_capability = None
                self._connection_retry_delays = None
                self._connection_update_loop = None
                self._connection_rpc_tasks.clear()

    def _capture_connection_update_loop(self, initial_dc: int | None) -> asyncio.Task[object] | None:
        current_task = asyncio.current_task()
        if initial_dc is not None and current_task is self._updates_handle:
            return current_task
        return None

    async def _reconnect_connection_owner(self) -> None:
        retained_loop = self._connection_update_loop
        if retained_loop is None:
            await super().connect()  # type: ignore[misc]
            return
        catch_up = self._catch_up
        self._catch_up = False
        try:
            await super().connect()  # type: ignore[misc]
        finally:
            self._catch_up = catch_up
            replacement = self._updates_handle
            if replacement is not None and replacement is not retained_loop:
                # Telethon creates this task at its non-yielding connect tail.
                replacement.cancel()
            self._updates_handle = retained_loop
            if replacement is not None and replacement is not retained_loop:
                await asyncio.gather(replacement, return_exceptions=True)

    def _start_connection_owner(
        self, scope: TelegramRpcScope, *, initial_dc: int | None = None, initial_delay: float = 0.0
    ) -> asyncio.Task[object]:
        retry_delays = self._connection_retry_delays
        assert retry_delays is not None

        async def run_vendor_connect() -> None:
            migration_dc = initial_dc
            for retry_delay in chain((initial_delay,), retry_delays):
                try:
                    if migration_dc is None:
                        await super(TelegramRpcGate, self).connect()  # type: ignore[misc]
                    else:
                        await self._continue_connection_migration(scope, migration_dc, retry_delay)
                    return
                except _ConnectionDcMigrationRequestedError as exc:
                    migration_dc = exc.dc_id
            raise self._connection_redirect_exhausted()

        return asyncio.get_running_loop().create_task(
            run_vendor_connect(),
            name="telethon_connection_bootstrap",
            context=Context(),
        )

    async def _finish_connection_failure(
        self,
        owner: asyncio.Task[object] | None,
        bootstrap_error: BaseException,
        *,
        disconnect_transport: bool = True,
    ) -> tuple[bool, bool]:
        if owner is None:
            return True, False
        cancelled, cleanup_error = await self._complete_failed_connection_cleanup(
            disconnect_transport=disconnect_transport
        )
        release_reservation = cleanup_error is None
        cancellation_wins = (
            isinstance(bootstrap_error, asyncio.CancelledError)
            or cancelled
            or isinstance(cleanup_error, asyncio.CancelledError)
        )
        if cleanup_error is not None:
            logger.error(
                "telegram_connection_bootstrap_cleanup_failed",
                exc_info=(type(cleanup_error), cleanup_error, cleanup_error.__traceback__),
            )
        if cancellation_wins:
            return release_reservation, True
        if cleanup_error is not None:
            raise TelegramRpcAdmissionDeferred(
                retry_after_seconds=self._scheduler_policy.admission_retry_seconds,
                latched=False,
                detail="Telegram transport termination is unconfirmed; retry after manual recovery",
            ) from bootstrap_error
        return release_reservation, False

    def _connection_owner_deferred(self) -> TelegramRpcAdmissionDeferred:
        retry_seconds = self._scheduler_policy.admission_retry_seconds
        return TelegramRpcAdmissionDeferred(
            retry_after_seconds=retry_seconds,
            latched=False,
            detail=f"Telegram connection bootstrap is already running; retry in {retry_seconds:g}s",
        )

    def _connection_redirect_exhausted(self) -> TelegramRpcAdmissionDeferred:
        retry_seconds = self._scheduler_policy.admission_retry_seconds
        return TelegramRpcAdmissionDeferred(
            retry_after_seconds=retry_seconds,
            latched=False,
            detail=f"Telegram connection bootstrap redirect exhausted; retry in {retry_seconds:g}s",
        )

    def _next_migration_delay(self, retry_delays: Iterator[float]) -> float:
        delay = next(retry_delays, None)
        if delay is None:
            raise self._connection_redirect_exhausted() from None
        return delay

    async def _continue_connection_migration(
        self,
        scope: TelegramRpcScope,
        dc_id: int,
        retry_delay: float,
    ) -> None:
        """Resume one unwound migration without refreshing connection identity."""
        if retry_delay:
            await asyncio.sleep(retry_delay)
        await self._check_connection_admission(scope)
        self._dc_transition_target = dc_id
        try:
            with self._image_identity_change():
                await super()._switch_dc(dc_id)  # type: ignore[misc]
                transition = self._ensure_safety_ledger()._record["transition"]
                if isinstance(transition, dict) and transition["purpose"] == "dc_migration":
                    account_id = transition["validated_account"]
                    if type(account_id) is not int:
                        profile = cast(object, await self.get_me())
                        if not isinstance(profile, types.User):
                            raise RuntimeError("DC transition did not verify the account")
                        account_id = profile.id
                    await self._ensure_safety_ledger().finish_dc_transition(account_id)
        finally:
            self._dc_transition_target = None

    async def _check_connection_admission(self, scope: TelegramRpcScope) -> None:
        await self._wait_for_account_cooldown(scope)
        self.check_circuit()
        _AdmissionAwareSender._raise_if_cancel_pending()
        self._reject_exhausted_attempt_budget(scope)

    def _reject_exhausted_attempt_budget(self, scope: TelegramRpcScope) -> None:
        budget = scope.attempt_budget
        if budget is None:
            return
        credit = self._retry_credit(budget)
        if credit is not None and credit.available:
            return
        if budget.exhausted or budget._retry_candidate is not None:
            self._admission_scheduler.record_attempt_budget_exhausted(scope)
            raise RpcAttemptBudgetExhaustedError("Telegram RPC attempt budget is exhausted")

    async def _get_dc(self, dc_id: int, cdn: bool = False) -> object:
        dc = await super()._get_dc(dc_id, cdn=cdn)  # type: ignore[misc]
        capability = self._connection_capability
        if capability is not None and asyncio.current_task() is self._connect_owner:
            await self._check_connection_admission(self._scope_for_connection_capability(capability))
            if self._dc_transition_target == dc_id:
                await self._ensure_safety_ledger().begin_dc_transition(dc_id, capability.owned_generation)
            # The vendor mutates session/auth keys immediately after this return.
            self._connection_mutation_started = True
        return dc

    async def _cleanup_failed_connection_bootstrap(self, *, disconnect_transport: bool = True) -> None:
        """Settle owned tasks; preserve READY transport until the mutation boundary."""
        tasks = tuple(self._connection_rpc_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._connection_rpc_tasks.clear()
        if disconnect_transport:
            await self._disconnect_connection_owner()

    async def _disconnect_connection_owner(self) -> None:
        if self._connection_update_loop is None:
            await super()._disconnect()  # type: ignore[misc]
            return
        await self._sender.disconnect()
        await helpers._cancel(logger, keepalive_handle=self._keepalive_handle)

    async def _disconnect(self) -> None:
        if self._connection_update_loop is not None and asyncio.current_task() is self._connect_owner:
            await self._disconnect_connection_owner()
            return
        await super()._disconnect()  # type: ignore[misc]

    async def _complete_failed_connection_cleanup(
        self, *, disconnect_transport: bool = True
    ) -> tuple[bool, BaseException | None]:
        """Keep the connection reservation until a cancellation-safe cleanup finishes."""
        cleanup = asyncio.get_running_loop().create_task(
            self._cleanup_failed_connection_bootstrap(disconnect_transport=disconnect_transport),
            name="telethon_connection_cleanup",
            context=Context(),
        )
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.wait({cleanup})
            except asyncio.CancelledError:
                cancelled = True
        try:
            cleanup.result()
        except asyncio.CancelledError as exc:
            return cancelled, exc
        except Exception as exc:  # noqa: BLE001 - a teardown failure is returned with its cancellation state.
            return cancelled, exc
        return cancelled, None

    async def _switch_dc(self, new_dc: int) -> None:
        """Unwind RPC redirects so only the reserved connection owner mutates DC."""
        owner = self._connect_owner
        if owner is not None and self._connection_capability is not None:
            current_task = asyncio.current_task()
            if current_task in self._connection_rpc_tasks:
                raise _ConnectionDcMigrationRequestedError(new_dc)
            if current_task is not owner:
                raise self._connection_owner_deferred()
        self._raise_if_transport_unavailable()
        if owner is None:
            if self._authentication_mode and asyncio.current_task() not in self._vendor_call_tasks:
                await self._connect_with_retry_slots(initial_dc=new_dc)
                return
            raise _ConnectionDcMigrationRequestedError(new_dc)
        with self._image_identity_change():
            await super()._switch_dc(new_dc)  # type: ignore[misc]

    def _capture_connection_capability(self) -> _ConnectionCapability:
        """Coalesce one finite mechanical allowance under the original cause."""
        try:
            token = current_demand_token()
        except MissingTelegramDemandContextError:
            token = create_demand_token(DemandKind.TELETHON_CONNECTION_BOOTSTRAP)
            deadline = token.admission_deadline
        else:
            scope = current_rpc_scope()
            if scope.deadline is None:
                raise RuntimeError("connection owner lacks its original deadline")
            deadline = scope.deadline
        contract = demand_contract(DemandKind.TELETHON_CONNECTION_BOOTSTRAP)
        child = replace(
            token,
            kind=contract.kind,
            source=contract.source,
            service_class=contract.service_class,
            admission_deadline=deadline,
            acquisition_kind=AcquisitionKind.CONNECTION_BOOTSTRAP,
        )
        budget = self._connection_budgets.get(token.attempt_evidence)
        if budget is None:
            budget = RpcAttemptBudget(CONNECTION_BOOTSTRAP_MAX_RPC_ATTEMPTS)
            self._connection_budgets[token.attempt_evidence] = budget
        return _ConnectionCapability(child, deadline, budget, self._wire_generation)

    @staticmethod
    def _scope_for_connection_capability(capability: _ConnectionCapability) -> TelegramRpcScope:
        contract = demand_contract(capability.token.kind)
        return TelegramRpcScope(
            source=capability.token.source,
            service_class=capability.token.service_class,
            deadline=capability.deadline,
            owner_task=capability.token.owner_task,
            demand_kind=capability.token.kind,
            acquisition_kind=capability.token.acquisition_kind,
            source_outstanding_limit=contract.source_outstanding_limit,
            attempt_budget=capability.attempt_budget,
            attempt_evidence=capability.token.attempt_evidence,
        )

    def _sender_capability(self) -> tuple[_ConnectionCapability, bool]:
        """Resolve a sender task's explicit root without accepting inherited work."""
        try:
            token = current_demand_token()
        except MissingTelegramDemandContextError as exc:
            return self._connection_capability_for_current_owner(exc), True
        capability = self._connection_capability_from_scope(token, current_rpc_scope())
        return capability, asyncio.current_task() in self._connection_rpc_tasks

    @staticmethod
    def _connection_capability_from_scope(
        token: DemandToken,
        scope: TelegramRpcScope,
    ) -> _ConnectionCapability:
        if scope.deadline is None:
            raise RuntimeError("Telegram RPC scope must carry its absolute admission deadline")
        return _ConnectionCapability(token, scope.deadline, scope.attempt_budget)

    def _connection_capability_for_current_owner(self, error: UnclassifiedTelegramDemandError) -> _ConnectionCapability:
        capability = self._connection_capability
        if capability is None or self._connect_owner is not asyncio.current_task():
            raise error
        return capability

    def _run_with_connection_capability(
        self,
        request: object,
        *,
        ordered: bool,
        capability: _ConnectionCapability,
        connection_owned: bool,
    ) -> asyncio.Task[object]:
        """Transfer exactly one captured connection capability to an admitted RPC."""
        call = (
            self._call_registered(request, ordered=ordered, connection_owned=True)
            if connection_owned
            else self._call_classified(request, ordered=ordered)
        )
        task = create_scoped_rpc_task(
            call,
            source=capability.token.source,
            deadline=capability.deadline,
            demand_token=capability.token,
            attempt_budget=capability.attempt_budget,
        )
        if connection_owned:
            self._connection_rpc_tasks.add(task)
            task.add_done_callback(self._connection_rpc_tasks.discard)
        return task

    async def _call_update_difference(self, request: object, *, ordered: bool) -> object:
        """Give only Telethon's actual difference request its protocol identity."""
        task = asyncio.current_task()
        connection_owned = task is self._connect_owner or task in self._connection_rpc_tasks
        try:
            result = await self._call_classified_difference(request, ordered=ordered)
            await self._recover_scheduled_difference_gap(request, result)
            return result
        except (TelegramRpcThrottled, TelegramRpcMethodRecoveryPending) as exc:
            if (
                task is self._update_loop_owner
                and not connection_owned
                and isinstance(self._message_box, GateMessageBox)
            ):
                self._message_box.preserve_deferred(request, exc)
                raise ValueError("Telegram difference waits for gate eligibility") from exc
            raise
        except RpcAdmissionExpiredError as exc:
            if not connection_owned and exc.source is TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE:
                raise OSError("Telegram update difference admission deadline elapsed") from exc
            raise

    async def _recover_scheduled_difference_gap(self, request: object, result: object) -> None:
        if self._authentication_mode:
            return
        target: int | None = None
        reason: str | None = None
        if isinstance(result, types.updates.DifferenceTooLong):
            reason = "account_difference_too_long"
        elif isinstance(result, types.updates.ChannelDifferenceTooLong):
            channel = cast(types.TypeInputChannel | None, getattr(request, "channel", None))
            try:
                target = int(cast(int, get_peer_id(channel))) if channel is not None else None
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Telegram channel difference gap has no canonical peer") from exc
            reason = "channel_difference_too_long"
        if reason is not None:
            callback = self._scheduled_continuity_recovery
            if callback is None or self._ensure_safety_ledger().verified_account_id is None:
                raise RuntimeError("scheduled continuity recovery is not bound to a verified account")
            await callback(target, reason)

    async def _call_classified_difference(self, request: object, *, ordered: bool) -> object:
        try:
            token = current_demand_token()
        except MissingTelegramDemandContextError:
            with demand_context(DemandKind.TELETHON_UPDATE_DIFFERENCE):
                with acquisition_context(AcquisitionKind.UPDATE_DIFFERENCE):
                    return await self._call_registered(request, ordered=ordered)
        if token.kind is not DemandKind.TELETHON_UPDATE_DIFFERENCE:
            raise RuntimeError("Telethon update difference request inherited an incompatible demand root")
        require_execution_mode(ExecutionMode.PROTOCOL)
        with acquisition_context(AcquisitionKind.UPDATE_DIFFERENCE):
            return await self._call_registered(request, ordered=ordered)

    async def _call_registered(
        self,
        request: object,
        *,
        ordered: bool,
        connection_owned: bool = False,
    ) -> object:
        """Run one logical RPC after its root demand context is established."""
        scope = self._require_rpc_scope()
        self._check_draft_application(request, scope)
        actual = _application_request(request)
        scheduled_check = self._check_scheduled_application(actual, scope)
        if type(_application_request(request)) is functions.messages.SaveDraftRequest:
            return await self._call_with_source_policy(
                request, ordered=ordered, scope=scope, connection_owned=connection_owned
            )
        if scheduled_check is not None:
            return await self._call_with_source_policy(
                request, ordered=ordered, scope=scope, connection_owned=connection_owned
            )
        return await self._call_with_connection_retries(
            request, ordered=ordered, scope=scope, connection_owned=connection_owned
        )

    async def _call_with_connection_retries(
        self,
        request: object,
        *,
        ordered: bool,
        scope: TelegramRpcScope,
        connection_owned: bool,
    ) -> object:
        retry_delays = self._retry_delays_for_call(connection_owned)
        migration_dc: int | None = None
        delay = 0.0
        while True:
            try:
                if migration_dc is not None:
                    await self._connect_with_retry_slots(
                        initial_dc=migration_dc, retry_delays=retry_delays, initial_delay=delay
                    )
                    migration_dc = None
                return await self._call_with_source_policy(
                    request,
                    ordered=ordered,
                    scope=scope,
                    connection_owned=connection_owned,
                )
            except _ConnectionDcMigrationRequestedError as exc:
                if connection_owned:
                    raise
                migration_dc = exc.dc_id
                delay = self._next_migration_delay(retry_delays)
                self._record_rpc_retry(scope, request, reason="dc_migration")
            except TransientRpcErrors:
                next_delay = next(retry_delays, None)
                if next_delay is None:
                    raise
                delay = next_delay
                self._record_rpc_retry(scope, request, reason="server_transient")
                if delay and migration_dc is None:
                    await asyncio.sleep(delay)

    def _record_rpc_retry(self, scope: TelegramRpcScope, request: object, *, reason: str) -> None:
        observed_scope = scope
        try:
            method = _canonical_wire_method(request)
            if scope.attempt_budget is None and scope.attempt_evidence is not None:
                observed_scope = replace(scope, attempt_budget=self._wire_budgets.get(scope.attempt_evidence))
        except Exception:  # noqa: BLE001 - diagnostic method resolution cannot change retry policy
            method = None
        self._admission_scheduler.record_retry(observed_scope, reason=reason, request_method=method)

    def _retry_delays_for_call(self, connection_owned: bool) -> Iterator[float]:
        if not connection_owned:
            return iter(self._transient_retry_delays)
        assert self._connection_retry_delays is not None
        return self._connection_retry_delays

    def _require_rpc_scope(self) -> TelegramRpcScope:
        try:
            return current_rpc_scope()
        except MissingTelegramDemandContextError as exc:
            self._admission_scheduler.record_unclassified()
            raise UnclassifiedTelegramRpcError("Telegram RPC has no explicit operation source") from exc
        except UnclassifiedTelegramRpcError:
            self._admission_scheduler.record_unclassified()
            raise

    async def _call_with_source_policy(
        self,
        request: object,
        *,
        ordered: bool,
        scope: TelegramRpcScope,
        connection_owned: bool,
    ) -> object:
        while True:
            self._raise_if_update_scope_expired(scope, connection_owned=connection_owned)
            try:
                return await self._dispatch_attempt(request, ordered=ordered, scope=scope)
            except (RpcAdmissionSaturatedError, RpcAdmissionExpiredError) as exc:
                await self._handle_admission_deferral(scope, exc, request=request, connection_owned=connection_owned)
            except TelegramRpcAdmissionDeferred:
                raise
            except TelegramRpcThrottled:
                raise
            except (FloodWaitError, FloodPremiumWaitError, FloodTestPhoneWaitError) as exc:
                await self._handle_flood_wait(scope, exc, connection_owned=connection_owned)

    def _raise_if_update_scope_expired(
        self,
        scope: TelegramRpcScope,
        *,
        connection_owned: bool,
    ) -> None:
        if connection_owned or scope.source is not TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE:
            return
        if scope.deadline is None or asyncio.get_running_loop().time() < scope.deadline:
            return
        self._admission_scheduler.record_expired_before_dispatch(scope, wait_seconds=0.0)
        raise RpcAdmissionExpiredError(scope, "Telegram RPC admission deadline elapsed")

    async def _disconnect_main_sender(self) -> object:
        """Coalesce every public/internal teardown under one transport owner."""
        self._reconnect_quiescing = True
        if self._main_disconnect is None:
            self._main_disconnect = asyncio.create_task(self._perform_main_disconnect(), context=Context())
        task = self._main_disconnect
        cancelled = False
        while not task.done():
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                cancelled = True
        result = task.result()
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _perform_main_disconnect(self) -> object:
        """Stop transport, then durably retain every unresolved written attempt."""
        await self._quiesce_reconnect_probe()
        self._transport_state = _TransportBoundaryState.DISCONNECTING
        self._wire_generation += 1
        self._admission_scheduler.notify_eligibility_changed()
        primary: BaseException | None = None
        result: object = None
        try:
            result = await self._main_sender.disconnect()
        except BaseException as error:  # noqa: BLE001 - drain all owners, then re-raise the original failure.
            primary = error
        try:
            await self._main_sender.settle_shutdown()
        except BaseException as error:  # noqa: BLE001 - drain all owners, then re-raise the original failure.
            primary = primary or error
        try:
            await self._drain_cooldown_saves()
        except BaseException as error:  # noqa: BLE001 - drain all owners, then re-raise the original failure.
            primary = primary or error
        if primary is not None:
            self._transport_state = _TransportBoundaryState.FAILED
            raise primary
        self._transport_state = _TransportBoundaryState.DISCONNECTED
        return result

    async def _handle_admission_deferral(
        self,
        scope: TelegramRpcScope,
        exc: RpcAdmissionError,
        *,
        request: object,
        connection_owned: bool,
    ) -> None:
        if not connection_owned and scope.source is TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE:
            if isinstance(exc, RpcAdmissionExpiredError):
                raise exc
            await self._retry_update_source(scope, request=request, reason=type(exc).__name__)
            return
        if isinstance(exc, RpcAdmissionExpiredError):
            raise self.admission_expired_deferral(scope.request_method) from None
        retry_seconds = self._scheduler_policy.admission_retry_seconds
        raise TelegramRpcAdmissionDeferred(
            retry_after_seconds=retry_seconds,
            latched=False,
            detail=f"Telegram is temporarily busy; retry in {retry_seconds:g}s",
            method_key=scope.request_method,
            reason="capacity" if isinstance(exc, RpcAdmissionSaturatedError) else None,
        ) from None

    def admission_expired_deferral(self, method_key: str | None) -> TelegramRpcAdmissionDeferred:
        """Translate expired local admission using the scheduler's existing retry policy."""
        retry_seconds = self._scheduler_policy.admission_retry_seconds
        return TelegramRpcAdmissionDeferred(
            retry_after_seconds=retry_seconds,
            latched=False,
            detail=f"Telegram is temporarily busy; retry in {retry_seconds:g}s",
            method_key=method_key,
            eligible_at=time.monotonic() + retry_seconds,
            reason="admission_expired",
        )

    async def _retry_update_source(self, scope: TelegramRpcScope, *, request: object, reason: str) -> None:
        self._record_rpc_retry(scope, request, reason=reason)
        delay = self._scheduler_policy.update_loop_retry_seconds
        if scope.source is TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE and scope.deadline is not None:
            delay = min(delay, max(0.0, scope.deadline - asyncio.get_running_loop().time()))
        await asyncio.sleep(delay)

    async def _handle_flood_wait(
        self,
        scope: TelegramRpcScope,
        exc: BaseException,
        *,
        connection_owned: bool,
    ) -> None:
        request_rejected = self._flood_attempt(exc).get("dispatch_kind") == "actual_send"
        try:
            seconds = await self._observe_flood(exc, scope=scope)
        except TelegramRpcThrottled as outcome:
            if request_rejected:
                outcome.request_rejected = True
            raise
        raise TelegramRpcThrottled(
            retry_after_seconds=seconds,
            latched=False,
            detail=f"Telegram RPC throttled for {seconds}s",
            request_rejected=request_rejected,
        ) from exc

    async def _dispatch_attempt(
        self,
        request: object,
        *,
        ordered: bool,
        scope: TelegramRpcScope,
    ) -> object:
        """Dispatch one Telethon attempt through the admission-aware sender.

        Keeping this seam separate from the source policy makes the boundary
        explicit and lets focused callers exercise admission translation
        without constructing a live Telethon sender.
        """
        self._raise_if_transport_unavailable()
        self.check_circuit()
        await self._wait_for_account_cooldown(scope)
        self.check_circuit()
        sender = _AdmissionAwareSender(self, scope)
        task = asyncio.current_task()
        assert task is not None
        self._vendor_call_tasks.add(task)
        try:
            return await super()._call(sender, request, ordered=ordered)  # type: ignore[misc]
        except (FloodWaitError, FloodPremiumWaitError, FloodTestPhoneWaitError) as exc:
            vendor_cache = self._is_vendor_cache_deferral(exc, request, sender)
            if vendor_cache:
                _annotate_flood_attempt(
                    exc,
                    request_method=_canonical_wire_method(request),
                    admission_sequence=None,
                    dispatch_at_monotonic=None,
                    dispatch_kind="vendor_cache",
                )
                self._observe_rpc_request(
                    scope,
                    phase="vendor_cached",
                    request_method=_canonical_wire_method(request),
                    reason="flood_wait",
                )
            raise
        finally:
            self._vendor_call_tasks.discard(task)

    def _is_vendor_cache_deferral(
        self,
        exc: BaseException,
        request: object,
        sender: _AdmissionAwareSender,
    ) -> bool:
        if sender._dispatch_started or getattr(exc, "request", None) is not request:
            return False
        constructor_id = getattr(request, "CONSTRUCTOR_ID", None)
        if not isinstance(constructor_id, int):
            return False
        due = self._flood_waited_requests.get(constructor_id)
        return isinstance(due, (int, float)) and due > time.time()

    async def _update_loop(self) -> None:
        """Let each difference RPC and dispatched update establish its own root."""
        if self._authentication_mode:
            await self._authentication_update_loop()
            return
        if self._delivery_repository is not None:
            await self._delivery_ready.wait()
            self._require_delivery_open()
            if self._delivery_failure.is_set():
                raise RuntimeError("Telegram update admission is closed")
            prior = self._active_delivery
            if prior is not None:
                await prior.done.wait()
            self._require_delivery_open()
            if self._delivery_failure.is_set():
                raise RuntimeError("Telegram update admission is closed")
            assert self._delivery_account_id is not None
            self._restore_delivery_message_box(self._delivery_account_id)
        self._update_loop_owner = asyncio.current_task()
        try:
            await super()._update_loop()  # type: ignore[misc]
        finally:
            self._update_loop_owner = None

    async def _authentication_update_loop(self) -> None:
        """Deliver login-token notifications without fetching or saving history."""
        while self._main_sender.physical_connected():
            envelope = cast(object, await self._updates_queue.get())
            if type(envelope) in {types.Updates, types.UpdatesCombined}:
                candidates = cast(list[object], cast(types.Updates | types.UpdatesCombined, envelope).updates)
            elif type(envelope) is types.UpdateShort:
                candidates = [cast(types.UpdateShort, envelope).update]
            else:
                candidates = [envelope]
            for update in candidates:
                if type(update) is not types.UpdateLoginToken:
                    continue
                vars(update)["_entities"] = {}
                for builder, callback in tuple(self._event_builders):
                    # QRLogin.wait registers this exact local-only Raw builder.
                    # Generic dispatch would implicitly fetch GetMe first.
                    if type(builder) is Raw and builder.types is types.UpdateLoginToken and builder.func is None:
                        await callback(update)

    async def _handle_auto_reconnect(self) -> None:
        """Classify and signal Telethon's vendor reconnect probe."""
        self._reset_auto_resume_stability()
        if self._reconnect_quiescing:
            return
        if self._authentication_mode:
            await self._handle_authentication_reconnect()
            return
        try:
            current_demand_token()
        except UnclassifiedTelegramDemandError:
            pass
        else:
            raise RuntimeError("nested root demand is invalid; use acquisition_context for nested helpers")
        self.reconnect_event.set()
        if self._reconnect_quiescing:
            return

        async def run_vendor_probe() -> None:
            require_execution_mode(ExecutionMode.PROTOCOL)
            with acquisition_context(AcquisitionKind.ACCOUNT_SELF_PROFILE):
                await super(TelegramRpcGate, self)._handle_auto_reconnect()  # type: ignore[misc]

        task = self._reconnect_probe
        if task is None:
            task = create_scoped_rpc_task(
                run_vendor_probe(),
                source=TelegramRpcSource.TELETHON_RECONNECT_PROBE,
                name="telethon_reconnect_probe",
            )
            self._reconnect_probe = task
            task.add_done_callback(self._reconnect_probe_done)
        await asyncio.shield(task)

    async def _handle_authentication_reconnect(self) -> None:
        token = self._authentication_token
        if token is None:
            raise RuntimeError("authentication reconnect lacks its original owner")
        try:
            active = current_demand_token()
        except UnclassifiedTelegramDemandError:
            with transferred_demand_context(token):
                await super()._handle_auto_reconnect()  # type: ignore[misc]
        else:
            if active.kind is not DemandKind.OPERATOR_AUTH or active.attempt_evidence is not token.attempt_evidence:
                raise RuntimeError("authentication reconnect inherited another owner")
            await super()._handle_auto_reconnect()  # type: ignore[misc]

    def _reconnect_probe_done(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()
        if self._reconnect_probe is task:
            self._reconnect_probe = None
            self._reconnect_wire_owner = None

    async def _quiesce_reconnect_probe(self) -> bool:
        self._reconnect_quiescing = True
        task = self._reconnect_probe
        if task is None:
            return False
        owner = self._reconnect_wire_owner
        if owner is None or owner.claimed == 0:
            task.cancel()
        cancelled = False
        while not task.done():
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                cancelled = True
        return cancelled

    async def _dispatch_update(self, update: object) -> None:
        """Give one ordinary or replayed update its event-owned demand root."""
        active = self._active_delivery
        if self._delivery_repository is None:
            with demand_context(DemandKind.REALTIME_EVENT_ACQUISITION):
                await super()._dispatch_update(update)  # type: ignore[misc]
            return
        if self._delivery_closed:
            return
        if active is None or id(update) not in active.remaining:
            self._delivery_failure.set()
            raise RuntimeError("Telegram update has no durable admission")
        context = active.contexts[id(update)]
        try:
            with delivered_update_context(context), demand_context(DemandKind.REALTIME_EVENT_ACQUISITION):
                await super()._dispatch_update(update)  # type: ignore[misc]
        except BaseException:
            context.failed = True
            self._delivery_failure.set()
            raise
        finally:
            active.remaining.discard(id(update))
            if not active.remaining:
                await self._complete_update_delivery()

    def _scheduler_transport_ready(self) -> bool:
        return (
            self._transport_state in {_TransportBoundaryState.READY, _TransportBoundaryState.CONNECTING}
            and self._main_sender.physical_connected()
        )

    async def _wait_for_scheduler_transport(self) -> None:
        while not self._scheduler_transport_ready():
            await asyncio.sleep(self._scheduler_policy.admission_retry_seconds)

    def _begin_transport_connect(self) -> None:
        self._raise_if_transport_unavailable(allow_disconnected=True)
        if self._transport_state is not _TransportBoundaryState.DISCONNECTED:
            raise self._transport_unavailable()
        if self._scheduler_shutdown is not None:
            raise self._transport_unavailable()
        for teardown in (self._owned_disconnect, self._main_disconnect):
            if teardown is not None and (
                not teardown.done() or teardown.cancelled() or teardown.exception() is not None
            ):
                raise self._transport_unavailable()
        self._reconnect_quiescing = False
        self._owned_disconnect = None
        self._main_disconnect = None
        self._wire_generation += 1
        self._transport_state = _TransportBoundaryState.CONNECTING
        self._admission_scheduler.notify_eligibility_changed()

    def _finish_transport_connect(self) -> None:
        if self._transport_state is _TransportBoundaryState.CONNECTING:
            self._transport_state = _TransportBoundaryState.READY
            self._admission_scheduler.notify_eligibility_changed()

    def _finish_transport_connect_failure(self) -> None:
        if self._transport_state is _TransportBoundaryState.CONNECTING:
            self._transport_state = _TransportBoundaryState.FAILED

    def _raise_if_transport_unavailable(self, *, allow_disconnected: bool = False) -> None:
        self.check_circuit()
        reusable = {_TransportBoundaryState.READY}
        if allow_disconnected:
            reusable.add(_TransportBoundaryState.DISCONNECTED)
        if self._transport_state not in reusable:
            raise self._transport_unavailable()

    def _transport_unavailable(self) -> TelegramRpcAdmissionDeferred:
        retry_seconds = self._scheduler_policy.admission_retry_seconds
        return TelegramRpcAdmissionDeferred(
            retry_after_seconds=retry_seconds,
            latched=False,
            detail=f"Telegram transport is {self._transport_state.value}; retry in {retry_seconds:g}s",
        )

    async def _wait_for_account_cooldown(self, scope: TelegramRpcScope) -> None:
        """Wait before Telethon's cache path, bounded by this scope's deadline."""
        started = time.monotonic()
        deadline = scope.deadline
        if deadline is None:
            raise RuntimeError("Telegram RPC scope must carry its absolute admission deadline")
        while True:
            deferred = self._difference_admission_deferral(scope)
            if deferred is not None:
                raise deferred
            self.check_circuit()
            now = time.monotonic()
            remaining_scope_seconds = deadline - now
            if remaining_scope_seconds <= 0:
                waited = max(0.0, now - started)
                self._admission_scheduler.record_expired_before_dispatch(scope, wait_seconds=waited)
                raise RpcAdmissionExpiredError(scope, "Telegram RPC admission deadline elapsed")
            remaining = account_cooldown_deadline() - now
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, remaining_scope_seconds))
            self.check_circuit()
            if time.monotonic() >= deadline and account_cooldown_deadline() > time.monotonic():
                waited = max(0.0, time.monotonic() - started)
                self._admission_scheduler.record_expired_before_dispatch(scope, wait_seconds=waited)
                raise RpcAdmissionExpiredError(scope, "Telegram RPC admission deadline elapsed")

    async def _observe_flood(self, exc: BaseException, *, scope: TelegramRpcScope | None = None) -> int:
        """Latch immediately, then await the gate-owned durable receipt."""
        seconds, receipt = self._record_flood(exc, scope=scope)
        if receipt is not None:
            await asyncio.shield(receipt)
        return seconds

    def _record_flood(
        self, exc: BaseException, *, scope: TelegramRpcScope | None = None, wire_owner: _WireAttempt | None = None
    ) -> tuple[int, asyncio.Task[None] | None]:
        """Extend cooldown and record one terminal FloodWait without caller ownership."""
        seconds = flood_seconds(exc, default=self._fallback_wait_seconds)
        if self._flood_already_observed(exc):
            return seconds, self._cooldown_save_tasks.get(id(exc))
        now = time.monotonic()
        global _COOLDOWN_DEADLINE
        _COOLDOWN_DEADLINE = max(_COOLDOWN_DEADLINE, now + seconds + self._cooldown_buffer_seconds)
        attempt = self._flood_attempt(exc)
        self._mark_flood_observed(exc)
        try:
            self._observe_flood_warning(attempt, seconds)
        except Exception:
            logger.exception("flood_wait_warning_observer_failed")
        receipt = None
        deadline_utc = time.time() + max(0.0, _COOLDOWN_DEADLINE - now)
        if wire_owner is not None:
            method = wire_owner.method
            self._pending_method_receipts[method] = self._pending_method_receipts.get(method, 0) + 1
            receipt = asyncio.create_task(
                self._ensure_safety_ledger().complete(
                    wire_owner.attempt_id,
                    known_outcome=True,
                    floor_until_utc=deadline_utc,
                    circuit_open=self._rpc_circuit_status().open,
                    actual_flood=bool(wire_owner.claimed),
                )
            )
            receipt.add_done_callback(lambda _task: self._method_receipt_finished(wire_owner.method), context=Context())
        else:
            receipt = asyncio.create_task(
                self._ensure_safety_ledger().record_floor(deadline_utc, circuit_open=self._rpc_circuit_status().open)
            )
        self._cooldown_save_tasks[id(exc)] = receipt
        receipt.add_done_callback(lambda _task: self._cooldown_save_tasks.pop(id(exc), None), context=Context())
        self._observe_flood_event(scope, attempt, seconds)
        return seconds, receipt

    def _method_receipt_finished(self, method: str) -> None:
        remaining = self._pending_method_receipts[method] - 1
        if remaining:
            self._pending_method_receipts[method] = remaining
        else:
            self._pending_method_receipts.pop(method)
        self._notify_rpc_eligibility_changed(method)

    @staticmethod
    def _flood_already_observed(exc: BaseException) -> bool:
        return getattr(exc, "_mcp_telegram_flood_observed", False) or id(exc) in _OBSERVED_FLOOD_IDS

    @staticmethod
    def _mark_flood_observed(exc: BaseException) -> None:
        try:
            setattr(exc, "_mcp_telegram_flood_observed", True)  # noqa: B010 - exception marker is intentional
        except AttributeError, TypeError:
            _OBSERVED_FLOOD_IDS.add(id(exc))

    @staticmethod
    def _flood_attempt(exc: BaseException) -> dict[object, object]:
        attempt = getattr(exc, "_mcp_telegram_flood_attempt", {})
        return attempt if isinstance(attempt, dict) else {}

    def _observe_flood_warning(self, attempt: dict[object, object], seconds: int) -> None:
        if attempt.get("dispatch_kind", "unknown") != "vendor_cache" and self._flood_observer is not None:
            self._flood_observer(source="telegram_rpc_gate", seconds=seconds)

    def _observe_flood_event(
        self,
        scope: TelegramRpcScope | None,
        attempt: dict[object, object],
        seconds: int,
    ) -> None:
        observer = getattr(self, "_flood_event_observer", None)
        if scope is None or observer is None:
            return
        observation = self._flood_event(scope, attempt, seconds)
        logger.warning(
            "telegram_flood_wait source=%s demand=%s acquisition=%s request_method=%s "
            "origin=%s actual_dispatch=%s admission_sequence=%s seconds=%d cooldown_until_utc_ms=%d circuit_open=%s",
            scope.source.value,
            scope.demand_kind.value if scope.demand_kind is not None else "unknown",
            scope.acquisition_kind.value if scope.acquisition_kind is not None else "unknown",
            observation.request_method,
            observation.origin,
            observation.actual_dispatch,
            observation.admission_sequence,
            observation.seconds,
            observation.cooldown_until_utc_ms,
            observation.circuit_open,
        )
        try:
            observer(observation)
        except Exception:
            logger.exception("flood_wait_event_observer_failed source=%s", scope.source.value)

    def _flood_event(
        self,
        scope: TelegramRpcScope,
        attempt: dict[object, object],
        seconds: int,
    ) -> FloodWaitObservation:
        origin_value = attempt.get("dispatch_kind")
        if origin_value == "actual_send":
            origin = "actual_send"
        elif origin_value == "vendor_cache":
            origin = "vendor_cache"
        else:
            origin = "unknown"
        return FloodWaitObservation(
            source=scope.source.value,
            service_class=scope.service_class.value,
            demand_kind=scope.demand_kind.value if scope.demand_kind is not None else None,
            acquisition_kind=scope.acquisition_kind.value if scope.acquisition_kind is not None else None,
            seconds=seconds,
            cooldown_until_utc_ms=_cooldown_until_utc_ms(),
            circuit_open=self._rpc_circuit_status().open,
            request_method=str(attempt.get("request_method", "unknown")),
            origin=origin,
            actual_dispatch=origin == "actual_send" if origin != "unknown" else None,
            admission_sequence=cast(int | None, attempt.get("admission_sequence")),
            dispatch_at_monotonic=cast(float | None, attempt.get("dispatch_at_monotonic")),
            observed_at_ms=int(time.time() * 1_000),
        )


__all__ = [
    "RpcAdmissionError",
    "RpcAdmissionExpiredError",
    "RpcAdmissionSaturatedError",
    "TelegramRpcAdmissionDeferred",
    "TelegramRpcBudget",
    "TelegramRpcGate",
    "TelegramRpcMethodRecoveryPending",
    "TelegramRpcSafetyStore",
    "TelegramRpcSource",
    "TransientRpcErrors",
    "UnclassifiedTelegramRpcError",
    "account_cooldown_deadline",
    "current_rpc_scope",
    "rpc_attempt_budget",
    "rpc_scope",
]
