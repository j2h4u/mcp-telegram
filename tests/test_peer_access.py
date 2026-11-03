"""Access state follows committed writer callbacks and fences old generations."""

import sqlite3
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from telethon import types
from telethon.tl.functions import channels, messages, users
from telethon.tl.tlobject import TLRequest

from mcp_telegram import sqlite_writer
from mcp_telegram.auth_scope import TelegramAuthScope
from mcp_telegram.peer_access import PeerAccess, PeerAccessRejectedError, revoke_peer
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_demand import RpcAttemptBudget
from mcp_telegram.telegram_demand_progress import WorkKey, WorkRevision
from mcp_telegram.telegram_rpc import _canonical_wire_method, _probe_target, _request_peers, _WireAttempt
from mcp_telegram.telegram_rpc_consumers import DemandKind, RpcServiceClass
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcScope, TelegramRpcSource


async def test_writer_reconciles_commit_and_rollback(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    ensure_sync_schema(path)
    scope = TelegramAuthScope(1, 42, 2, 9)
    access = PeerAccess(scope)
    writer = await SQLiteWriter.open(path)
    await writer.run(access.bind)
    old = access.capture(scope, (11,), None)
    assert not PeerAccess(TelegramAuthScope(1, 43, 2, 9)).claim(old)

    def loss(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            revoke_peer(conn, 11)
            assert not access.confirmed_lost(scope, 11)
            conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (11,'access_lost')")

    await writer.run(loss)
    assert access.confirmed_lost(scope, 11)
    assert not access.confirmed_lost(None, 11)
    assert not access.confirmed_lost(TelegramAuthScope(1, 43, 2, 9), 11)
    assert not access.claim(old)
    assert not access.claim(access.capture(scope, (11,), None))
    probe = access.capture(scope, (11,), 11)
    assert not access.claim(access.capture(scope, (11,), None))
    assert access.claim(probe)

    def failed_restore(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            revoke_peer(conn, 11)
            assert not access.confirmed_lost(scope, 11)
            conn.execute("UPDATE synced_dialogs SET status='synced' WHERE dialog_id=11")
            raise ValueError("rollback")

    with pytest.raises(ValueError, match="rollback"):
        await writer.run(failed_restore)
    assert access.confirmed_lost(scope, 11)
    assert not access.claim(probe)
    assert not access.claim(access.capture(scope, (11,), None))

    def restore(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            revoke_peer(conn, 11)
            conn.execute("UPDATE synced_dialogs SET status='synced' WHERE dialog_id=11")

    await writer.run(restore)
    assert not access.confirmed_lost(scope, 11)
    assert access.claim(access.capture(scope, (11,), None))
    with pytest.raises(PeerAccessRejectedError):
        access.capture(TelegramAuthScope(1, 43, 2, 9), (11,), None)
    await writer.run(access.unbind)
    await writer.aclose()


async def test_reconciliation_failure_preserves_writer_outcomes_and_denies_peer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.db"
    ensure_sync_schema(path)
    scope = TelegramAuthScope(1, 42, 2, 9)
    access = PeerAccess(scope)
    writer = await SQLiteWriter.open(path)
    await writer.run(access.bind)

    def fail_reconciliation(_conn: sqlite3.Connection) -> None:
        raise RuntimeError("observer failure")

    monkeypatch.setattr(sqlite_writer, "reconcile_peers", fail_reconciliation)

    def commit(conn: sqlite3.Connection) -> str:
        with write_transaction(conn):
            revoke_peer(conn, 11)
            conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (11,'access_lost')")
        return "committed"

    assert await writer.run(commit) == "committed"
    assert not access.claim(access.capture(scope, (11,), None))

    def fail(_conn: sqlite3.Connection) -> None:
        raise ValueError("original")

    with pytest.raises(ValueError, match="original"):
        await writer.run(fail)
    assert not access.claim(access.capture(scope, (11,), 11))
    await writer.run(access.unbind)
    await writer.aclose()


def test_request_targets_include_all_typed_vector_peers() -> None:
    assert _request_peers(users.GetUsersRequest([types.InputUser(1, 1), types.InputUser(2, 1)]), 42, None) == (1, 2)
    assert _request_peers(channels.GetChannelsRequest([types.InputChannel(5, 1)]), 42, None) == (-1000000000005,)
    assert _request_peers(messages.GetChatsRequest([3, 4]), 42, None) == (-4, -3)
    assert _request_peers(messages.GetAllDraftsRequest(), 42, None) == ()
    search = messages.SearchRequest(
        types.InputPeerUser(11, 1),
        "",
        types.InputMessagesFilterEmpty(),
        None,
        None,
        0,
        0,
        1,
        0,
        0,
        0,
        from_id=types.InputPeerUser(12, 1),
    )
    assert _request_peers(search, 42, None) == (11,)
    for field in ("peer", "channel", "chat_id"):
        with pytest.raises(PeerAccessRejectedError):
            _request_peers(cast(TLRequest, SimpleNamespace(**{field: 11})), 42, None)


def test_probe_exemption_requires_the_exact_selected_peer_and_read_effect() -> None:
    auth = TelegramAuthScope(1, 42, 2, 9)
    selected = WorkRevision(WorkKey(DemandKind.DELTA_ACCESS_PROBE, 42, "probe:11"), "generation")
    scope = TelegramRpcScope(
        TelegramRpcSource.DELTA_SYNC,
        RpcServiceClass.BACKGROUND,
        time.monotonic() + 10,
        None,
        demand_kind=DemandKind.DELTA_ACCESS_PROBE,
        attempt_budget=RpcAttemptBudget(1, selected=selected),
    )
    request = messages.GetHistoryRequest(types.InputPeerUser(11, 1), 0, None, 0, 1, 0, 0, 0)
    owner = _WireAttempt(object(), "attempt", _canonical_wire_method(request), scope)
    assert _probe_target(owner, auth, (11,)) == 11
    assert _probe_target(owner, auth, (12,)) is None
    assert _probe_target(owner, TelegramAuthScope(1, 43, 2, 9), (11,)) is None
    owner.scope = replace(scope, demand_kind=DemandKind.DELTA_GAP_FILL)
    assert _probe_target(owner, auth, (11,)) is None
    owner.scope = scope
    owner.method = _canonical_wire_method(messages.SaveDraftRequest(types.InputPeerUser(11, 1), "test"))
    assert _probe_target(owner, auth, (11,)) is None
