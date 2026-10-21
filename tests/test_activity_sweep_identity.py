"""Activity search response identity retention through canonical publication."""

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from telethon.tl.functions.messages import SearchRequest
from telethon.tl.types import Chat, ChatPhotoEmpty, InputPeerUser, Message, PeerChat, PeerUser, User
from telethon.tl.types.messages import Messages
from telethon.utils import get_peer_id

from mcp_telegram.activity_peer_sweep import PeerSweepRequest, SkipReason, sweep_peer_once
from mcp_telegram.dialog_identity import capture_identity_baseline, publish_dialog_identity
from mcp_telegram.dialog_identity_contracts import DialogIdentityObservation
from mcp_telegram.sync_db import _apply_migrations
from mcp_telegram.sync_transactions import enable_runtime_writes, write_transaction
from sqlite_writer_helpers import inline_writer


class _Client:
    async def get_input_entity(self, dialog_id: int) -> InputPeerUser:
        return InputPeerUser(dialog_id, 1)

    async def __call__(self, request: object) -> object:
        raise AssertionError("Search must use the governed request boundary")


def _request(conn: sqlite3.Connection, dialog_id: int = 111) -> PeerSweepRequest:
    return PeerSweepRequest(_Client(), conn, dialog_id, 0, 0, 20, 1, writer=inline_writer(conn))


@pytest.mark.parametrize("with_message", [False, True])
@pytest.mark.parametrize(
    "target",
    [
        User(id=111, first_name="Real", last_name="Name"),
        Chat(
            id=111,
            title="Real Group",
            photo=ChatPhotoEmpty(),
            participants_count=1,
            date=datetime(2026, 1, 1, tzinfo=UTC),
            version=1,
        ),
    ],
)
async def test_sweep_retains_only_response_target_identity(
    monkeypatch: pytest.MonkeyPatch, with_message: bool, target: User | Chat
) -> None:
    dialog_id = get_peer_id(target)
    with closing(sqlite3.connect(":memory:")) as conn:
        _apply_migrations(conn)
        conn.execute("INSERT INTO dialogs(dialog_id,hidden) VALUES(?,1)", (dialog_id,))
        conn.commit()
        enable_runtime_writes(conn)
        calls: list[object] = []

        async def search(client: object, request: object, *, timeout_s: float) -> Messages:
            assert not conn.in_transaction
            calls.append(request)
            assert isinstance(request, SearchRequest)
            return Messages(
                topics=[],
                messages=[
                    Message(
                        id=4,
                        peer_id=PeerUser(111) if isinstance(target, User) else PeerChat(111),
                        date=datetime.now(UTC),
                        message="mine",
                        out=True,
                    )
                ]
                if with_message
                else [],
                users=[User(id=999, first_name="Unrelated"), *([target] if isinstance(target, User) else [])],
                chats=[target] if isinstance(target, Chat) else [],
            )

        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.call_with_timeout", search)
        result = await sweep_peer_once(_request(conn, dialog_id))
        assert result.completed
        assert result.skip_reason is (SkipReason.NONE if with_message else SkipReason.HISTORY_FLOOR)
        assert len(calls) == 1
        assert conn.execute("SELECT name,hidden FROM dialogs WHERE dialog_id=?", (dialog_id,)).fetchone() == (
            "Real Name" if isinstance(target, User) else "Real Group",
            1,
        )
        assert conn.execute("SELECT 1 FROM dialogs WHERE dialog_id=999").fetchone() is None
        assert not conn.in_transaction


async def test_sweep_stale_response_cannot_overwrite_newer_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        _apply_migrations(conn)
        enable_runtime_writes(conn)

        async def search(client: object, request: object, *, timeout_s: float) -> Messages:
            assert not conn.in_transaction
            with write_transaction(conn):
                assert publish_dialog_identity(
                    conn,
                    111,
                    DialogIdentityObservation(111, name="Newer", source="profile", observed_at=1),
                    capture_identity_baseline(conn, 111),
                )
            return Messages(messages=[], users=[User(id=111, first_name="Stale")], chats=[], topics=[])

        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.call_with_timeout", search)
        result = await sweep_peer_once(_request(conn))
        assert result.hit_floor
        assert conn.execute("SELECT name FROM dialogs WHERE dialog_id=111").fetchone() == ("Newer",)


@pytest.mark.parametrize("cancel", [False, True])
async def test_sweep_failed_search_leaves_identity_untouched(monkeypatch: pytest.MonkeyPatch, cancel: bool) -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        _apply_migrations(conn)
        with write_transaction(conn):
            conn.execute("INSERT INTO dialogs(dialog_id,name) VALUES(111,'Existing')")
        enable_runtime_writes(conn)

        async def search(client: object, request: object, *, timeout_s: float) -> Messages:
            assert not conn.in_transaction
            if cancel:
                raise asyncio.CancelledError
            raise RuntimeError("search failed")

        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.call_with_timeout", search)
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await sweep_peer_once(_request(conn))
        else:
            result = await sweep_peer_once(_request(conn))
            assert result.skip_reason is SkipReason.ACCESS_SKIP
        assert conn.execute("SELECT name,identity_revision FROM dialogs WHERE dialog_id=111").fetchone() == (
            "Existing",
            0,
        )
        assert not conn.in_transaction


async def test_sweep_ignores_response_without_target(monkeypatch: pytest.MonkeyPatch) -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        _apply_migrations(conn)
        enable_runtime_writes(conn)

        async def search(client: object, request: object, *, timeout_s: float) -> Messages:
            return Messages(messages=[], users=[User(id=999, first_name="Unrelated")], chats=[], topics=[])

        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.call_with_timeout", search)
        assert (await sweep_peer_once(_request(conn))).hit_floor
        assert conn.execute("SELECT name,identity_revision FROM dialogs WHERE dialog_id=111").fetchone() == (None, 0)
        assert conn.execute("SELECT 1 FROM dialogs WHERE dialog_id=999").fetchone() is None


async def test_sweep_failed_message_write_does_not_publish_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        _apply_migrations(conn)
        enable_runtime_writes(conn)

        async def search(client: object, request: object, *, timeout_s: float) -> Messages:
            return Messages(
                messages=[Message(id=4, peer_id=PeerUser(111), date=datetime.now(UTC), message="mine", out=True)],
                users=[User(id=111, first_name="Real")],
                chats=[],
                topics=[],
            )

        def fail_write(*args: object, **kwargs: object) -> None:
            raise RuntimeError("message write failed")

        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.call_with_timeout", search)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.insert_messages_with_fts", fail_write)
        result = await sweep_peer_once(_request(conn))
        assert result.skip_reason is SkipReason.ACCESS_SKIP
        assert conn.execute("SELECT name,identity_revision FROM dialogs WHERE dialog_id=111").fetchone() == (None, 0)
        assert not conn.in_transaction
