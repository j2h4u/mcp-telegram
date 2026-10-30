"""Memory-only authorization and active image source revocation."""

import sqlite3
import threading
from dataclasses import dataclass, field

from .auth_scope import TelegramAuthScope
from .own_only_contracts import is_channel_peer_id


@dataclass(frozen=True, slots=True)
class ScopeToken:
    generation: int
    scope: TelegramAuthScope
    _owner: object = field(repr=False, compare=False)


class ScopeCell:
    """A short linearization point; no lock is held across I/O."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generation = 0
        self._scope: TelegramAuthScope | None = None
        self._changes: set[int] = set()
        self._next_change = 0

    def capture(self) -> ScopeToken | None:
        with self._lock:
            if self._scope is None or self._changes:
                return None
            return ScopeToken(self._generation, self._scope, self)

    def claim(self, token: ScopeToken) -> bool:
        with self._lock:
            return (
                token._owner is self
                and not self._changes
                and token.generation == self._generation
                and token.scope == self._scope
            )

    def begin_change(self) -> int:
        with self._lock:
            self._next_change += 1
            self._changes.add(self._next_change)
            self._generation += 1
            self._scope = None
            return self._next_change

    def end_change(self, change_id: int) -> None:
        with self._lock:
            self._changes.remove(change_id)

    def publish(self, scope: TelegramAuthScope | None) -> bool:
        with self._lock:
            if self._changes:
                return False
            if scope != self._scope:
                self._generation += 1
                self._scope = scope
            return True


type _SourceKey = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class SourceLease:
    _registry: ActiveSources
    _key: _SourceKey
    _serial: int

    def is_current(self) -> bool:
        return self._registry._current(self._key, self._serial)


class ActiveSources:
    """Only active attempts are represented; mutation invalidates before writing."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._serial = 0
        self._active: dict[_SourceKey, tuple[int, int]] = {}

    def register(self, account_id: int, dialog_id: int, message_id: int, attempt: int) -> SourceLease:
        key = (account_id, dialog_id, message_id)
        with self._lock:
            self._serial += 1
            self._active[key] = (attempt, self._serial)
            return SourceLease(self, key, self._serial)

    def _current(self, key: _SourceKey, serial: int) -> bool:
        with self._lock:
            entry = self._active.get(key)
            return entry is not None and entry[1] == serial

    def revoke(self, account_id: int, dialog_id: int, message_id: int, attempt: int | None = None) -> None:
        key = (account_id, dialog_id, message_id)
        with self._lock:
            entry = self._active.get(key)
            if entry is not None and (attempt is None or entry[0] == attempt):
                del self._active[key]

    def invalidate(self, dialog_id: int, message_id: int) -> None:
        with self._lock:
            for key in tuple(self._active):
                if key[1:] == (dialog_id, message_id):
                    del self._active[key]

    def invalidate_nonchannel(self, message_ids: tuple[int, ...]) -> None:
        ids = set(message_ids)
        with self._lock:
            for key in tuple(self._active):
                if key[2] in ids and not is_channel_peer_id(key[1]):
                    del self._active[key]

    def bind(self, conn: sqlite3.Connection) -> None:
        with _bindings_lock:
            if conn in _bindings:
                raise RuntimeError("image source observer already bound")
            _bindings[conn] = self

    def unbind(self, conn: sqlite3.Connection) -> None:
        with _bindings_lock:
            if _bindings.get(conn) is not self:
                raise RuntimeError("image source observer binding mismatch")
            del _bindings[conn]
        with self._lock:
            self._active.clear()


_bindings_lock = threading.Lock()
_bindings: dict[sqlite3.Connection, ActiveSources] = {}


def invalidate_message_source(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> None:
    with _bindings_lock:
        observer = _bindings.get(conn)
    if observer is not None:
        observer.invalidate(dialog_id, message_id)


def invalidate_nonchannel_sources(conn: sqlite3.Connection, message_ids: tuple[int, ...]) -> None:
    with _bindings_lock:
        observer = _bindings.get(conn)
    if observer is not None:
        observer.invalidate_nonchannel(message_ids)
