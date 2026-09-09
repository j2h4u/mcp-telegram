"""Account-wide admission control for the daemon's Telethon client."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Never, Protocol, cast

from aiolimiter import AsyncLimiter
from telethon import TelegramClient  # type: ignore[import-untyped]
from telethon.errors import (  # type: ignore[import-untyped]
    FloodPremiumWaitError,
    FloodTestPhoneWaitError,
    FloodWaitError,
    InterdcCallErrorError,
    InterdcCallRichErrorError,
    RpcCallFailError,
    RpcMcgetFailError,
    ServerError,
    TimedOutError,
)
from telethon.utils import is_list_like  # type: ignore[import-untyped]

from .flood import TelegramRpcThrottled, flood_seconds
from .telegram_demand import AcquisitionKind
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
    current_rpc_scope,
    rpc_attempt_budget,
    rpc_scope,
)

logger = logging.getLogger(__name__)


def raise_if_flood_wait_error(error: BaseException) -> None:
    """Re-raise vendor FloodWait outcomes before application catches."""
    if isinstance(error, FloodWaitError):
        raise error


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


class _AdmissionAwareSender:
    """Admit each scalar attempt at Telethon's synchronous sender seam."""

    def __init__(self, gate: TelegramRpcGate, send_attempt: _SendAttempt, scope: TelegramRpcScope) -> None:
        self._gate = gate
        self._send_attempt = send_attempt
        self._scope = scope

    def send(self, request: object, *, ordered: bool = False) -> Coroutine[object, object, object]:
        return self._send(request, ordered=ordered)

    async def _send(self, request: object, *, ordered: bool) -> object:
        while True:
            budget = self._scope.attempt_budget
            if budget is not None and budget.exhausted:
                self._gate._admission_scheduler.record_attempt_budget_exhausted(self._scope)
                budget.debit()
            admission = await self._gate._admit(self._scope)
            try:
                if not self._gate._scheduler_transport_ready():
                    self._gate._admission_scheduler.record_retry(
                        self._scope,
                        reason="transport_readiness_changed",
                    )
                    continue
                if budget is not None:
                    if budget.exhausted:
                        self._gate._admission_scheduler.record_attempt_budget_exhausted(self._scope)
                    budget.debit()
                self._gate._admission_scheduler.record_dispatch(admission)
                future = self._send_attempt(request, ordered=ordered)
                if isinstance(future, list):
                    self._reject_batch(future)
                return await future
            finally:
                self._gate._admission_scheduler.complete(admission)

    @staticmethod
    def _reject_batch(futures: list[asyncio.Future[object]]) -> Never:
        for future in futures:
            future.cancel()
        raise RuntimeError("Telegram sender returned a future batch for a scalar request")


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
    """Synchronous persistence port for one account-wide UTC cooldown."""

    load_until_utc: Callable[[], float | None]
    save_until_utc: Callable[[float], None]

    def __post_init__(self) -> None:
        if not callable(self.load_until_utc):
            raise TypeError("load_until_utc must be callable")
        if not callable(self.save_until_utc):
            raise TypeError("save_until_utc must be callable")


_COOLDOWN_LOCK = asyncio.Lock()
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
        flood_observer: Callable[..., None] | None = None,
        cooldown_persistence: TelegramRpcCooldownPersistence | None = None,
        **kwargs: object,
    ) -> None:
        kwargs["request_retries"] = 0
        kwargs["flood_sleep_threshold"] = 0
        kwargs["raise_last_call_error"] = True
        kwargs.setdefault("auto_reconnect", True)
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
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
        self._cooldown_persistence = cooldown_persistence
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
        await self._admission_scheduler.close()

    def set_rpc_admission_observer(self, observer: AdmissionObserver | None) -> None:
        """Attach the daemon-owned operational telemetry sink."""
        self._admission_scheduler.set_observer(observer)

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

    def _persist_account_cooldown(self, *, monotonic_now: float) -> None:
        """Persist the effective process deadline without changing FloodWait semantics."""
        persistence = cast(
            TelegramRpcCooldownPersistence | None,
            getattr(self, "_cooldown_persistence", None),
        )
        if persistence is None:
            return
        deadline_utc = time.time() + max(0.0, _COOLDOWN_DEADLINE - monotonic_now)
        try:
            persistence.save_until_utc(deadline_utc)
        except Exception:
            logger.exception("telegram_rpc_cooldown_persist_failed")

    async def __call__(
        self, request: object, ordered: bool = False, flood_sleep_threshold: int | None = None
    ) -> object:
        """Admit one scalar logical RPC and invoke Telethon."""
        del flood_sleep_threshold  # The gate always uses the client-level zero threshold.
        if request is not None and is_list_like(request):
            raise ValueError("transport batching is forbidden; use sequential scalar calls")
        scope = self._require_rpc_scope()
        for retry_index, delay in enumerate((0.0, *self._transient_retry_delays)):
            if retry_index and delay:
                await asyncio.sleep(delay)
            try:
                return await self._call_with_source_policy(request, ordered=ordered, scope=scope)
            except TransientRpcErrors:
                if retry_index >= len(self._transient_retry_delays):
                    raise
                self._admission_scheduler.record_retry(scope, reason="server_transient")
        raise AssertionError("unreachable")

    def _require_rpc_scope(self) -> TelegramRpcScope:
        try:
            return current_rpc_scope()
        except UnclassifiedTelegramRpcError as exc:
            self._admission_scheduler.record_unclassified()
            if "no demand context" in str(exc):
                raise UnclassifiedTelegramRpcError("Telegram RPC has no explicit operation source") from exc
            raise

    async def _call_with_source_policy(
        self,
        request: object,
        *,
        ordered: bool,
        scope: TelegramRpcScope,
    ) -> object:
        while True:
            try:
                return await self._dispatch_attempt(request, ordered=ordered, scope=scope)
            except (RpcAdmissionSaturatedError, RpcAdmissionExpiredError) as exc:
                await self._handle_admission_deferral(scope, exc)
            except TelegramRpcThrottled:
                if scope.source is not TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE:
                    raise
                await self._retry_update_source(scope, reason="account_circuit")
            except (FloodWaitError, FloodPremiumWaitError, FloodTestPhoneWaitError) as exc:
                await self._handle_flood_wait(scope, exc)

    def _send_real_sender(
        self,
        request: object,
        *,
        ordered: bool = False,
    ) -> Awaitable[object] | list[asyncio.Future[object]]:
        return cast(_SendAttempt, self._sender.send)(request, ordered=ordered)

    async def _handle_admission_deferral(self, scope: TelegramRpcScope, exc: RpcAdmissionError) -> None:
        if scope.source is TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE:
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
        await asyncio.sleep(self._scheduler_policy.update_loop_retry_seconds)

    async def _handle_flood_wait(self, scope: TelegramRpcScope, exc: BaseException) -> None:
        seconds = await self._observe_flood(exc)
        if scope.source is TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE:
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
        self.check_circuit()
        sender = _AdmissionAwareSender(self, self._send_real_sender, scope)
        return await super()._call(sender, request, ordered=ordered)  # type: ignore[misc]

    async def _admit(self, scope: TelegramRpcScope) -> RpcAdmission:
        self.check_circuit()
        return await self._admission_scheduler.admit(scope)

    async def _update_loop(self) -> None:
        """Give Telethon difference RPCs a live scope and non-fatal policy waits."""
        with rpc_scope(TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE):
            await super()._update_loop()  # type: ignore[misc]

    async def _dispatch_update(self, update: object) -> None:
        """Give Telethon's update child task its own live scope ownership."""
        with rpc_scope(TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE):
            await super()._dispatch_update(update)  # type: ignore[misc]

    def _scheduler_transport_ready(self) -> bool:
        return not self._rpc_circuit_status().open and account_cooldown_deadline() <= time.monotonic()

    async def _wait_for_scheduler_transport(self) -> None:
        while not self._scheduler_transport_ready():
            status = self._rpc_circuit_status()
            delay = (
                self._scheduler_policy.update_loop_retry_seconds
                if status.open
                else max(account_cooldown_deadline() - time.monotonic(), 0.0)
            )
            await asyncio.sleep(delay)

    async def _observe_flood(self, exc: BaseException) -> int:
        """Atomically extend cooldown and send exactly one telemetry event."""
        seconds = flood_seconds(exc, default=self._fallback_wait_seconds)
        now = time.monotonic()
        global _COOLDOWN_DEADLINE
        async with _COOLDOWN_LOCK:
            _COOLDOWN_DEADLINE = max(_COOLDOWN_DEADLINE, now + seconds + self._cooldown_buffer_seconds)
            self._persist_account_cooldown(monotonic_now=now)
            identity = id(exc)
            if getattr(exc, "_mcp_telegram_flood_observed", False) or identity in _OBSERVED_FLOOD_IDS:
                return seconds
            try:
                setattr(exc, "_mcp_telegram_flood_observed", True)  # noqa: B010 - exception marker is intentional
            except AttributeError:
                _OBSERVED_FLOOD_IDS.add(identity)
            except TypeError:
                _OBSERVED_FLOOD_IDS.add(identity)
            if self._flood_observer is not None:
                self._flood_observer(source="telegram_rpc_gate", seconds=seconds)
            return seconds


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
