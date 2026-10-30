"""Explicit prepare preserves an admitted attempt across same-photo observations."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from telethon import types

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.image_authorization import ActiveSources, ScopeCell
from mcp_telegram.message_images.contracts import ImageScope
from mcp_telegram.message_images.fence import SourceFence
from mcp_telegram.message_images.repository import current_source
from mcp_telegram.message_images.service import MessageImageService
from mcp_telegram.message_images.worker import MessageImageWorker
from mcp_telegram.messages.sqlite_bundle import insert_messages_with_fts
from mcp_telegram.messages.telegram_adapter import extract_message_row
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_image_download import PhotoSourceToken, TelegramImageGateway
from tests.demand_adapter_helpers import run_ready_slice


def _message(text: str) -> types.Message:
    now = datetime.now(UTC)
    photo = types.Photo(99, 555, b"private", now, [], 2)
    return types.Message(8, types.PeerUser(7), date=now, message=text, media=types.MessageMediaPhoto(photo=photo))


def _persist(conn: sqlite3.Connection, text: str, order: int) -> None:
    with write_transaction(conn):
        insert_messages_with_fts(conn, [replace(extract_message_row(7, _message(text)), observation_order=order)])


async def _spend_and_observe(writer: SQLiteWriter, conn: sqlite3.Connection, scope: ImageScope, state: str) -> None:
    def spend(db: sqlite3.Connection) -> None:
        with write_transaction(db):
            db.execute(
                "UPDATE message_image_jobs SET state=?,remaining=19,setup_remaining=9,reference_refreshes=1,redirect_dc=3 WHERE account_id=42 AND dialog_id=7 AND message_id=8",
                (state,),
            )

    await writer.run(spend)
    source, token = current_source(conn, 7, 8), scope.capture()
    assert source is not None and token is not None
    permit: PhotoSourceToken = (
        scope.sources.register(42, 7, 8, 1)
        if state == "blocked"
        else await SourceFence(writer, scope, token, 7, 8, source, 1).prepare()
    )
    assert permit.is_current()
    await writer.run(lambda db: _persist(db, "new caption", 2))
    assert not permit.is_current()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued", "running", "blocked"])
async def test_prepare_coalesces_same_photo_after_new_observation(tmp_path: Path, state: str) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
    _persist(conn, "before", 1)
    writer = await SQLiteWriter.open(path)
    cell, sources = ScopeCell(), ActiveSources()
    assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 100))
    await writer.run(sources.bind)
    scope = ImageScope(cell.capture, cell.claim, sources)
    offers: list[bool] = []
    service = MessageImageService(conn, writer, tmp_path / "images", scope, lambda: offers.append(True))
    worker = MessageImageWorker(conn, writer, tmp_path / "images", scope, cast(TelegramImageGateway, MagicMock()))
    try:
        await service.prepare(7, 8)
        await _spend_and_observe(writer, conn, scope, state)
        before = cast(
            tuple[str, int, int, int, int, int, int | None] | None,
            conn.execute(
                "SELECT state,attempt_generation,source_generation,remaining,setup_remaining,reference_refreshes,redirect_dc FROM message_image_jobs"
            ).fetchone(),
        )
        status = await service.prepare(7, 8)
        assert status["acquisition"] == {"state": state}
        assert len(offers) == 1
        assert (
            conn.execute(
                "SELECT state,attempt_generation,source_generation,remaining,setup_remaining,reference_refreshes,redirect_dc FROM message_image_jobs"
            ).fetchone()
            == before
        )
        if state != "blocked":
            await run_ready_slice(worker, now=100)
            assert conn.execute(
                "SELECT attempt_generation,source_generation,remaining,setup_remaining,reference_refreshes,redirect_dc FROM message_image_jobs"
            ).fetchone() == (1, 2, 19, 9, 1, 3)
    finally:
        await worker.close()
        await writer.run(sources.unbind)
        await writer.aclose()
        conn.close()
