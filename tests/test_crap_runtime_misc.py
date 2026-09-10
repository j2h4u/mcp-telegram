"""Branch tests for runtime boundaries that are easy to miss in broad suites."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from mcp_telegram.activity_peer_resolve import LinkedChatResolution
from mcp_telegram.activity_peer_sweep import WorkingSetEnrollmentSliceResult, run_working_set_enrollment_slice
from mcp_telegram.activity_substrate import ActivityClient
from mcp_telegram.activity_sync import _SearchResultLike, _upsert_entities_from_search
from mcp_telegram.daemon import (
    _create_tracked_task,
    _persist_runtime_observation_loss,
    _SyncMainContext,
)
from mcp_telegram.daemon_api import DaemonAPIServer
from mcp_telegram.message_contracts import ExtractedMessage, StoredMessage
from mcp_telegram.messages.sqlite_bundle import insert_messages_with_fts
from mcp_telegram.messages.sqlite_hydration import apply_message_transcription_if_absent
from mcp_telegram.runtime_observations import RuntimeObservationSink
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.telegram_demand import demand_context
from mcp_telegram.telegram_rpc_consumers import DemandKind, TelegramRpcSource


@pytest.fixture()
def sync_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    try:
        yield conn
    finally:
        conn.close()


def test_runtime_observation_loss_persists_only_nonzero_sink_counts(sync_conn: sqlite3.Connection) -> None:
    ctx = cast(_SyncMainContext, SimpleNamespace(conn=sync_conn, rpc_observation_sink=None))
    _persist_runtime_observation_loss(ctx)
    assert sync_conn.execute(
        "SELECT COUNT(*) FROM daemon_state WHERE key LIKE 'runtime_observations_last_%'"
    ).fetchone() == (0,)

    ctx.rpc_observation_sink = cast(
        RuntimeObservationSink,
        SimpleNamespace(
            queue_full_drops=2,
            shutdown_grace_drops=0,
            startup_drops=1,
            rejected_submissions=0,
            permanent_failures=3,
        ),
    )
    _persist_runtime_observation_loss(ctx)
    rows = dict(sync_conn.execute("SELECT key, value FROM daemon_state WHERE key LIKE 'runtime_observations_last_%'"))
    assert rows["runtime_observations_last_queue_full_drops"] == "2"
    assert rows["runtime_observations_last_writer_failures"] == "3"


@pytest.mark.asyncio
async def test_tracked_task_failure_records_noncritical_and_critical_outcomes(sync_conn: sqlite3.Connection) -> None:
    async def fail() -> None:
        raise RuntimeError("boom")

    api = cast(DaemonAPIServer, SimpleNamespace(_ready=True, startup_detail=""))
    ctx = cast(
        _SyncMainContext,
        SimpleNamespace(
            conn=sync_conn,
            background_tasks=set(),
            shutdown_event=asyncio.Event(),
            api_server=api,
        ),
    )
    task = _create_tracked_task(ctx, fail(), name="noncritical", critical=False)
    with pytest.raises(RuntimeError, match="boom"):
        await task
    await asyncio.sleep(0)
    assert sync_conn.execute("SELECT reason_code FROM runtime_observations").fetchone() == ("RuntimeError",)
    assert ctx.background_tasks == set()

    task = _create_tracked_task(ctx, fail(), name="critical", critical=True)
    with pytest.raises(RuntimeError, match="boom"):
        await task
    await asyncio.sleep(0)
    assert api._ready is False
    assert ctx.shutdown_event.is_set()
    assert sync_conn.execute("SELECT COUNT(*) FROM runtime_observations").fetchone() == (2,)


@dataclass
class _SearchResult:
    users: list[object] | None = None
    chats: list[object] | None = None


def test_activity_search_entity_upsert_skips_unknown_and_bad_peer(
    sync_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _User:
        id = 7
        first_name = " Ada "
        last_name = "Lovelace"
        username = "ada"

    class _Chat:
        id = 8
        title = "Project Room"
        username = "room"

    class _Unknown:
        id = 9

    monkeypatch.setattr(
        "mcp_telegram.activity_sync._classify_entity",
        lambda obj: "User" if isinstance(obj, _User) else "Channel" if isinstance(obj, _Chat) else None,
    )

    def fake_peer_id(obj: object) -> int:
        if isinstance(obj, _Chat):
            return obj.id
        raise TypeError

    monkeypatch.setattr("telethon.utils.get_peer_id", fake_peer_id)
    _upsert_entities_from_search(
        sync_conn,
        cast(_SearchResultLike, _SearchResult(users=[_User(), _Unknown()], chats=[_Chat()])),
    )
    assert sync_conn.execute(
        "SELECT id, type, name, username, name_normalized FROM entities ORDER BY id"
    ).fetchall() == [
        (7, "User", " Ada  Lovelace", "ada", "ada  lovelace"),
        (8, "Channel", "Project Room", "room", "project room"),
    ]


def _seed_dialog(conn: sqlite3.Connection, dialog_id: int, dialog_type: str) -> None:
    conn.execute(
        "INSERT INTO dialogs(dialog_id, type, hidden, last_message_at) VALUES (?, ?, 0, 100)",
        (dialog_id, dialog_type),
    )
    conn.commit()


class _FakeClient:
    async def __call__(self, request: object) -> object:
        del request
        return object()

    async def get_input_entity(self, dialog_id: int) -> object:
        del dialog_id
        return object()


@pytest.mark.asyncio
async def test_working_set_enrollment_slice_preserves_phase_and_finishes(
    sync_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        await run_working_set_enrollment_slice(
            cast(ActivityClient, _FakeClient()),
            sync_conn,
            source=TelegramRpcSource.ACTIVITY_COLD_BACKFILL,
            cadence_s=10,
            timeout_s=1,
            now=100,
        )
        == WorkingSetEnrollmentSliceResult()
    )

    _seed_dialog(sync_conn, 11, "supergroup")
    first = await run_working_set_enrollment_slice(
        cast(ActivityClient, _FakeClient()),
        sync_conn,
        source=TelegramRpcSource.ACTIVITY_COLD_BACKFILL,
        cadence_s=10,
        timeout_s=1,
        now=100,
    )
    assert first.consumed

    _seed_dialog(sync_conn, 12, "channel")

    async def resolve(*_args: object, **_kwargs: object) -> LinkedChatResolution:
        return LinkedChatResolution(linked_chat_id=None, flood_wait_seconds=None)

    monkeypatch.setattr("mcp_telegram.activity_peer_sweep.resolve_linked_chat_id", resolve)
    with demand_context(DemandKind.COLD_PEER_PAGE):
        second = await run_working_set_enrollment_slice(
            cast(ActivityClient, _FakeClient()),
            sync_conn,
            source=TelegramRpcSource.ACTIVITY_COLD_BACKFILL,
            cadence_s=10,
            timeout_s=1,
            now=100,
        )
    assert second.consumed
    final = await run_working_set_enrollment_slice(
        cast(ActivityClient, _FakeClient()),
        sync_conn,
        source=TelegramRpcSource.ACTIVITY_COLD_BACKFILL,
        cadence_s=10,
        timeout_s=1,
        now=100,
    )
    assert final.completed
    assert sync_conn.execute(
        "SELECT value FROM activity_sync_state WHERE key='activity_working_set_completed_at'"
    ).fetchone() == ("100",)


def _message(message_id: int, *, text: str | None, media_kind: str = "voice") -> ExtractedMessage:
    return ExtractedMessage(
        message=StoredMessage(
            dialog_id=42,
            message_id=message_id,
            sent_at=100,
            text=text,
            sender_id=42,
            sender_first_name="Test",
            reply_to_msg_id=None,
            forum_topic_id=None,
            edit_date=None,
            grouped_id=None,
            reply_to_peer_id=None,
            out=0,
            is_service=0,
            post_author=None,
            media_kind=media_kind,
            media_payload="{}",
        ),
        reply_count=0,
    )


def _enable_history(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (42, 'synced')")
    conn.execute(
        "INSERT INTO full_history_enrollment(dialog_id, enabled, source, updated_at) VALUES (42, 1, 'explicit', 1)"
    )


def test_transcription_worker_result_is_idempotent_and_race_aware(sync_conn: sqlite3.Connection) -> None:
    assert (
        apply_message_transcription_if_absent(
            sync_conn, 42, 1, transcribed_text=" ", transcription_id=1, received_at=100
        )
        == "not_applied"
    )
    _enable_history(sync_conn)
    with sync_conn:
        insert_messages_with_fts(sync_conn, [_message(1, text=None)])
    assert (
        apply_message_transcription_if_absent(
            sync_conn, 42, 1, transcribed_text="hello", transcription_id=1, received_at=100
        )
        == "applied"
    )
    assert (
        apply_message_transcription_if_absent(
            sync_conn, 42, 1, transcribed_text="newer", transcription_id=2, received_at=101
        )
        == "already_applied"
    )
    assert (
        apply_message_transcription_if_absent(
            sync_conn, 42, 99, transcribed_text="missing", transcription_id=3, received_at=100
        )
        == "not_applied"
    )
