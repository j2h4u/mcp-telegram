"""Account-wide admission control for the daemon's Telethon client."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

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

from .flood import flood_seconds

FloodWaitErrors = (FloodWaitError, FloodPremiumWaitError, FloodTestPhoneWaitError)
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


@dataclass(frozen=True, slots=True)
class TelegramRpcBudget:
    """Process-wide logical RPC limiter settings."""

    max_calls_per_period: int
    period_seconds: float

    @property
    def enabled(self) -> bool:
        return self.max_calls_per_period > 0


class TelegramRpcCircuitOpenError(RuntimeError):
    """Raised before admission when the account kill switch is open."""


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
        fallback_wait_seconds: int = 60,
        cooldown_buffer_seconds: float = 1.0,
        transient_retry_delays_seconds: tuple[float, ...] = (2.0,),
        flood_observer: Callable[..., None] | None = None,
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
        self._rpc_budget = rpc_budget
        self._rpc_circuit_status = circuit_status
        self._fallback_wait_seconds = fallback_wait_seconds
        self._cooldown_buffer_seconds = cooldown_buffer_seconds
        self._transient_retry_delays = transient_retry_delays_seconds
        self._flood_observer = flood_observer
        self._limiter = (
            AsyncLimiter(rpc_budget.max_calls_per_period, rpc_budget.period_seconds) if rpc_budget.enabled else None
        )

    def check_circuit(self) -> None:
        status = self._rpc_circuit_status()
        if status.open:
            raise TelegramRpcCircuitOpenError(status.detail())

    async def __call__(
        self, request: object, ordered: bool = False, flood_sleep_threshold: int | None = None
    ) -> object:
        """Admit one scalar logical RPC and invoke Telethon."""
        if request is not None and is_list_like(request):
            raise ValueError("transport batching is forbidden; use sequential scalar calls")
        for retry_index, delay in enumerate((0.0, *self._transient_retry_delays)):
            if retry_index:
                await asyncio.sleep(delay)
            try:
                await self._admit()
                return await super().__call__(request, ordered=ordered, flood_sleep_threshold=0)
            except FloodWaitErrors as exc:
                await self._observe_flood(exc)
                raise
            except TransientRpcErrors:
                if retry_index >= len(self._transient_retry_delays):
                    raise
        raise AssertionError("unreachable")

    async def _admit(self) -> None:
        self.check_circuit()
        await self._wait_for_cooldown()
        if self._limiter is not None:
            await self._limiter.acquire()
        self.check_circuit()
        await self._wait_for_cooldown()

    async def _wait_for_cooldown(self) -> None:
        while True:
            self.check_circuit()
            async with _COOLDOWN_LOCK:
                remaining = _COOLDOWN_DEADLINE - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(remaining)

    async def _observe_flood(self, exc: BaseException) -> None:
        """Atomically extend cooldown and send exactly one telemetry event."""
        seconds = flood_seconds(exc, default=self._fallback_wait_seconds)
        now = time.monotonic()
        global _COOLDOWN_DEADLINE
        async with _COOLDOWN_LOCK:
            _COOLDOWN_DEADLINE = max(_COOLDOWN_DEADLINE, now + seconds + self._cooldown_buffer_seconds)
            identity = id(exc)
            if getattr(exc, "_mcp_telegram_flood_observed", False) or identity in _OBSERVED_FLOOD_IDS:
                return
            try:
                setattr(exc, "_mcp_telegram_flood_observed", True)  # noqa: B010 - exception marker is intentional
            except AttributeError:
                _OBSERVED_FLOOD_IDS.add(identity)
            except TypeError:
                _OBSERVED_FLOOD_IDS.add(identity)
            if self._flood_observer is not None:
                self._flood_observer(source="telegram_rpc_gate", seconds=seconds)


__all__ = [
    "FloodWaitErrors",
    "TelegramRpcBudget",
    "TelegramRpcCircuitOpenError",
    "TelegramRpcGate",
    "TransientRpcErrors",
    "account_cooldown_deadline",
    "reset_account_cooldown",
]
