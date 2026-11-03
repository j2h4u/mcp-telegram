"""Writer-owned access state, captured before RPC admission and claimed in memory."""

import asyncio
import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import cast

from .auth_scope import TelegramAuthScope
from .image_authorization import ScopeCell, ScopeToken

logger = logging.getLogger(__name__)


class PeerAccessRejectedError(RuntimeError):
    """A peer was inaccessible before this physical attempt."""

    def __init__(self, reason: str, *, was_claimed: bool = False) -> None:
        super().__init__(reason)
        self.was_claimed = was_claimed


class PeerAccess:
    def __init__(self, scope: TelegramAuthScope) -> None:
        self.scope = scope
        self._lock = threading.Lock()
        self._cells: dict[int, ScopeCell] = {}
        self._probe_cells: dict[int, ScopeCell] = {}
        self._denied: set[int] = set()
        self._dirty: set[int] = set()
        self._closed = False

    def bind(self, conn: sqlite3.Connection) -> None:
        rows = cast(
            list[tuple[int]],
            conn.execute("SELECT dialog_id FROM synced_dialogs WHERE status='access_lost'").fetchall(),
        )
        self._denied = {int(row[0]) for row in rows}
        with _bindings_lock:
            if conn in _bindings:
                raise RuntimeError("peer access already bound")
            _bindings[conn] = self

    def unbind(self, conn: sqlite3.Connection) -> None:
        with _bindings_lock:
            if _bindings.pop(conn, None) is not self:
                raise RuntimeError("peer access binding mismatch")
        with self._lock:
            self._closed = True
            for cell in (*self._cells.values(), *self._probe_cells.values()):
                cell.publish(None)

    def capture(
        self, scope: TelegramAuthScope | None, targets: tuple[int, ...], exempt: int | None
    ) -> tuple[ScopeToken | None, ...]:
        with self._lock:
            self._refresh_scope(scope)
            tokens = []
            for target in targets:
                cells = self._probe_cells if target == exempt else self._cells
                cell = cells.setdefault(target, ScopeCell())
                allowed = target not in self._denied or (target == exempt and target not in self._dirty)
                cell.publish(self.scope if allowed else None)
                tokens.append(cell.capture())
            return tuple(tokens)

    def _refresh_scope(self, scope: TelegramAuthScope | None) -> None:
        if self._closed or scope is None or scope.account_id != self.scope.account_id:
            raise PeerAccessRejectedError("peer_access_scope_changed")
        if scope != self.scope:
            for cell in (*self._cells.values(), *self._probe_cells.values()):
                cell.publish(None)
            self.scope = scope

    def claim(self, tokens: tuple[ScopeToken | None, ...]) -> bool:
        with self._lock:
            return not self._closed and all(
                token is not None and token.scope == self.scope and cast(ScopeCell, token._owner).claim(token)
                for token in tokens
            )

    def confirmed_lost(self, scope: TelegramAuthScope | None, dialog_id: int) -> bool:
        """Report committed access loss only for the active account scope."""
        with self._lock:
            return (
                not self._closed
                and scope is not None
                and scope == self.scope
                and dialog_id in self._denied
                and dialog_id not in self._dirty
            )

    def revoke(self, dialog_id: int) -> None:
        with self._lock:
            self._dirty.add(dialog_id)
            self._denied.add(dialog_id)
            for cells in (self._cells, self._probe_cells):
                cell = cells.get(dialog_id)
                if cell is not None:
                    cell.publish(None)

    def invalidate_scope(self) -> None:
        """Revoke queued attempts before a transport identity transition."""
        with self._lock:
            for cell in (*self._cells.values(), *self._probe_cells.values()):
                cell.publish(None)

    def reconcile(self, conn: sqlite3.Connection) -> None:
        with self._lock:
            dirty = tuple(self._dirty)
        for dialog_id in dirty:
            try:
                row = cast(
                    tuple[str | None] | None,
                    conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)).fetchone(),
                )
            except Exception as error:  # noqa: BLE001 - retain denied state and preserve the writer result.
                logger.warning("peer_access_reconcile_failed error_type=%s", type(error).__name__)
                continue
            with self._lock:
                if row is None or row[0] != "access_lost":
                    self._denied.discard(dialog_id)
                else:
                    self._denied.add(dialog_id)
                self._dirty.discard(dialog_id)


_bindings_lock = threading.Lock()
_bindings: dict[sqlite3.Connection, PeerAccess] = {}
_target: ContextVar[tuple[asyncio.Task[object] | None, int] | None] = ContextVar("peer_access_target", default=None)


@contextmanager
def peer_target(dialog_id: int) -> Iterator[None]:
    if type(dialog_id) is not int or dialog_id == 0:
        raise ValueError("peer target must be a nonzero canonical dialog ID")
    token = _target.set((asyncio.current_task(), dialog_id))
    try:
        yield
    finally:
        _target.reset(token)


def current_peer_target() -> int | None:
    target = _target.get()
    return target[1] if target is not None and target[0] is asyncio.current_task() else None


def revoke_peer(conn: sqlite3.Connection, dialog_id: int) -> None:
    with _bindings_lock:
        access = _bindings.get(conn)
    if access is not None:
        access.revoke(dialog_id)


def reconcile_peers(conn: sqlite3.Connection) -> None:
    with _bindings_lock:
        access = _bindings.get(conn)
    if access is not None:
        access.reconcile(conn)
