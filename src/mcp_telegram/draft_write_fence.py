"""In-flight draft commands fenced by canonical draft revisions."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from weakref import WeakSet

from mcp_telegram.drafts.contracts import DraftScope


class DraftWriteRejectedError(RuntimeError):
    """Draft changed before the physical write."""


@dataclass(eq=False, slots=True, weakref_slot=True)
class DraftWatch:
    scope: DraftScope
    valid: bool = True


_WATCHES: WeakSet[DraftWatch] = WeakSet()
_LOCK = threading.Lock()
_CHECK: ContextVar[DraftWatch | None] = ContextVar("draft_write_check", default=None)


@contextmanager
def draft_write_context(scope: DraftScope) -> Iterator[DraftWatch]:
    watch = DraftWatch(scope)
    with _LOCK:
        _WATCHES.add(watch)
    token = _CHECK.set(watch)
    try:
        yield watch
    finally:
        _CHECK.reset(token)
        with _LOCK:
            _WATCHES.discard(watch)


def invalidate_draft_watches(scope: DraftScope) -> None:
    own = _CHECK.get()
    with _LOCK:
        for watch in tuple(_WATCHES):
            if watch.scope == scope and watch is not own:
                watch.valid = False


def current_draft_write_check() -> DraftWatch | None:
    return _CHECK.get()


@contextmanager
def draft_claim(watch: DraftWatch | None) -> Iterator[None]:
    if watch is None:
        yield
        return
    with _LOCK:
        if not watch.valid:
            raise DraftWriteRejectedError("draft_version_changed")
        yield
