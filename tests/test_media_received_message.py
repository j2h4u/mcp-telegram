"""Media hydration retains the complete received Telegram message."""

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from telethon.tl import types

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.fact_hydration import MessageFactHydrationWorker
from mcp_telegram.hydration_queue import MEDIA_METADATA_KIND, HydrationJob, HydrationQueueRepository
from mcp_telegram.image_authorization import ScopeCell, ScopeToken
from mcp_telegram.media_hydration import MediaFactHydrationHandler
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from tests.sqlite_writer_helpers import inline_writer


@pytest.mark.parametrize("revoke", [False, True])
async def test_media_hydration_publishes_full_supplied_message(tmp_path: Path, revoke: bool) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    cell = ScopeCell()
    assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))

    class Client:
        calls = 0

        def capture_image_scope(self) -> ScopeToken | None:
            return cell.capture()

        def claim_image_scope(self, token: ScopeToken) -> bool:
            return cell.claim(token)

        async def get_messages(self, *, entity: int, ids: list[int]) -> object:
            assert entity == 55 and ids == [7]
            self.calls += 1
            if revoke:
                change = cell.begin_change()
                cell.end_change(change)
                assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 43, 2, 99))
                assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
            return [
                types.Message(
                    id=7,
                    peer_id=types.PeerUser(55),
                    date=datetime.fromtimestamp(10, UTC),
                    edit_date=datetime.fromtimestamp(20, UTC),
                    message="edited body",
                    media=types.MessageMediaContact("123", "Name", "", "", 7),
                )
            ]

    client = Client()
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (55,'synced')")
        conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES (55,1,0)")
        conn.execute(
            "INSERT INTO messages(dialog_id,message_id,sent_at,text,media_kind,media_payload) VALUES(55,7,10,'old body','other','{}')"
        )
        HydrationQueueRepository(conn).enqueue(HydrationJob(MEDIA_METADATA_KIND, 55, 7, due_at=1, attempts=0))
    handler = MediaFactHydrationHandler(batch_size=1)
    worker = MessageFactHydrationWorker(
        client,
        conn,
        asyncio.Event(),
        writer=inline_writer(conn),
        handlers=[handler],
        interval_seconds=1,
        max_requests_per_cycle=1,
        max_jobs_per_cycle=1,
        retry_delay_seconds=1,
        circuit_retry_seconds=1,
        max_attempts=3,
        pause_between_requests_seconds=0,
        backfill_debt_limit=1,
        profile_owner_account_id=42,
    )
    result = await worker._process_batch(handler, [HydrationJob(MEDIA_METADATA_KIND, 55, 7, due_at=1, attempts=0)], 100)
    assert client.calls == 1
    if revoke:
        assert result.stopped and result.completed == 0
        assert conn.execute("SELECT text,media_kind FROM messages WHERE dialog_id=55 AND message_id=7").fetchone() == (
            "old body",
            "other",
        )
        assert conn.execute("SELECT COUNT(*) FROM hydration_jobs WHERE dialog_id=55 AND message_id=7").fetchone() == (
            1,
        )
    else:
        assert result.completed == 1
        assert conn.execute("SELECT text,media_kind FROM messages WHERE dialog_id=55 AND message_id=7").fetchone() == (
            "edited body",
            "contact",
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM hydration_jobs WHERE dialog_id=55 AND message_id=7 AND kind=?", (MEDIA_METADATA_KIND,)
        ).fetchone() == (0,)
    conn.close()


@pytest.mark.parametrize("revoke_at", [0, 4, 5])
async def test_media_admission_failure_keeps_original_transaction_fence(tmp_path: Path, revoke_at: int) -> None:
    from mcp_telegram.telegram_rpc_scheduler import RpcAdmissionClosedError, current_rpc_scope

    path = tmp_path / "fence.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    cell = ScopeCell()
    scope = TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99)
    assert cell.publish(scope)
    failing = False
    claims = 0

    class Client:
        def capture_image_scope(self) -> ScopeToken | None:
            return cell.capture()

        def claim_image_scope(self, token: ScopeToken) -> bool:
            nonlocal claims
            if failing:
                claims += 1
                if claims == revoke_at:
                    change = cell.begin_change()
                    cell.end_change(change)
                    assert cell.publish(scope)
            return cell.claim(token)

        async def get_messages(self, *, entity: int, ids: list[int]) -> object:
            nonlocal failing
            failing = True
            raise RpcAdmissionClosedError(current_rpc_scope(), "closed")

    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (55,'synced')")
        conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES (55,1,0)")
        conn.execute(
            "INSERT INTO messages(dialog_id,message_id,sent_at,text,media_kind,media_payload) VALUES(55,7,10,'old','other','{}')"
        )
        job = HydrationJob(MEDIA_METADATA_KIND, 55, 7, due_at=1, attempts=0)
        HydrationQueueRepository(conn).enqueue(job)
    handler = MediaFactHydrationHandler(batch_size=1)
    worker = MessageFactHydrationWorker(
        Client(),
        conn,
        asyncio.Event(),
        writer=inline_writer(conn),
        handlers=[handler],
        interval_seconds=1,
        max_requests_per_cycle=1,
        max_jobs_per_cycle=1,
        retry_delay_seconds=1,
        circuit_retry_seconds=1,
        max_attempts=3,
        pause_between_requests_seconds=0,
        backfill_debt_limit=1,
        profile_owner_account_id=42,
    )
    if revoke_at:
        result = await worker._process_batch(handler, [job], 100)
        assert result.stopped
        assert conn.execute("SELECT attempts,last_outcome FROM hydration_jobs").fetchone() == (1, "processing")
    else:
        with pytest.raises(RpcAdmissionClosedError):
            await worker._process_batch(handler, [job], 100)
        assert conn.execute("SELECT attempts FROM hydration_jobs").fetchone() == (0,)
    assert conn.execute("SELECT text FROM messages WHERE message_id=7").fetchone() == ("old",)
    conn.close()


@pytest.mark.parametrize("known_version", [False, True])
async def test_later_media_only_hydration_survives_earlier_newer_body_response(
    tmp_path: Path, known_version: bool
) -> None:
    from dataclasses import replace

    from mcp_telegram.messages.sqlite_bundle import insert_messages_with_fts, persist_received_messages
    from mcp_telegram.messages.telegram_adapter import extract_message_row, extract_received_messages
    from mcp_telegram.observation_order import allocate_observation_order
    from mcp_telegram.reactions.contracts import ReactionAggregateSource
    from mcp_telegram.reactions.persistence import allocate_observation_boundary

    path = tmp_path / "media-order.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    cell = ScopeCell()
    assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
    token = cell.capture()
    assert token is not None

    class Client:
        def capture_image_scope(self) -> ScopeToken | None:
            return cell.capture()

        def claim_image_scope(self, token: ScopeToken) -> bool:
            return cell.claim(token)

        async def get_messages(self, *, entity: int, ids: list[int]) -> object:
            return [
                types.Message(
                    id=7,
                    peer_id=types.PeerUser(55),
                    date=datetime.fromtimestamp(10, UTC),
                    edit_date=datetime.fromtimestamp(20, UTC) if known_version else None,
                    message="same body",
                    media=types.MessageMediaContact("123", "Name", "", "", 7),
                )
            ]

    initial = types.Message(
        id=7,
        peer_id=types.PeerUser(55),
        date=datetime.fromtimestamp(10, UTC),
        edit_date=datetime.fromtimestamp(20, UTC) if known_version else None,
        message="same body",
    )
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (55,'synced')")
        conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES (55,1,0)")
        item = extract_message_row(55, initial)
        item = replace(
            item,
            message=replace(item.message, media_kind="other", media_payload="{}"),
            observation_order=allocate_observation_order(conn),
        )
        insert_messages_with_fts(conn, [item])
        old_request = allocate_observation_order(conn)
        old_reactions = allocate_observation_boundary(conn, ReactionAggregateSource.HISTORY)
    handler = MediaFactHydrationHandler(batch_size=1)
    worker = MessageFactHydrationWorker(
        Client(),
        conn,
        asyncio.Event(),
        writer=inline_writer(conn),
        handlers=[handler],
        interval_seconds=1,
        max_requests_per_cycle=1,
        max_jobs_per_cycle=1,
        retry_delay_seconds=1,
        circuit_retry_seconds=1,
        max_attempts=3,
        pause_between_requests_seconds=0,
        backfill_debt_limit=1,
        profile_owner_account_id=42,
    )
    job = HydrationQueueRepository(conn).due_jobs(int(datetime.now(UTC).timestamp()) + 5, 1)[0]
    result = await worker._process_batch(handler, [job], 100)
    assert result.completed == 1
    stored = cast(
        sqlite3.Row | None,
        conn.execute(
            "SELECT m.media_kind,o.observation_order,s.observation_order FROM messages m "
            "JOIN message_observations o USING(dialog_id,message_id) "
            "JOIN message_image_sources s USING(dialog_id,message_id) WHERE m.dialog_id=55 AND m.message_id=7"
        ).fetchone(),
    )
    assert stored is not None and stored[0] == "contact"
    assert (stored[1] < stored[2]) is known_version
    newer = types.Message(
        id=7,
        peer_id=types.PeerUser(55),
        date=datetime.fromtimestamp(10, UTC),
        edit_date=datetime.fromtimestamp(40, UTC),
        message="newer body",
    )
    with write_transaction(conn):
        assert persist_received_messages(
            conn,
            extract_received_messages([newer]),
            scope=token,
            claim_scope=cell.claim,
            observation_order=old_request,
            expected_dialog_id=55,
            reaction_boundary=old_reactions,
        ) == int(known_version)
    actual = cast(
        sqlite3.Row | None,
        conn.execute("SELECT text,media_kind FROM messages WHERE dialog_id=55 AND message_id=7").fetchone(),
    )
    assert actual is not None and tuple(actual) == ("newer body" if known_version else "same body", "contact")
    conn.close()


@pytest.mark.parametrize("error", [False, True])
async def test_media_response_does_not_settle_replaced_job(tmp_path: Path, error: bool) -> None:
    from mcp_telegram.telegram_rpc_scheduler import RpcAdmissionClosedError, current_rpc_scope

    path = tmp_path / "replacement.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    cell = ScopeCell()
    assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))

    class Client:
        def capture_image_scope(self) -> ScopeToken | None:
            return cell.capture()

        def claim_image_scope(self, token: ScopeToken) -> bool:
            return cell.claim(token)

        async def get_messages(self, *, entity: int, ids: list[int]) -> object:
            with write_transaction(conn):
                conn.execute("UPDATE hydration_jobs SET attempts=7,due_at=987,last_outcome='pending'")
            if error:
                raise RpcAdmissionClosedError(current_rpc_scope(), "closed")
            return [
                types.Message(
                    id=7,
                    peer_id=types.PeerUser(55),
                    date=datetime.fromtimestamp(10, UTC),
                    message="new",
                    media=types.MessageMediaContact("123", "Name", "", "", 7),
                )
            ]

    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (55,'synced')")
        conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES (55,1,0)")
        conn.execute(
            "INSERT INTO messages(dialog_id,message_id,sent_at,text,media_kind,media_payload) VALUES(55,7,10,'old','other','{}')"
        )
        job = HydrationJob(MEDIA_METADATA_KIND, 55, 7, due_at=1, attempts=0)
        HydrationQueueRepository(conn).enqueue(job)
    handler = MediaFactHydrationHandler(batch_size=1)
    worker = MessageFactHydrationWorker(
        Client(),
        conn,
        asyncio.Event(),
        writer=inline_writer(conn),
        handlers=[handler],
        interval_seconds=1,
        max_requests_per_cycle=1,
        max_jobs_per_cycle=1,
        retry_delay_seconds=1,
        circuit_retry_seconds=1,
        max_attempts=3,
        pause_between_requests_seconds=0,
        backfill_debt_limit=1,
        profile_owner_account_id=42,
    )
    if error:
        with pytest.raises(RpcAdmissionClosedError):
            await worker._process_batch(handler, [job], 100)
        assert conn.execute("SELECT attempts,due_at,last_outcome FROM hydration_jobs").fetchone() == (7, 987, "pending")
    else:
        result = await worker._process_batch(handler, [job], 100)
        assert result.completed == 0
        assert conn.execute("SELECT text,media_kind FROM messages").fetchone() == ("new", "contact")
        assert conn.execute("SELECT count(*) FROM hydration_jobs").fetchone() == (0,)
    conn.close()
