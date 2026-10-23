"""Retained identity of one durably admitted Telegram update."""

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import Context, ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(slots=True)
class DeliveredUpdate:
    observation_order: int
    captured_at: float
    reaction_sequence: int
    failed: bool = False
    owner_task: object | None = None


_current: ContextVar[DeliveredUpdate | None] = ContextVar("delivered_update", default=None)


def current_delivered_update() -> DeliveredUpdate | None:
    current = _current.get()
    if current is None:
        return None
    try:
        task = asyncio.current_task()
    except RuntimeError:
        # SQLiteWriter explicitly copies the admitting task's context to its worker.
        return current
    return current if current.owner_task is task else None


def isolate_delivered_update(context: Context) -> None:
    """Strip a child task's inherited event identity before handing work to SQLite."""
    context.run(_current.set, current_delivered_update())


@contextmanager
def delivered_update_context(update: DeliveredUpdate | None) -> Iterator[None]:
    if update is not None:
        update.owner_task = asyncio.current_task()
    token = _current.set(update)
    try:
        yield
    finally:
        _current.reset(token)


def delivered_observed_at() -> int:
    current = current_delivered_update()
    return int(time.time() if current is None else current.captured_at)


def delivered_observed_datetime() -> datetime:
    current = current_delivered_update()
    return datetime.now(UTC) if current is None else datetime.fromtimestamp(current.captured_at, UTC)


def fail_delivered_update() -> None:
    current = current_delivered_update()
    if current is not None:
        current.failed = True
