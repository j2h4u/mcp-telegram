"""The shared gateway fixture owns and releases its real access registry."""

import sqlite3
from typing import cast

import pytest

from mcp_telegram.auth_scope import TelegramAuthScope
from mcp_telegram.peer_access import PeerAccess
from mcp_telegram.telegram_rpc import TelegramRpcGate
from tests import peer_access_helpers


def test_fixture_cleanup_closes_database_and_revokes_tokens() -> None:
    scope = TelegramAuthScope(1, 42, 2, 9)

    class Gate:
        access: PeerAccess

        def retained_auth_scope(self) -> TelegramAuthScope:
            return scope

        def bind_peer_access(self, access: PeerAccess) -> None:
            self.access = access

    gate = Gate()
    peer_access_helpers.bind_test_peer_access(cast(TelegramRpcGate, gate))
    access, conn = peer_access_helpers._bindings[-1]
    token = access.capture(scope, (11,), None)
    assert access.claim(token)
    peer_access_helpers.close_test_peer_access()
    assert not access.claim(token)
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
