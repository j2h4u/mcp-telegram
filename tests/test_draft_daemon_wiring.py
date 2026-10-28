import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from telethon.tl import types
from telethon.tl.functions.messages import GetAllDraftsRequest

from mcp_telegram.config import DraftRecoveryConfig
from mcp_telegram.daemon import _bind_draft_commands
from mcp_telegram.drafts.owner import DraftMessageOwner
from mcp_telegram.drafts.sqlite_projection import SQLiteDraftProjection
from mcp_telegram.event_handlers import UpdateProcessingBarrier
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from tests.test_daemon import _typed_ctx
from tests.test_daemon_api import make_server


@pytest.mark.parametrize(
    "kind,topic,topic_retained,error",
    [
        ("forum", None, False, "topic_required"),
        ("user", 1, False, "topic_mismatch"),
        ("unknown", None, False, "dialog_unavailable"),
        (None, None, False, "dialog_unavailable"),
        ("forum", 1, False, "topic_unavailable"),
        ("forum", 1, True, None),
        ("user", None, False, None),
        ("bot", None, False, "topic_capability_unknown"),
        ("bot", None, True, "topic_required"),
        ("bot", 1, True, None),
    ],
)
async def test_draft_commands_bind_and_respect_exact_retained_target(
    tmp_path: Path, kind: str | None, topic: int | None, topic_retained: bool, error: str | None
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    conn = sqlite3.connect(path)
    calls: list[object] = []

    class Client:
        def retained_draft_peer(self, dialog_id: int) -> object:
            assert dialog_id == -1000000000007
            return types.InputPeerChannel(7, 99)

        async def __call__(self, request: object) -> object:
            calls.append(request)
            if isinstance(request, GetAllDraftsRequest):
                return types.Updates([], [], [], datetime.now(UTC), 1)
            return True

    client = Client()
    server = make_server(conn, client, feedback_conn=conn, writer=writer)
    server.self_id = 42
    try:
        owner = DraftMessageOwner(
            client,
            SQLiteDraftProjection(conn, DraftRecoveryConfig()),
            asyncio.Event(),
            UpdateProcessingBarrier(closed=False),
            writer=writer,
        )

        def seed(db: sqlite3.Connection) -> None:
            with write_transaction(db):
                if kind is not None:
                    db.execute("INSERT INTO dialogs(dialog_id,type) VALUES (-1000000000007,?)", (kind,))
                if kind == "bot" and topic_retained:
                    db.execute("INSERT INTO topic_catalog_state(dialog_id,applicable) VALUES (-1000000000007,1)")
                if topic_retained:
                    db.execute(
                        "INSERT INTO topic_metadata(dialog_id,topic_id,title,is_general,is_deleted,updated_at) "
                        "VALUES (-1000000000007,1,'General',1,0,1)"
                    )

        await writer.run(seed)
        ctx = _typed_ctx(
            conn=conn,
            writer=writer,
            client=client,
            draft_owner=owner,
            api_server=server,
        )
        await _bind_draft_commands(ctx)
        payload: dict[str, object] = {
            "method": "manage_draft",
            "dialog_id": -1000000000007,
            "action": "create",
            "text": "test",
            "operation_key": "scope",
        }
        if topic is not None:
            payload["topic_id"] = topic
        result = await server._dispatch(payload)
        if error is None:
            assert result["ok"] is True
            assert result["outcome"] == "acknowledged"
            assert len(calls) == 2
        else:
            assert result["ok"] is False
            assert result["error"] == error
            assert not calls
    finally:
        await server.shutdown()
        conn.close()
        await writer.aclose()
