"""Session peer durability and deferred checkpoint mirroring after delivery."""

# pyright: reportAny=false

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
from collections.abc import Awaitable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
from telethon import TelegramClient, events, types
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession

from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.telegram_rpc import TelegramRpcGate
from test_update_delivery import DATE, _client, _database, _mixed, _pending, _prepare_migration_transport, _safe, _saved

CHANNEL = -1_000_000_000_123
UNTRACKED_CHANNEL = -1_000_000_000_789


def _durable_peers(path: Path) -> dict[int, int]:
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as reader:
        return dict(reader.execute("SELECT id,hash FROM entities"))


def _put_channel(session: SQLiteSession, access_hash: int, *, title: str = "fixture") -> None:
    session.process_entities(
        types.contacts.ResolvedPeer(types.PeerChannel(123), [types.ChannelForbidden(123, access_hash, title)], [])
    )


async def _complete_peer_batch(client: TelegramRpcGate) -> None:
    # This full hash is intentionally available only in Session, not the safe vector.
    _put_channel(cast(SQLiteSession, client.session), 888)
    client._mb_entity_cache.hash_map.clear()
    updates = await _mixed(
        client,
        users=[types.User(55, access_hash=555, first_name="fixture")],
        chats=[types.ChannelForbidden(789, 7890, "untracked fixture")],
    )
    for update in updates:
        await client._dispatch_update(update)


@dataclass
class _SessionWrites:
    saves: int = 0
    synthetic: list[tuple[int, int]] = field(default_factory=list)
    statements: list[str] = field(default_factory=list)


def _observe_writes(session: SQLiteSession, monkeypatch: pytest.MonkeyPatch) -> _SessionWrites:
    writes = _SessionWrites()
    original_process, original_save = session.process_entities, session.save

    def process(entity: object) -> None:
        if isinstance(entity, types.contacts.ResolvedPeer):
            writes.synthetic.extend(
                (channel.id, channel.access_hash)
                for channel in entity.chats
                if isinstance(channel, types.ChannelForbidden) and channel.title == ""
            )
        original_process(entity)

    def save() -> None:
        writes.saves += 1
        original_save()

    def forbidden_checkpoint(*_args: object) -> None:
        raise AssertionError("completion must not write Session checkpoints")

    monkeypatch.setattr(session, "process_entities", process)
    monkeypatch.setattr(session, "save", save)
    monkeypatch.setattr(session, "set_update_state", forbidden_checkpoint)
    assert session._conn is not None
    session._conn.set_trace_callback(writes.statements.append)
    return writes


@pytest.mark.parametrize(
    ("state", "expected_hash", "synthetic"),
    [
        ("same", 456, 0),
        ("missing", 456, 1),
        ("different", 456, 1),
        ("full", 777, 1),
        ("min", 456, 0),
        ("empty", None, 0),
    ],
)
async def test_completion_only_saves_peer_material_and_avoids_redundant_channel_rewrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str, expected_hash: int | None, synthetic: int
) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    client = _client(tmp_path / "session", writer, channel=state != "empty")
    session = cast(SQLiteSession, client.session)
    try:
        await client.start_update_delivery(99)
        if state == "missing":
            assert session._conn is not None
            session._conn.execute("DELETE FROM entities WHERE id=?", (CHANNEL,))
            session.save()
        elif state == "different":
            _put_channel(session, 999)
            session.save()
        writes = _observe_writes(session, monkeypatch)
        chats: list[types.TypeChat] = []
        if state == "min":
            chats = [types.Channel(123, "min", types.ChatPhotoEmpty(), DATE, min=True, access_hash=777)]
        elif state == "full":
            chats = [types.ChannelForbidden(123, 777, "full")]
        updates = (
            await client._preprocess_updates([], [], []) if state == "empty" else await _mixed(client, chats=chats)
        )
        if state == "full":
            _put_channel(session, 999, title="intervening")
        for update in updates:
            await client._dispatch_update(update)
        assert _pending(path) is None
        assert _safe(path).pts == (10 if state == "empty" else 12)
        assert _saved(client)[0][0] == 10
        assert _durable_peers(Path(session.filename)).get(CHANNEL) == expected_hash
        assert writes.saves == 1 and len(writes.synthetic) == synthetic
        if state in ("same", "min", "empty"):
            assert not [
                sql
                for sql in writes.statements
                if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "BEGIN", "COMMIT"))
            ]
    finally:
        session.close()
        await writer.aclose()


async def test_entity_commit_precedes_canonical_completion_without_session_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    client = _client(tmp_path / "session", writer)
    try:
        await client.start_update_delivery(99)
        repository = client._delivery_repository
        assert repository is not None
        complete = repository.complete

        async def verify_then_complete(snapshot: str) -> None:
            assert _durable_peers(tmp_path / "session.session") == {
                0: 99,
                55: 555,
                CHANNEL: 888,
                UNTRACKED_CHANNEL: 7890,
            }
            assert _pending(path) is not None and _safe(path).pts == 10
            await complete(snapshot)

        monkeypatch.setattr(repository, "complete", verify_then_complete)
        await _complete_peer_batch(client)
        assert _pending(path) is None
        assert _safe(path).channel_hashes == {} and _safe(path).channels == {123: 21}
        assert _saved(client)[0][0] == 10
    finally:
        cast(SQLiteSession, client.session).close()
        await writer.aclose()


@pytest.mark.parametrize("committed", [False, True])
async def test_first_session_save_failure_retains_pending_and_old_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    client = _client(tmp_path / "session", writer)
    session = cast(SQLiteSession, client.session)
    try:
        await client.start_update_delivery(99)
        save = session.save

        def fail_save() -> None:
            if committed:
                save()
            raise OSError("fixture Session save failure")

        monkeypatch.setattr(session, "save", fail_save)
        with pytest.raises(OSError, match="Session save failure"):
            await _complete_peer_batch(client)
        assert _pending(path) is not None and _safe(path).pts == 10
        assert client._delivery_safe is not None and client._delivery_safe.pts == 10
        assert client._delivery_failure.is_set() and _saved(client)[0][0] == 10
        assert (_durable_peers(tmp_path / "session.session").get(55) == 555) is committed
    finally:
        session.close()
        await writer.aclose()


@pytest.mark.parametrize("owner", ["keepalive", "auth"])
async def test_vendor_lifecycle_flush_keeps_pending_head_private_and_saves_peer_auth_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    client = _client(tmp_path / "session", writer)
    session = cast(SQLiteSession, client.session)
    sender = _prepare_migration_transport(client, monkeypatch)
    original_wait = asyncio.wait_for

    async def keepalive_tick(awaitable: Awaitable[object], timeout: float | None) -> object:
        if awaitable is sender.disconnected and timeout == 60:
            raise TimeoutError
        return await original_wait(awaitable, timeout)

    try:
        await client.start_update_delivery(99)
        baseline = _saved(client)
        await _mixed(client)
        session.process_entities(
            types.contacts.ResolvedPeer(types.PeerUser(66), [], [types.User(66, access_hash=666, first_name="fixture")])
        )
        session.set_dc(2, "offline.invalid", 443)
        if owner == "keepalive":
            monkeypatch.setattr(asyncio, "wait_for", keepalive_tick)
            monkeypatch.setattr(sender, "_keepalive_ping", lambda _random_id: setattr(sender, "_connected", False))
            await TelegramClient._keepalive_loop(client)
        else:
            await TelegramClient._auth_key_callback(client, AuthKey(b"\x01" * 256))
            with closing(sqlite3.connect(f"file:{session.filename}?mode=ro", uri=True)) as reader:
                assert reader.execute("SELECT auth_key FROM sessions").fetchone()[0] == b"\x01" * 256
        assert _saved(client) == baseline
        assert _pending(path) is not None and _safe(path).pts == 10
        assert _durable_peers(Path(session.filename))[66] == 666
    finally:
        await sender.disconnect()
        await client.close_rpc_scheduler()
        session.close()
        await writer.aclose()


@pytest.mark.integration
async def test_hard_exit_after_completion_keeps_session_only_peers_and_restarts_without_replay(tmp_path: Path) -> None:
    path = _database(tmp_path)
    script = """
import asyncio,os,sys
from pathlib import Path
sys.path.insert(0,'tests')
from test_update_delivery import _client
from test_update_delivery_session import _complete_peer_batch
from mcp_telegram.sqlite_writer import SQLiteWriter
async def main():
    root=Path(sys.argv[1]);writer=await SQLiteWriter.open(root/'sync.db')
    client=_client(root/'session',writer);await client.start_update_delivery(99)
    await _complete_peer_batch(client)
    os._exit(0)
asyncio.run(main())
"""
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert _durable_peers(tmp_path / "session.session")[55] == 555
    assert _durable_peers(tmp_path / "session.session")[UNTRACKED_CHANNEL] == 7890
    assert _durable_peers(tmp_path / "session.session")[CHANNEL] == 888
    assert _pending(path) is None and _safe(path).pts == 12
    with closing(sqlite3.connect(f"file:{tmp_path / 'session.session'}?mode=ro", uri=True)) as reader:
        assert reader.execute("SELECT pts FROM update_state WHERE id=0").fetchone() == (10,)
    writer = await SQLiteWriter.open(path)
    client = _client(tmp_path / "session", writer, channel=False)

    async def unexpected(_update: object) -> None:
        raise AssertionError("completed payload must not replay")

    client.add_event_handler(unexpected, events.Raw())
    try:
        await client.start_update_delivery(99)
        safe = _safe(path)
        account, channels = client._message_box.session_state()
        assert (account["pts"], account["qts"], account["seq"], channels) == (
            safe.pts,
            safe.qts,
            safe.seq,
            safe.channels,
        )
        assert _saved(client)[0][:3] == (12, 4, 1)
        peer = cast(SQLiteSession, client.session).get_input_entity(types.PeerChannel(123))
        assert isinstance(peer, types.InputPeerChannel) and peer.access_hash == 888
    finally:
        cast(SQLiteSession, client.session).close()
        await writer.aclose()
