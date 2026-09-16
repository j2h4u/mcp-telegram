"""Task-local request correlation for transport/runtime boundaries.

This module deliberately has no project or third-party imports.  A context is
isolated by ``contextvars`` so concurrent MCP calls cannot observe one
another's daemon request IDs.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

_CorrelationState = contextvars.ContextVar[list[str] | None]
_state: _CorrelationState = contextvars.ContextVar("correlation_ids", default=None)
_operation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("operation_id", default=None)


@contextmanager
def correlation_context(operation_id: str | None = None) -> Iterator[None]:
    """Install an empty request-correlation context for one logical operation."""
    token = _state.set([])
    operation_token = _operation_id.set(operation_id)
    try:
        yield
    finally:
        _state.reset(token)
        _operation_id.reset(operation_token)


def record_correlation_id(request_id: str) -> None:
    """Record *request_id* in the current context, if one is active."""
    request_ids = _state.get()
    if request_ids is not None:
        request_ids.append(request_id)


def current_correlation_ids() -> tuple[str, ...]:
    """Return the current context's IDs as an immutable snapshot."""
    request_ids = _state.get()
    return tuple(request_ids) if request_ids is not None else ()


def current_operation_id() -> str | None:
    """Return the opaque logical operation ID active in this task."""
    return _operation_id.get()


__all__ = ["correlation_context", "current_correlation_ids", "current_operation_id", "record_correlation_id"]
