"""Referenced identities supplied by history and per-peer search."""

import asyncio
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from telethon._updates.entitycache import EntityCache
from telethon.tl import types
from telethon.tl.functions.messages import GetHistoryRequest, SearchRequest

from mcp_telegram import activity_peer_sweep
from mcp_telegram.activity_peer_sweep import PeerSweepRequest, sweep_peer_once
from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.delta_sync import DeltaSyncWorker
from mcp_telegram.dialog_identity import read_dialog_identities
from mcp_telegram.history_enrollment import enable_history
from mcp_telegram.image_authorization import ScopeCell, ScopeToken
from mcp_telegram.message_history.telegram_adapter import TelethonForwardGapPageAdapter, TelethonFullHistoryPageAdapter
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.sync_worker import FullSyncWorker
from mcp_telegram.telegram_demand import demand_context
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcSource, rpc_scope


@pytest.mark.asyncio
@pytest.mark.parametrize("forward", [False, True])
@pytest.mark.parametrize("change_at", ["none", "rpc", "writer"])
@pytest.mark.parametrize("before", [0, 7])
async def test_history_sender_sidecar_is_persisted(tmp_path: Path, forward: bool, change_at: str, before: int):
    path = tmp_path / "history.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    dialog = -123

    def seed(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
            conn.execute("INSERT INTO entities(id,type,name,username,updated_at) VALUES(88,'user','Before','before',1)")
            conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(-123,'synced')")
        enable_history(conn, dialog)

    await writer.run(seed)
    locked = threading.Event()
    release = threading.Event()
    blockers: list[asyncio.Task[object]] = []

    class Client:
        cell = ScopeCell()
        scope = TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99)
        assert cell.publish(scope)

        def capture_image_scope(self) -> ScopeToken | None:
            return self.cell.capture()

        def claim_image_scope(self, token: ScopeToken) -> bool:
            return self.cell.claim(token)

        _self_id = 42
        _mb_entity_cache = EntityCache()

        async def get_input_entity(self, dialog_id: int) -> types.InputPeerChat:
            return types.InputPeerChat(123)

        async def __call__(self, request: object) -> types.messages.Messages:
            assert isinstance(request, GetHistoryRequest)
            if change_at == "rpc":
                change = self.cell.begin_change()
                self.cell.end_change(change)
                assert self.cell.publish(self.scope)
            if change_at == "writer":

                def block(_conn: sqlite3.Connection) -> None:
                    locked.set()
                    assert release.wait(5)

                blockers.append(asyncio.create_task(writer.run(block)))
                assert await asyncio.to_thread(locked.wait, 5)

                def revoke() -> None:
                    change = self.cell.begin_change()
                    self.cell.end_change(change)
                    assert self.cell.publish(self.scope)
                    release.set()

                asyncio.get_running_loop().call_soon(revoke)
            message = types.Message(
                id=7,
                peer_id=types.PeerChat(123),
                date=datetime(2026, 1, 1, tzinfo=UTC),
                message="hello",
                from_id=types.PeerUser(88),
            )
            chat = types.Chat(123, "Group", types.ChatPhotoEmpty(), 1, None, 1)
            return types.messages.Messages(
                [message], [], [chat], [types.User(88, first_name="After", username="after")]
            )

        async def get_entity(self, peer: object) -> object:
            raise AssertionError("network forbidden")

    client = Client()
    reader = sqlite3.connect(path)
    try:
        if forward:
            owner = DeltaSyncWorker(TelethonForwardGapPageAdapter(client), reader, asyncio.Event(), writer=writer)
            await owner.fetch_delta_slice_for_dialog(dialog)
        else:
            owner = FullSyncWorker(TelethonFullHistoryPageAdapter(client), reader, asyncio.Event(), writer=writer)
            await owner._fetch_batch(dialog, before)
        await asyncio.gather(*blockers)
        revoked = change_at != "none"
        expected = ("Before", "before") if revoked else ("After", "after")
        assert reader.execute("SELECT name,username FROM entities WHERE id=88").fetchone() == expected
        message = cast(
            tuple[object, ...] | None,
            reader.execute(
                "SELECT sender_id,sender_first_name FROM messages WHERE dialog_id=-123 AND message_id=7"
            ).fetchone(),
        )
        assert message == (None if revoked or (before and not forward) else (88, "After"))
    finally:
        release.set()
        reader.close()
        await writer.aclose()
    with closing(sqlite3.connect(path)) as reopened:
        assert reopened.execute("SELECT name,username FROM entities WHERE id=88").fetchone() == expected
        assert read_dialog_identities(reopened, [88])[88].name == expected[0]
        assert reopened.execute("SELECT 1 FROM synced_dialogs WHERE dialog_id=88").fetchone() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change_at", ["none", "rpc", "extraction"])
@pytest.mark.parametrize("stale", [False, True])
async def test_peer_search_forwarded_user_sidecar_is_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change_at: str, stale: bool
):
    path = tmp_path / "peer.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)

    def seed(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
            conn.execute("INSERT INTO entities(id,type,name,username,updated_at) VALUES(88,'user','Before','before',1)")
            conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(-123,'synced')")

    await writer.run(seed)

    class Client:
        cell = ScopeCell()
        scope = TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99)
        assert cell.publish(scope)

        def capture_image_scope(self) -> ScopeToken | None:
            return self.cell.capture()

        def claim_image_scope(self, token: ScopeToken) -> bool:
            return self.cell.claim(token)

        async def get_input_entity(self, dialog_id: int) -> types.InputPeerChat:
            return types.InputPeerChat(123)

        async def __call__(self, request: object) -> types.messages.Messages:
            assert isinstance(request, SearchRequest)
            if change_at == "rpc":
                change = self.cell.begin_change()
                self.cell.end_change(change)
                assert self.cell.publish(self.scope)
            msg = types.Message(
                7,
                types.PeerChat(123),
                datetime(2026, 1, 1, tzinfo=UTC),
                "forward",
                out=True,
                from_id=types.PeerUser(42),
                fwd_from=types.MessageFwdHeader(datetime(2026, 1, 1, tzinfo=UTC), from_id=types.PeerUser(88)),
            )
            return types.messages.Messages(
                [msg],
                [],
                [types.Chat(123, "Group", types.ChatPhotoEmpty(), 1, None, 1)],
                [types.User(42, first_name="Self", is_self=True), types.User(88, first_name="After", username="after")],
            )

    client = Client()
    if change_at == "extraction":

        def fail_extraction(*_args: object) -> None:
            change = client.cell.begin_change()
            client.cell.end_change(change)
            assert client.cell.publish(client.scope)
            raise ValueError("controlled extraction failure")

        monkeypatch.setattr(activity_peer_sweep, "_extract_sweep_messages", fail_extraction)
    reader = sqlite3.connect(path)
    try:
        checks = 0

        def accept_page(_conn: sqlite3.Connection) -> bool:
            nonlocal checks
            checks += 1
            return not stale or checks == 1

        with demand_context(DemandKind.HOT_ACTIVITY_PAGE), rpc_scope(TelegramRpcSource.ACTIVITY_HOT_SWEEP):
            result = await sweep_peer_once(
                PeerSweepRequest(
                    client,
                    reader,
                    -123,
                    0,
                    0,
                    100,
                    1,
                    writer=writer,
                    profile_owner_account_id=42,
                    accept_page=accept_page,
                )
            )
        revoked = change_at == "rpc" or (change_at == "extraction" and not stale)
        assert result.persisted == (0 if revoked or stale else 1)
        expected = ("Before", "before") if revoked else ("After", "after")
        assert reader.execute("SELECT name,username FROM entities WHERE id=88").fetchone() == expected
        message = cast(
            tuple[object, ...] | None,
            reader.execute(
                "SELECT fwd_from_peer_id FROM message_forwards WHERE dialog_id=-123 AND message_id=7"
            ).fetchone(),
        )
        assert message == (None if revoked or stale else (88,))
    finally:
        reader.close()
        await writer.aclose()
    with closing(sqlite3.connect(path)) as reopened:
        assert reopened.execute("SELECT name,username FROM entities WHERE id=88").fetchone() == expected
        assert read_dialog_identities(reopened, [88])[88].name == expected[0]
        assert reopened.execute("SELECT 1 FROM synced_dialogs WHERE dialog_id=88").fetchone() is None
