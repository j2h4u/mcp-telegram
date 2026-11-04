"""Directory and read-position responses persist supplied bodies through their owners."""

import asyncio
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from telethon.tl import types

from mcp_telegram.auth_scope import TelegramAuthScope
from mcp_telegram.daemon import _DaemonClient, _reconcile_read_position_batch
from mcp_telegram.dialog_directory import CanonicalDialogDirectory
from mcp_telegram.image_authorization import ScopeCell
from mcp_telegram.message_fact_refresh import ReadReceiptDemandAdapter
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema
from tests.history_enrollment_helpers import seed_full_history_enrollment
from tests.test_dialog_directory import _dialog, _FakeClient, _pinned_response, _response


@pytest.fixture
async def writer(tmp_path: Path):
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    owned = await SQLiteWriter.open(path)
    try:
        yield owned
    finally:
        await owned.aclose()


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (55,'synced')")
    seed_full_history_enrollment(conn, 55, enabled=True)
    conn.execute("UPDATE dialog_directory_state SET account_id=100 WHERE singleton=1")
    conn.commit()


def _message() -> types.Message:
    return types.Message(id=7, peer_id=types.PeerUser(55), date=datetime.now(UTC), message="received body")


@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.parametrize("revoke", [False, True])
async def test_directory_response_persists_full_message_without_refetch(
    tmp_path: Path, pinned: bool, revoke: bool, writer: SQLiteWriter
) -> None:
    path = tmp_path / "sync.db"
    with closing(sqlite3.connect(path)) as conn:
        _seed(conn)
    response = cast(
        types.messages.PeerDialogs | types.messages.Dialogs,
        _pinned_response([_dialog(55, 7)]) if pinned else _response([_dialog(55, 7)]),
    )
    response.messages[:] = [_message()]

    class Client(_FakeClient):
        async def __call__(self, request: object) -> object:
            value = await super().__call__(request)
            if revoke and getattr(value, "messages", []):
                change = self._scope.begin_change()
                self._scope.end_change(change)
            return value

    replies: list[object] = [response] if pinned else [_pinned_response(), _pinned_response(), response]
    client = Client(replies)
    owner = CanonicalDialogDirectory(client, path, asyncio.Event(), writer=writer)
    if not pinned:
        await owner.run_slice()
        await owner.run_slice()
    await owner.run_slice()
    with closing(sqlite3.connect(path)) as conn:
        row = cast(
            tuple[str] | None, conn.execute("SELECT text FROM messages WHERE dialog_id=55 AND message_id=7").fetchone()
        )
        if revoke:
            assert row is None
        else:
            assert row == ("received body",)
            assert conn.execute(
                "SELECT message_id FROM messages_fts WHERE messages_fts MATCH 'body' AND dialog_id=55"
            ).fetchone() == (7,)
    assert len(client.requests) == (1 if pinned else 3)


@pytest.mark.parametrize("case", ["current", "revoked", "stale", "wrong_peer"])
async def test_read_position_response_persists_only_current_scope(
    tmp_path: Path, case: str, writer: SQLiteWriter
) -> None:
    path = tmp_path / "sync.db"
    with closing(sqlite3.connect(path)) as conn:
        _seed(conn)
        cell = ScopeCell()
        assert cell.publish(TelegramAuthScope(1, 100, 2, 99))

        class Client:
            capture_image_scope = cell.capture
            claim_image_scope = cell.claim

            async def get_input_entity(self, _id: int) -> object:
                return types.InputPeerUser(55, 1)

            async def __call__(self, _request: object) -> object:
                if case == "revoked":
                    change = cell.begin_change()
                    cell.end_change(change)
                elif case == "stale":
                    with closing(sqlite3.connect(path)) as newer, newer:
                        newer.execute("UPDATE synced_dialogs SET read_inbox_max_id=10 WHERE dialog_id=55")
                message = _message()
                if case == "wrong_peer":
                    message.peer_id = types.PeerUser(56)
                return types.messages.PeerDialogs(
                    [_dialog(55, 7)],
                    [message],
                    [],
                    [],
                    types.updates.State(0, 0, datetime.now(UTC), 0, 0),  # pyright: ignore[reportArgumentType]
                )

        adapter = ReadReceiptDemandAdapter(conn, lambda _: None, account_id=100)  # type: ignore[arg-type]
        status = adapter.status(time.time())
        assert status is not None and status.selected is not None
        invoke = _reconcile_read_position_batch(
            cast(_DaemonClient, Client()),
            conn,
            [55],
            writer=writer,
            success_recheck_seconds=60,
            selected=status.selected,
        )
        outcome = await invoke
        assert (outcome.kind.value == "stale") is (case in {"revoked", "stale"})
        row = cast(
            tuple[str] | None, conn.execute("SELECT text FROM messages WHERE dialog_id=55 AND message_id=7").fetchone()
        )
        assert row == (("received body",) if case in {"current", "stale"} else None)
        if case == "revoked":
            assert conn.execute("SELECT read_inbox_max_id FROM synced_dialogs WHERE dialog_id=55").fetchone() == (None,)
        elif case == "stale":
            assert conn.execute("SELECT read_inbox_max_id FROM synced_dialogs WHERE dialog_id=55").fetchone() == (10,)


@pytest.mark.parametrize("stage", ["after_message", "during_metadata"])
async def test_directory_metadata_does_not_commit_after_scope_revocation(
    tmp_path: Path, writer: SQLiteWriter, stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sync.db"
    with closing(sqlite3.connect(path)) as conn:
        _seed(conn)
    response = _pinned_response([_dialog(55, 7)])
    response.messages[:] = [_message()]
    client = _FakeClient([response])
    owner = CanonicalDialogDirectory(client, path, asyncio.Event(), writer=writer)
    if stage == "after_message":
        original = owner._publish_received_response

        async def revoke_after_message(*args: object) -> None:
            await original(*args)  # type: ignore[arg-type]
            change = client._scope.begin_change()
            client._scope.end_change(change)

        monkeypatch.setattr(owner, "_publish_received_response", revoke_after_message)
    else:
        original_stage = owner._stage_facts

        def revoke_during_metadata(*args: object, **kwargs: object) -> bool:
            result = original_stage(*args, **kwargs)  # type: ignore[arg-type]
            change = client._scope.begin_change()
            client._scope.end_change(change)
            return result

        monkeypatch.setattr(owner, "_stage_facts", revoke_during_metadata)
    assert await owner.run_slice() is None
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT pinned_main_status FROM dialog_directory_state").fetchone() == ("pending",)
        assert conn.execute("SELECT COUNT(*) FROM dialog_directory_staging").fetchone() == (0,)
        assert conn.execute("SELECT text FROM messages WHERE dialog_id=55 AND message_id=7").fetchone() == (
            "received body",
        )


@pytest.mark.parametrize("failure", [False, True])
async def test_directory_revoked_scope_cannot_record_error_or_non_authoritative_result(
    tmp_path: Path, writer: SQLiteWriter, failure: bool
) -> None:
    path = tmp_path / "sync.db"
    with closing(sqlite3.connect(path)) as conn:
        _seed(conn)

    class Client(_FakeClient):
        async def __call__(self, _request: object) -> object:
            change = self._scope.begin_change()
            self._scope.end_change(change)
            if failure:
                raise ConnectionError("controlled")
            return types.messages.DialogsNotModified(0)

    owner = CanonicalDialogDirectory(Client([]), path, asyncio.Event(), writer=writer)
    assert await owner.run_slice() is None
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT status,pinned_main_status,reason FROM dialog_directory_state").fetchone() == (
            "in_progress",
            "pending",
            None,
        )
