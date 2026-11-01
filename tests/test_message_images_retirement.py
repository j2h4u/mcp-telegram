"""Replacement keeps only unreferenced image artifacts and survives cancellation."""

import asyncio
import sqlite3
import threading
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from telethon import types

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.image_authorization import ActiveSources, ScopeCell
from mcp_telegram.message_images import worker as worker_module
from mcp_telegram.message_images.contracts import ImageScope
from mcp_telegram.message_images.service import MessageImageService
from mcp_telegram.message_images.worker import MessageImageWorker
from mcp_telegram.messages.sqlite_bundle import insert_messages_with_fts
from mcp_telegram.messages.telegram_adapter import extract_message_row
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_image_download import (
    PhotoBudget,
    PhotoDownloadSession,
    PhotoLocator,
    PhotoObservation,
    PhotoSourceFence,
    PhotoTarget,
    PhotoWork,
)
from tests.demand_adapter_helpers import run_ready_slice


class Gateway:
    photo_id = 99

    def message(self) -> types.Message:
        photo = types.Photo(self.photo_id, 555, b"private", datetime.now(UTC), [], 2)
        return types.Message(
            8, types.PeerUser(7), date=datetime.now(UTC), message="", media=types.MessageMediaPhoto(photo=photo)
        )

    async def resolve_photo(
        self,
        target: PhotoTarget,
        *,
        scope: TelegramAuthScope,
        observation_order: int,
        budget: PhotoBudget,
        source_fence: PhotoSourceFence,
    ) -> PhotoObservation:
        assert (await source_fence.prepare()).is_current()
        await budget.reserve(setup=True)
        return PhotoObservation(
            self.message(), PhotoLocator(self.photo_id, "x", 2, len(Session(self.photo_id).image), 10, 20, object())
        )

    async def open_photo_download(
        self,
        locator: PhotoLocator,
        *,
        scope: TelegramAuthScope,
        work: PhotoWork,
        budget: PhotoBudget,
        source_fence: PhotoSourceFence,
    ) -> PhotoDownloadSession:
        assert (await source_fence.prepare()).is_current()
        await budget.reserve(setup=False)
        return Session(locator.photo_id)


class Session:
    def __init__(self, photo_id: int) -> None:
        self.image = b"\xff\xd8\xff" + str(photo_id).encode() + b"\xff\xd9"

    def chunks(self, *, offset: int) -> AsyncIterator[bytes]:
        async def stream() -> AsyncIterator[bytes]:
            yield self.image[offset:]

        return stream()

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["consecutive", "referenced", "cancel"])
async def test_retirement_after_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> None:  # noqa: PLR0914, PLR0915
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
    writer = await SQLiteWriter.open(path)
    cell, sources = ScopeCell(), ActiveSources()
    assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 100))
    await writer.run(sources.bind)
    scope = ImageScope(cell.capture, cell.claim, sources)
    gateway = Gateway()
    directory = tmp_path / "images"
    service = MessageImageService(conn, writer, directory, scope, lambda: None)
    worker = MessageImageWorker(conn, writer, directory, scope, gateway)
    started, proceed = threading.Event(), threading.Event()
    original = worker_module.retire_image

    async def acquire(photo_id: int) -> None:
        gateway.photo_id = photo_id

        def observe(db: sqlite3.Connection) -> None:
            with write_transaction(db):
                insert_messages_with_fts(
                    db, [replace(extract_message_row(7, gateway.message()), observation_order=photo_id * 10)]
                )
                db.execute(
                    "INSERT OR REPLACE INTO daemon_state(key,value) VALUES('canonical_observation_sequence',?)",
                    (str(photo_id * 10),),
                )

        await writer.run(observe)
        await service.prepare(7, 8)
        outcome = await run_ready_slice(worker, limit=32, now=100)
        assert outcome is not None
        assert conn.execute("SELECT state FROM message_image_jobs").fetchone() == ("complete",), (
            outcome,
            conn.execute("SELECT * FROM message_image_jobs").fetchall(),
        )

    def block_retirement(root: Path, account: int, name: str) -> None:
        started.set()
        assert proceed.wait(timeout=10)
        original(root, account, name)

    try:
        await acquire(99)
        old = cast(
            tuple[str], conn.execute("SELECT filename FROM message_image_artifacts WHERE message_id=8").fetchone()
        )[0]
        if case == "referenced":

            def share(db: sqlite3.Connection) -> None:
                with write_transaction(db):
                    db.execute(
                        "INSERT INTO message_image_artifacts SELECT account_id,dialog_id,9,photo_id,source_generation,filename,sha256,byte_size,width,height FROM message_image_artifacts WHERE message_id=8"
                    )

            await writer.run(share)
        if case == "cancel":
            monkeypatch.setattr(worker_module, "retire_image", block_retirement)
            task = asyncio.create_task(acquire(100))
            assert await asyncio.to_thread(started.wait, 10)
            assert conn.execute("SELECT photo_id FROM message_image_artifacts WHERE message_id=8").fetchone() == (100,)
            task.cancel()
            proceed.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await acquire(100)
        files = {p.name for p in (directory / "42").glob("*.jpg")}
        current = cast(
            tuple[str], conn.execute("SELECT filename FROM message_image_artifacts WHERE message_id=8").fetchone()
        )[0]
        assert files == ({old, current} if case == "referenced" else {current})
        assert (await service.read(7, 8)).image_bytes == Session(100).image
        if case == "consecutive":
            await acquire(101)
            current = cast(
                tuple[str], conn.execute("SELECT filename FROM message_image_artifacts WHERE message_id=8").fetchone()
            )[0]
            assert {p.name for p in (directory / "42").glob("*.jpg")} == {current}
            assert (await service.read(7, 8)).image_bytes == Session(101).image
    finally:
        proceed.set()
        await worker.close()
        await writer.run(sources.unbind)
        await writer.aclose()
        conn.close()
