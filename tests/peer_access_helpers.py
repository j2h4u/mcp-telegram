"""Real peer-access bindings for the shared Telegram gateway test harness."""

from __future__ import annotations

import atexit
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp_telegram.peer_access import PeerAccess
    from mcp_telegram.telegram_rpc import TelegramRpcGate

_bindings: list[tuple[PeerAccess, sqlite3.Connection]] = []


def bind_test_peer_access(gate: TelegramRpcGate) -> None:
    from mcp_telegram.peer_access import PeerAccess

    scope = gate.retained_auth_scope()
    if scope is None:
        return
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE synced_dialogs(dialog_id INTEGER PRIMARY KEY, status TEXT)")
        access = PeerAccess(scope)
        access.bind(conn)
        try:
            gate.bind_peer_access(access)
        except BaseException:
            access.unbind(conn)
            raise
    except BaseException:
        conn.close()
        raise
    _bindings.append((access, conn))


def close_test_peer_access() -> None:
    while _bindings:
        access, conn = _bindings.pop()
        try:
            access.unbind(conn)
        finally:
            conn.close()


atexit.register(close_test_peer_access)
