"""Account-wide admission control for the daemon's Telethon client."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import AbstractContextManager
from contextvars import Context
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from functools import wraps
from typing import Never, Protocol, cast

from aiolimiter import AsyncLimiter
from telethon import (  # type: ignore[import-untyped]
    TelegramClient,  # type: ignore[import-untyped]
    types,
)
from telethon._updates import ChannelState, SessionState  # type: ignore[import-untyped]
from telethon.errors import (  # type: ignore[import-untyped]
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
from telethon.events.common import EventBuilder  # type: ignore[import-untyped]
from telethon.tl.functions.channels import GetFullChannelRequest  # type: ignore[import-untyped]
from telethon.tl.functions.updates import (  # type: ignore[import-untyped]
    GetChannelDifferenceRequest,
    GetDifferenceRequest,
)
from telethon.utils import is_list_like  # type: ignore[import-untyped]

from .delivered_update import DeliveredUpdate, delivered_update_context, fail_delivered_update
from .flood import FloodWaitObservation, TelegramRpcThrottled, flood_seconds
from .request_timing import current_timing, timing_phase
from .telegram_delivery import DeliveryEnvelope, ProtocolSnapshot, load_delivery_envelope, load_protocol_snapshot
from .telegram_demand import (
    AcquisitionKind,
    DemandToken,
    MissingTelegramDemandContextError,
    RpcAttemptBudget,
    UnclassifiedTelegramDemandError,
    acquisition_context,
    create_demand_token,
    current_demand_token,
    demand_context,
    require_execution_mode,
)
from .telegram_rpc_consumers import DemandKind, ExecutionMode, demand_contract
from .telegram_rpc_scheduler import (
    AdmissionObserver,
    RpcAdmission,
    RpcAdmissionError,
    RpcAdmissionExpiredError,
    RpcAdmissionSaturatedError,
    RpcTransportReadiness,
    TelegramRpcAdmissionDeferred,
    TelegramRpcAdmissionScheduler,
    TelegramRpcSchedulerPolicy,
    TelegramRpcScope,
    TelegramRpcSource,
    UnclassifiedTelegramRpcError,
    create_scoped_rpc_task,
    current_rpc_scope,
    rpc_attempt_budget,
    rpc_scope,
)
from .update_delivery import UpdateDeliveryRepository

logger = logging.getLogger(__name__)


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


class _SendAttempt(Protocol):
    def __call__(
        self,
        request: object,
        *,
        ordered: bool = False,
    ) -> Awaitable[object] | list[asyncio.Future[object]]: ...


class _MainSender(_SendAttempt, Protocol):
    """The only raw Telethon sender surface retained by the gate."""

    auth_key: object

    def send(
        self,
        request: object,
        *,
        ordered: bool = False,
    ) -> Awaitable[object] | list[asyncio.Future[object]]: ...

    async def connect(self, connection: object) -> object: ...

    async def disconnect(self) -> object: ...

    def is_connected(self) -> bool: ...

    @property
    def disconnected(self) -> asyncio.Future[object]: ...

    def _transport_connected(self) -> bool: ...

    def _keepalive_ping(self, random_id: int) -> None: ...


class _AdmissionAwareSender:
    """Admit each scalar attempt at Telethon's synchronous sender seam."""

    def __init__(self, gate: TelegramRpcGate, send_attempt: _SendAttempt, scope: TelegramRpcScope) -> None:
        self._gate = gate
        self._send_attempt = send_attempt
        self._scope = scope
        self._dispatch_started = False

    def send(self, request: object, *, ordered: bool = False) -> Coroutine[object, object, object]:
        return self._send(request, ordered=ordered)

    async def _send(self, request: object, *, ordered: bool) -> object:
        while True:
            self._gate._raise_if_transport_unavailable()
            self._reject_exhausted_budget()
            admission = await self._admit()
            completion_transferred = False
            try:
                if not self._gate._scheduler_transport_ready():
                    self._gate._admission_scheduler.record_retry(
                        self._scope,
                        reason="transport_readiness_changed",
                    )
                    continue
                self._raise_if_cancel_pending()
                self._debit_dispatched_attempt()
                self._gate._admission_scheduler.record_dispatch(admission)
                if isinstance(request, GetFullChannelRequest):
                    self._gate._observe_rpc_request(self._scope)
                if (timing := current_timing()) is not None:
                    timing.record_rpc_attempt()
                raw_future = self._start_dispatched_send(request, ordered=ordered, admission=admission)
                completion_transferred = True
                with timing_phase("rpc_execution"):
                    async with asyncio.timeout_at(self._scope.deadline):
                        await asyncio.wait({raw_future})
                    return raw_future.result()
            finally:
                if not completion_transferred:
                    self._gate._admission_scheduler.complete(admission)

    def _reject_exhausted_budget(self) -> None:
        budget = self._scope.attempt_budget
        if budget is not None and budget.exhausted:
            self._gate._admission_scheduler.record_attempt_budget_exhausted(self._scope)
            budget.debit()

    async def _admit(self) -> RpcAdmission:
        with timing_phase("rpc_admission"):
            return await self._gate._admit(self._scope)

    def _debit_dispatched_attempt(self) -> None:
        budget = self._scope.attempt_budget
        if budget is not None:
            self._reject_exhausted_budget()
            budget.debit()

    def _start_dispatched_send(
        self,
        request: object,
        *,
        ordered: bool,
        admission: RpcAdmission,
    ) -> asyncio.Future[object]:
        self._raise_if_cancel_pending()
        dispatch_at_monotonic = time.monotonic()
        self._dispatch_started = True
        raw_awaitable = self._send_attempt(request, ordered=ordered)
        if isinstance(raw_awaitable, list):
            self._reject_batch(raw_awaitable)
        raw_future = asyncio.ensure_future(raw_awaitable)
        self._gate._register_scalar_dispatch_completion(
            raw_future,
            scope=self._scope,
            request_method=type(request).__name__,
            admission=admission,
            dispatch_at_monotonic=dispatch_at_monotonic,
        )
        return raw_future

    @staticmethod
    def _raise_if_cancel_pending() -> None:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError

    @staticmethod
    def _reject_batch(futures: list[asyncio.Future[object]]) -> Never:
        for future in futures:
            future.cancel()
        raise RuntimeError("Telegram sender returned a future batch for a scalar request")


@dataclass(frozen=True, slots=True)
class _ConnectionCapability:
    """Explicit identity carried into Telethon's context-free connect task."""

    token: DemandToken
    deadline: float
    attempt_budget: RpcAttemptBudget | None


class _ConnectionDcMigrationRequestedError(Exception):
    """Unwind a connection-owned RPC before Telethon mutates its transport."""

    def __init__(self, dc_id: int) -> None:
        self.dc_id = dc_id


class _TransportBoundaryState(StrEnum):
    """Whether the admitted main transport is reusable or unresolved."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    READY = "ready"
    DISCONNECTING = "disconnecting"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class _PendingScalarDispatch:
    """Frozen completion ownership for one dispatched scalar request."""

    scope: TelegramRpcScope
    request_method: str
    admission: RpcAdmission
    dispatch_at_monotonic: float


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

    @property
    def enabled(self) -> bool:
        return self.max_calls_per_period > 0


@dataclass(frozen=True, slots=True)
class TelegramRpcCooldownPersistence:
    """Read-only startup load and asynchronous durable cooldown save."""

    load_until_utc: Callable[[], float | None]
    save_until_utc: Callable[[float], Awaitable[None]]

    def __post_init__(self) -> None:
        if not callable(self.load_until_utc):
            raise TypeError("load_until_utc must be callable")
        if not callable(self.save_until_utc):
            raise TypeError("save_until_utc must be callable")


_COOLDOWN_DEADLINE = 0.0
_OBSERVED_FLOOD_IDS: set[int] = set()


def account_cooldown_deadline() -> float:
    """Return the current process-wide monotonic cooldown deadline."""
    return _COOLDOWN_DEADLINE


def reset_account_cooldown() -> None:
    """Reset process policy for isolated tests and process startup."""
    global _COOLDOWN_DEADLINE
    _COOLDOWN_DEADLINE = 0.0
    _OBSERVED_FLOOD_IDS.clear()


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


class TelegramRpcGate(TelegramClient):
    """A Telethon client with account-global admission and transient retry.

    Every admitted attempt consumes one limiter acquisition. Flood errors
    extend the account cooldown atomically and are re-raised immediately;
    application retry is limited to the configured server-transient taxonomy.
    An RPC already admitted or in flight may finish after a later FloodWait is
    observed: this gate makes no stronger claim about cancellation of work.
    """

    def __init__(  # noqa: PLR0913 - Telethon constructor plus explicit account policy
        self,
        *args: object,
        rpc_budget: TelegramRpcBudget,
        circuit_status: Callable[[], CircuitStatus],
        fallback_wait_seconds: int,
        cooldown_buffer_seconds: float,
        transient_retry_delays_seconds: tuple[float, ...],
        scheduler_policy: TelegramRpcSchedulerPolicy,
        admission_observer: AdmissionObserver | None = None,
        rpc_request_observer: Callable[..., None] | None = None,
        flood_observer: Callable[..., None] | None = None,
        cooldown_persistence: TelegramRpcCooldownPersistence | None = None,
        **kwargs: object,
    ) -> None:
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
        self._cooldown_persistence = cooldown_persistence
        self._cooldown_save_tasks: dict[int, asyncio.Task[None]] = {}
        self._scalar_persistence_pending: set[asyncio.Future[object]] = set()
        self._restore_account_cooldown()
        self._scheduler_policy = scheduler_policy
        self._limiter = (
            AsyncLimiter(rpc_budget.max_calls_per_period, rpc_budget.period_seconds) if rpc_budget.enabled else None
        )
        self._admission_scheduler = TelegramRpcAdmissionScheduler(
            policy=self._scheduler_policy,
            limiter=self._limiter,
            observer=admission_observer,
            readiness=RpcTransportReadiness(
                probe=self._scheduler_transport_ready,
                wait=self._wait_for_scheduler_transport,
            ),
        )
        self._main_sender = cast(_MainSender, object.__getattribute__(self, "_sender"))
        self._sender = _MainSenderAdapter(self)
        self._connect_owner: asyncio.Task[object] | object | None = None
        self._connection_capability: _ConnectionCapability | None = None
        self._connection_rpc_tasks: set[asyncio.Task[object]] = set()
        self._pending_scalar_dispatches: dict[asyncio.Future[object], _PendingScalarDispatch] = {}
        self._transport_state = _TransportBoundaryState.DISCONNECTED
        self._delivery_repository: UpdateDeliveryRepository | None = None
        self._delivery_ready = asyncio.Event()
        self._delivery_failure = asyncio.Event()
        self._delivery_safe: ProtocolSnapshot | None = None
        self._active_delivery: _ActiveDelivery | None = None
        self._delivery_callbacks: dict[object, object] = {}

    def configure_update_delivery(self, repository: UpdateDeliveryRepository) -> None:
        """Hold the vendor loop before connect; auth RPC remains available."""
        if self._delivery_repository is not None or self.is_connected():
            raise RuntimeError("update delivery must be configured once before connect")
        self._delivery_repository = repository

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
        repository = self._delivery_repository
        if repository is None:
            raise RuntimeError("update delivery repository is not configured")
        state = await repository.bind(account_id, self._capture_delivery_snapshot([]).model_dump_json())
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
        repository = self._delivery_repository
        if repository is None:
            return cast(list[object], await super()._preprocess_updates(updates, users, chats))
        # Capture before either await: the vendor head can change while the prior callbacks finish.
        envelope = DeliveryEnvelope.capture(self._capture_delivery_snapshot(chats), time.time(), updates, users, chats)
        prior = self._active_delivery
        if prior is not None:
            await prior.done.wait()
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

    async def _complete_update_delivery(self) -> None:
        active = self._active_delivery
        repository = self._delivery_repository
        assert active is not None and repository is not None and self.session is not None
        try:
            if any(context.failed for context in active.contexts.values()):
                self._delivery_failure.set()
                return
            snapshot = active.envelope.checkpoint_after
            for channel_id, access_hash in snapshot.channel_hashes.items():
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
            await self._save_states_and_entities()
            self.session.save()
            self._active_delivery = None
        except BaseException:
            self._delivery_failure.set()
            raise
        finally:
            active.done.set()

    async def _save_states_and_entities(self) -> None:
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
        acquisition_kind: AcquisitionKind | None = None,
    ) -> AbstractContextManager[TelegramRpcScope]:
        """Return a source scope for application helpers using this client."""
        return rpc_scope(
            source,
            deadline=deadline,
            timeout_seconds=timeout_seconds,
            acquisition_kind=acquisition_kind,
        )

    async def close_rpc_scheduler(self) -> None:
        """Cancel queued admission work during final daemon shutdown."""
        try:
            await self._admission_scheduler.close()
        finally:
            await self._drain_cooldown_saves()

    async def _drain_cooldown_saves(self) -> None:
        """Keep durable FloodWait receipts owned until shutdown can close storage."""
        cancelled = False
        while self._cooldown_save_tasks:
            receipts = asyncio.gather(*tuple(self._cooldown_save_tasks.values()))
            while not receipts.done():
                try:
                    await asyncio.wait({receipts})
                except asyncio.CancelledError:
                    cancelled = True
            receipts.result()
        if cancelled:
            raise asyncio.CancelledError

    def set_rpc_admission_observer(self, observer: AdmissionObserver | None) -> None:
        """Attach the daemon-owned operational telemetry sink."""
        self._admission_scheduler.set_observer(observer)

    def set_rpc_request_observer(self, observer: Callable[..., None] | None) -> None:
        """Attach bounded per-request-class attempt telemetry."""
        self._rpc_request_observer = observer

    def set_flood_event_observer(self, observer: Callable[[FloodWaitObservation], None] | None) -> None:
        """Attach content-free FloodWait telemetry owned by the daemon."""
        self._flood_event_observer = observer

    @property
    def reconnect_event(self) -> asyncio.Event:
        """Signal Telethon's internal reconnect handler to daemon recovery."""
        return self._reconnect_event

    def _observe_rpc_request(self, scope: TelegramRpcScope) -> None:
        observer = self._rpc_request_observer
        if observer is None:
            return
        try:
            observer(
                request_class="get_full_channel",
                source=scope.source,
                service_class=scope.service_class,
                demand_kind=scope.demand_kind,
                acquisition_kind=scope.acquisition_kind,
            )
        except Exception:
            logger.debug("telegram_rpc_request_observation_failed", exc_info=True)

    def check_circuit(self) -> None:
        status = self._rpc_circuit_status()
        if status.open:
            raise TelegramRpcThrottled(
                retry_after_seconds=None,
                latched=True,
                detail=status.detail(),
            )

    def _restore_account_cooldown(self) -> None:
        """Translate a persisted UTC deadline into current monotonic time."""
        persistence = self._cooldown_persistence
        if persistence is None:
            return
        persisted = persistence.load_until_utc()
        if persisted is None:
            return
        deadline_utc = _validate_cooldown_until_utc(persisted)
        remaining = deadline_utc - time.time()
        if remaining <= 0:
            return
        global _COOLDOWN_DEADLINE
        _COOLDOWN_DEADLINE = max(_COOLDOWN_DEADLINE, time.monotonic() + remaining)

    async def _persist_account_cooldown(self, deadline_utc: float) -> None:
        """Persist the effective process deadline without changing FloodWait semantics."""
        persistence = cast(
            TelegramRpcCooldownPersistence | None,
            getattr(self, "_cooldown_persistence", None),
        )
        if persistence is None:
            return
        try:
            await persistence.save_until_utc(deadline_utc)
        except Exception:
            logger.exception("telegram_rpc_cooldown_persist_failed")

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
        if isinstance(request, (GetDifferenceRequest, GetChannelDifferenceRequest)):
            return await self._call_update_difference(request, ordered=ordered)
        return await self._call_registered(request, ordered=ordered)

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
                await super().connect()  # type: ignore[misc]
                return
            raise self._connection_owner_deferred()
        self.check_circuit()
        if self._transport_state is _TransportBoundaryState.READY:
            if self._main_sender.is_connected():
                return
            raise self._transport_unavailable()
        self._raise_if_transport_unavailable(allow_disconnected=True)
        capability = self._capture_connection_capability()
        scope = self._scope_for_connection_capability(capability)
        self._connection_capability = capability
        reservation = object()
        self._connect_owner = reservation
        owner: asyncio.Task[object] | None = None
        release_reservation = False
        try:
            await self._wait_for_account_cooldown(scope)
            self.check_circuit()
            owner = self._start_connection_owner(scope)
            self._connect_owner = owner
            await owner
            release_reservation = True
        except BaseException as bootstrap_error:
            release_reservation, cancellation_arrived = await self._finish_connection_failure(owner, bootstrap_error)
            if cancellation_arrived:
                raise asyncio.CancelledError from None
            raise
        finally:
            if release_reservation and (self._connect_owner is reservation or self._connect_owner is owner):
                self._connect_owner = None
                self._connection_capability = None
                self._connection_rpc_tasks.clear()

    def _start_connection_owner(self, scope: TelegramRpcScope) -> asyncio.Task[object]:
        async def run_vendor_connect() -> None:
            migration_dc: int | None = None
            for retry_index, retry_delay in enumerate((0.0, *self._transient_retry_delays)):
                try:
                    if migration_dc is None:
                        await super(TelegramRpcGate, self).connect()  # type: ignore[misc]
                    else:
                        await self._continue_connection_migration(scope, migration_dc, retry_delay)
                    return
                except _ConnectionDcMigrationRequestedError as exc:
                    migration_dc = exc.dc_id
                    if retry_index >= len(self._transient_retry_delays):
                        raise self._connection_redirect_exhausted() from None

        return asyncio.get_running_loop().create_task(
            run_vendor_connect(),
            name="telethon_connection_bootstrap",
            context=Context(),
        )

    async def _finish_connection_failure(
        self,
        owner: asyncio.Task[object] | None,
        bootstrap_error: BaseException,
    ) -> tuple[bool, bool]:
        if owner is None:
            return True, False
        cancelled, cleanup_error = await self._complete_failed_connection_cleanup()
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

    async def _continue_connection_migration(
        self,
        scope: TelegramRpcScope,
        dc_id: int,
        retry_delay: float,
    ) -> None:
        """Resume one unwound migration without refreshing connection identity."""
        if retry_delay:
            await asyncio.sleep(retry_delay)
        self.check_circuit()
        await self._wait_for_account_cooldown(scope)
        self.check_circuit()
        await super()._switch_dc(dc_id)  # type: ignore[misc]

    async def _cleanup_failed_connection_bootstrap(self) -> None:
        """Cancel owned sends, then let Telethon clean up the partial transport."""
        tasks = tuple(self._connection_rpc_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._connection_rpc_tasks.clear()
        await super()._disconnect()  # type: ignore[misc]

    async def _complete_failed_connection_cleanup(self) -> tuple[bool, BaseException | None]:
        """Keep the connection reservation until a cancellation-safe cleanup finishes."""
        cleanup = asyncio.get_running_loop().create_task(
            self._cleanup_failed_connection_bootstrap(),
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
        """Prevent concurrent migration from mutating a guarded bootstrap transport."""
        owner = self._connect_owner
        if owner is not None and self._connection_capability is not None:
            current_task = asyncio.current_task()
            if current_task in self._connection_rpc_tasks:
                raise _ConnectionDcMigrationRequestedError(new_dc)
            if current_task is not owner:
                raise self._connection_owner_deferred()
        self._raise_if_transport_unavailable()
        await super()._switch_dc(new_dc)  # type: ignore[misc]

    def _capture_connection_capability(self) -> _ConnectionCapability:
        """Capture an existing root or create the sole protocol bootstrap root."""
        try:
            token = current_demand_token()
        except MissingTelegramDemandContextError:
            token = replace(
                create_demand_token(DemandKind.TELETHON_CONNECTION_BOOTSTRAP),
                acquisition_kind=AcquisitionKind.CONNECTION_BOOTSTRAP,
            )
            return _ConnectionCapability(token, token.admission_deadline, None)
        return self._connection_capability_from_scope(token, current_rpc_scope())

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
        except RpcAdmissionExpiredError as exc:
            if not connection_owned and exc.source is TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE:
                raise OSError("Telegram update difference admission deadline elapsed") from exc
            raise

    async def _call_registered(
        self,
        request: object,
        *,
        ordered: bool,
        connection_owned: bool = False,
    ) -> object:
        """Run one logical RPC after its root demand context is established."""
        scope = self._require_rpc_scope()
        retry_delays = self._transient_retry_delays
        for retry_index, delay in enumerate((0.0, *retry_delays)):
            if retry_index and delay:
                await asyncio.sleep(delay)
            try:
                return await self._call_with_source_policy(
                    request,
                    ordered=ordered,
                    scope=scope,
                    connection_owned=connection_owned,
                )
            except TransientRpcErrors:
                if retry_index >= len(retry_delays):
                    raise
                self._admission_scheduler.record_retry(scope, reason="server_transient")
        raise AssertionError("unreachable")

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
                await self._handle_admission_deferral(scope, exc, connection_owned=connection_owned)
            except TelegramRpcAdmissionDeferred:
                raise
            except TelegramRpcThrottled:
                if connection_owned or scope.source is not TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE:
                    raise
                await self._retry_update_source(scope, reason="account_circuit")
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

    def _send_real_sender(
        self,
        request: object,
        *,
        ordered: bool = False,
    ) -> Awaitable[object] | list[asyncio.Future[object]]:
        return cast(_SendAttempt, self._main_sender.send)(request, ordered=ordered)

    def _register_scalar_dispatch_completion(
        self,
        raw_future: asyncio.Future[object],
        *,
        scope: TelegramRpcScope,
        request_method: str,
        admission: RpcAdmission,
        dispatch_at_monotonic: float,
    ) -> None:
        """Transfer one dispatched scalar response and its lease to the gate."""
        self._pending_scalar_dispatches[raw_future] = _PendingScalarDispatch(
            scope=scope,
            request_method=request_method,
            admission=admission,
            dispatch_at_monotonic=dispatch_at_monotonic,
        )

        def finalize(future: asyncio.Future[object]) -> None:
            self._finalize_scalar_dispatch(future)

        raw_future.add_done_callback(finalize, context=Context())
        if raw_future.done():
            finalize(raw_future)

    def _finalize_scalar_dispatch(self, raw_future: asyncio.Future[object]) -> None:
        """Record terminal actual-send outcomes and release their admission once."""
        pending = self._pending_scalar_dispatches.get(raw_future)
        if pending is None or raw_future in self._scalar_persistence_pending:
            return
        if raw_future.cancelled():
            self._settle_unconfirmed_scalar_dispatch(raw_future, pending)
            return
        exc = raw_future.exception()
        if exc is not None and not isinstance(exc, RPCError):
            self._settle_unconfirmed_scalar_dispatch(raw_future, pending)
            return
        if isinstance(exc, (FloodWaitError, FloodPremiumWaitError, FloodTestPhoneWaitError)):
            _annotate_flood_attempt(
                exc,
                request_method=pending.request_method,
                admission_sequence=pending.admission.sequence,
                dispatch_at_monotonic=pending.dispatch_at_monotonic,
                dispatch_kind="actual_send",
            )
            _, receipt = self._record_flood(exc, scope=pending.scope)
            if receipt is not None:
                self._scalar_persistence_pending.add(raw_future)

                def release(_receipt: asyncio.Task[None]) -> None:
                    self._scalar_persistence_pending.discard(raw_future)
                    self._release_scalar_dispatch(raw_future, pending)

                receipt.add_done_callback(release, context=Context())
                return
        self._release_scalar_dispatch(raw_future, pending)

    def _settle_unconfirmed_scalar_dispatch(
        self, raw_future: asyncio.Future[object], pending: _PendingScalarDispatch
    ) -> None:
        if self._transport_state is _TransportBoundaryState.DISCONNECTED:
            self._release_scalar_dispatch(raw_future, pending)
        elif self._transport_state is not _TransportBoundaryState.DISCONNECTING:
            self._transport_state = _TransportBoundaryState.FAILED

    def _release_scalar_dispatch(
        self,
        raw_future: asyncio.Future[object],
        pending: _PendingScalarDispatch,
    ) -> None:
        """Release an admission only after a response or confirmed teardown."""
        if self._pending_scalar_dispatches.pop(raw_future, None) is not None:
            self._admission_scheduler.complete(pending.admission)

    async def _disconnect_main_sender(self) -> object:
        """Terminate raw transport, then settle every gate-owned scalar response."""
        if self._transport_state in {_TransportBoundaryState.FAILED, _TransportBoundaryState.DISCONNECTING}:
            raise self._transport_unavailable()
        if self._transport_state is _TransportBoundaryState.DISCONNECTED:
            return None
        try:
            connected = self._main_sender.is_connected() and self._main_sender._transport_connected()
        except Exception:  # noqa: BLE001 - a sender-probe failure is unconfirmed termination evidence.
            connected = False
        if not connected:
            self._transport_state = _TransportBoundaryState.FAILED
            raise self._transport_unavailable()
        self._transport_state = _TransportBoundaryState.DISCONNECTING
        try:
            result = await self._main_sender.disconnect()
        except BaseException:
            self._transport_state = _TransportBoundaryState.FAILED
            raise
        pending = tuple(self._pending_scalar_dispatches)
        for raw_future in pending:
            if not raw_future.done():
                raw_future.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._transport_state = _TransportBoundaryState.DISCONNECTED
        self._release_confirmed_teardown_dispatches()
        await self._drain_cooldown_saves()
        return result

    def _release_confirmed_teardown_dispatches(self) -> None:
        """A confirmed raw teardown is the sole fallback completion evidence."""
        for raw_future, _pending in tuple(self._pending_scalar_dispatches.items()):
            if raw_future.done() and not raw_future.cancelled():
                self._finalize_scalar_dispatch(raw_future)
            remaining = self._pending_scalar_dispatches.get(raw_future)
            if remaining is not None and raw_future not in self._scalar_persistence_pending:
                self._release_scalar_dispatch(raw_future, remaining)

    async def _handle_admission_deferral(
        self,
        scope: TelegramRpcScope,
        exc: RpcAdmissionError,
        *,
        connection_owned: bool,
    ) -> None:
        if not connection_owned and scope.source is TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE:
            if isinstance(exc, RpcAdmissionExpiredError):
                raise exc
            await self._retry_update_source(scope, reason=type(exc).__name__)
            return
        retry_seconds = self._scheduler_policy.admission_retry_seconds
        raise TelegramRpcAdmissionDeferred(
            retry_after_seconds=retry_seconds,
            latched=False,
            detail=f"Telegram is temporarily busy; retry in {retry_seconds:g}s",
        ) from None

    async def _retry_update_source(self, scope: TelegramRpcScope, *, reason: str) -> None:
        self._admission_scheduler.record_retry(scope, reason=reason)
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
        seconds = await self._observe_flood(exc, scope=scope)
        if not connection_owned and scope.source is TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE:
            self._admission_scheduler.record_retry(scope, reason="flood_wait")
            return
        raise TelegramRpcThrottled(
            retry_after_seconds=seconds,
            latched=False,
            detail=f"Telegram RPC throttled for {seconds}s",
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
        sender = _AdmissionAwareSender(self, self._send_real_sender, scope)
        try:
            return await super()._call(sender, request, ordered=ordered)  # type: ignore[misc]
        except (FloodWaitError, FloodPremiumWaitError, FloodTestPhoneWaitError) as exc:
            vendor_cache = self._is_vendor_cache_deferral(exc, request, sender)
            _annotate_flood_attempt(
                exc,
                request_method=type(request).__name__ if vendor_cache else "unknown",
                admission_sequence=None,
                dispatch_at_monotonic=None,
                dispatch_kind="vendor_cache" if vendor_cache else "unknown",
            )
            raise

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

    async def _admit(self, scope: TelegramRpcScope) -> RpcAdmission:
        self.check_circuit()
        return await self._admission_scheduler.admit(scope)

    async def _update_loop(self) -> None:
        """Let each difference RPC and dispatched update establish its own root."""
        if self._delivery_repository is not None:
            await self._delivery_ready.wait()
        await super()._update_loop()  # type: ignore[misc]

    async def _handle_auto_reconnect(self) -> None:
        """Classify and signal Telethon's vendor reconnect probe."""
        try:
            current_demand_token()
        except UnclassifiedTelegramDemandError:
            pass
        else:
            raise RuntimeError("nested root demand is invalid; use acquisition_context for nested helpers")
        self.reconnect_event.set()

        async def run_vendor_probe() -> None:
            require_execution_mode(ExecutionMode.PROTOCOL)
            with acquisition_context(AcquisitionKind.ACCOUNT_SELF_PROFILE):
                await super(TelegramRpcGate, self)._handle_auto_reconnect()  # type: ignore[misc]

        task = create_scoped_rpc_task(
            run_vendor_probe(),
            source=TelegramRpcSource.TELETHON_RECONNECT_PROBE,
            name="telethon_reconnect_probe",
        )
        await task

    async def _dispatch_update(self, update: object) -> None:
        """Give one ordinary or replayed update its event-owned demand root."""
        active = self._active_delivery
        if self._delivery_repository is None:
            with demand_context(DemandKind.REALTIME_EVENT_ACQUISITION):
                await super()._dispatch_update(update)  # type: ignore[misc]
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
            self._transport_state is _TransportBoundaryState.READY
            and not self._rpc_circuit_status().open
            and account_cooldown_deadline() <= time.monotonic()
        )

    async def _wait_for_scheduler_transport(self) -> None:
        while not self._scheduler_transport_ready():
            status = self._rpc_circuit_status()
            delay: float
            if self._transport_state is not _TransportBoundaryState.READY:
                delay = self._scheduler_policy.admission_retry_seconds
            elif status.open:
                delay = self._scheduler_policy.update_loop_retry_seconds
            else:
                delay = max(account_cooldown_deadline() - time.monotonic(), 0.0)
            await asyncio.sleep(delay)

    def _begin_transport_connect(self) -> None:
        self._raise_if_transport_unavailable(allow_disconnected=True)
        if self._transport_state is not _TransportBoundaryState.DISCONNECTED:
            raise self._transport_unavailable()
        self._transport_state = _TransportBoundaryState.CONNECTING

    def _finish_transport_connect(self) -> None:
        if self._transport_state is _TransportBoundaryState.CONNECTING:
            self._transport_state = _TransportBoundaryState.READY

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
        self, exc: BaseException, *, scope: TelegramRpcScope | None = None
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
        if self._cooldown_persistence is not None:
            deadline_utc = time.time() + max(0.0, _COOLDOWN_DEADLINE - now)
            receipt = asyncio.create_task(self._persist_account_cooldown(deadline_utc))
            self._cooldown_save_tasks[id(exc)] = receipt
            receipt.add_done_callback(lambda _task: self._cooldown_save_tasks.pop(id(exc), None), context=Context())
        self._observe_flood_event(scope, attempt, seconds)
        return seconds, receipt

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
    "TelegramRpcCooldownPersistence",
    "TelegramRpcGate",
    "TelegramRpcSource",
    "TransientRpcErrors",
    "UnclassifiedTelegramRpcError",
    "account_cooldown_deadline",
    "current_rpc_scope",
    "reset_account_cooldown",
    "rpc_attempt_budget",
    "rpc_scope",
]
