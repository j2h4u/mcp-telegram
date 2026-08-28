"""Shared Telegram RPC governor.

This module owns account-level request budgeting for daemon-owned Telegram
calls. Call sites receive a client-like proxy instead of duplicating local
sleep/rate-limit rules.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, cast

from aiolimiter import AsyncLimiter
from telethon.utils import is_list_like  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


class CircuitStatus(Protocol):
    @property
    def open(self) -> bool: ...

    def detail(self) -> str: ...


class GovernedTelegramClientTarget(Protocol):
    def __call__(self, *args: object, **kwargs: object) -> Awaitable[object]: ...

    def get_messages(self, *args: object, **kwargs: object) -> Awaitable[object]: ...

    def get_input_entity(self, *args: object, **kwargs: object) -> Awaitable[object]: ...

    def iter_messages(self, *args: object, **kwargs: object) -> AsyncIterator[object]: ...

    def iter_dialogs(self, *args: object, **kwargs: object) -> AsyncIterator[object]: ...

    def iter_participants(self, *args: object, **kwargs: object) -> AsyncIterator[object]: ...


class _ClientBoundIterator(Protocol):
    client: object


@dataclass(frozen=True, slots=True)
class TelegramRpcBudget:
    """Global account-level Telegram RPC budget."""

    max_calls_per_period: int
    period_seconds: float

    @property
    def enabled(self) -> bool:
        return self.max_calls_per_period > 0


class TelegramRpcCircuitOpenError(RuntimeError):
    """Raised before a Telegram RPC when the account breaker is already open."""


class TelegramRpcGovernor:
    """Rate-limit Telegram RPC entry and honor the account circuit breaker."""

    def __init__(
        self,
        budget: TelegramRpcBudget,
        *,
        circuit_status: Callable[[], CircuitStatus],
    ) -> None:
        self._circuit_status = circuit_status
        self._limiter = AsyncLimiter(budget.max_calls_per_period, budget.period_seconds) if budget.enabled else None

    def check_circuit(self) -> None:
        status = self._circuit_status()
        if not status.open:
            return
        raise TelegramRpcCircuitOpenError(status.detail())

    async def acquire(self, *, source: str) -> None:
        self.check_circuit()
        if self._limiter is None:
            return
        logger.debug("telegram_rpc_budget_acquire source=%s", source)
        async with self._limiter:
            return


class GovernedTelegramClient:
    """Client-like proxy applying a shared governor to Telegram-facing calls."""

    def __init__(self, client: GovernedTelegramClientTarget, governor: TelegramRpcGovernor) -> None:
        self._client = client
        self._governor = governor

    def __getattr__(self, name: str) -> object:
        return cast(object, getattr(self._client, name))

    async def __call__(self, *args: object, **kwargs: object) -> object:
        request = args[0] if args else kwargs.get("request")
        if request is not None and is_list_like(request):
            raise ValueError("transport batching is forbidden; use sequential scalar calls")
        await self._governor.acquire(source="client_call")
        return await self._client(*args, **kwargs)

    async def get_messages(self, *args: object, **kwargs: object) -> object:
        await self._governor.acquire(source="get_messages")
        return await self._client.get_messages(*args, **kwargs)

    async def get_input_entity(self, *args: object, **kwargs: object) -> object:
        """Resolve a peer through the shared account budget and circuit."""
        await self._governor.acquire(source="get_input_entity")
        return await self._client.get_input_entity(*args, **kwargs)

    def iter_messages(self, *args: object, **kwargs: object) -> AsyncIterator[object]:
        return self._governed_iterator("iter_messages", *args, **kwargs)

    def iter_dialogs(self, *args: object, **kwargs: object) -> AsyncIterator[object]:
        return self._governed_iterator("iter_dialogs", *args, **kwargs)

    def iter_participants(self, *args: object, **kwargs: object) -> AsyncIterator[object]:
        return self._governed_iterator("iter_participants", *args, **kwargs)

    async def _governed_iterator(self, method_name: str, *args: object, **kwargs: object) -> AsyncIterator[object]:
        if method_name == "iter_messages":
            iterator = self._client.iter_messages(*args, **kwargs)
        elif method_name == "iter_dialogs":
            iterator = self._client.iter_dialogs(*args, **kwargs)
        else:
            iterator = self._client.iter_participants(*args, **kwargs)
        if hasattr(cast(object, iterator), "client"):
            # Telethon RequestIter routes each page through ``client(request)``.
            # Bind this proxy at that seam so every page is governed exactly once.
            cast(_ClientBoundIterator, iterator).client = self
        else:
            # Preserve governance for third-party async iterators without a
            # RequestIter client seam; they expose no page-level request hook.
            await self._governor.acquire(source=method_name)
        async for item in iterator:
            yield item
