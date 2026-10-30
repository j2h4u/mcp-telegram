"""Transport failures preserve durable image facts and finite attempt budgets."""

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
from telethon import types

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.image_authorization import ActiveSources, ScopeCell
from mcp_telegram.message_images.contracts import ImageScope
from mcp_telegram.message_images.service import MessageImageService
from mcp_telegram.message_images.store import publish_image
from mcp_telegram.message_images.worker import MessageImageWorker
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_demand_progress import SliceOutcomeKind
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
from tests.test_message_images_owner import _observe_same_photo
from tests.test_message_images_prepare_coalescing import _message, _persist


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unchanged", "resolving", "transferring"])
async def test_transport_failure_preserves_attempt_and_retained_image(tmp_path: Path, failure: str) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
    _persist(conn, "old", 1)
    writer = await SQLiteWriter.open(path)
    cell, sources = ScopeCell(), ActiveSources()
    assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 100))
    await writer.run(sources.bind)
    scope = ImageScope(cell.capture, cell.claim, sources)
    image = b"\xff\xd8\xffretained\xff\xd9"
    image_dir = tmp_path / "images"
    service = MessageImageService(conn, writer, image_dir, scope, lambda: None)
    calls = [0]

    class Session:
        def chunks(self, *, offset: int) -> AsyncIterator[bytes]:
            async def stream() -> AsyncIterator[bytes]:
                if failure == "transferring" and calls[0] == 1:
                    await _observe_same_photo(writer, _message("new caption"), 10)
                    raise ConnectionError("transport lost")
                yield image[offset:]

            return stream()

        async def close(self) -> None:
            pass

    class Gateway:
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
            calls[0] += 1
            if calls[0] == 1 and failure in {"unchanged", "resolving"}:
                if failure == "resolving":
                    await _observe_same_photo(writer, _message("new caption"), 10)
                raise ConnectionError("transport lost")
            message: types.Message = _message("observed")
            return PhotoObservation(message, PhotoLocator(99, "x", 2, len(image), 10, 20, object()))

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
            return Session()

    worker = MessageImageWorker(conn, writer, image_dir, scope, Gateway())
    try:
        await service.prepare(7, 8)
        stored = publish_image(image_dir, 42, image)
        with write_transaction(conn):
            conn.execute(
                "INSERT INTO message_image_artifacts VALUES(42,7,8,99,1,?,?,?,?,?)",
                (stored.filename, stored.sha256, stored.byte_size, 10, 20),
            )
        outcome = await run_ready_slice(worker, limit=32, now=100)
        assert outcome is not None
        job = cast(
            tuple[str, int, int, int, int, str | None] | None,
            conn.execute(
                "SELECT state,attempt_generation,source_generation,remaining,setup_remaining,reason FROM message_image_jobs"
            ).fetchone(),
        )
        assert job is not None and job[1] == 1
        assert (await service.read(7, 8)).image_bytes == image
        if failure == "unchanged":
            assert outcome.kind is SliceOutcomeKind.TERMINAL
            assert job == ("failed", 1, 1, 31, 15, "acquisition_failed")
            assert worker.status(100) is None
        else:
            assert outcome.kind is SliceOutcomeKind.PROGRESSED
            assert job == ("queued", 1, 10, 30 if failure == "transferring" else 31, 15, None)
            result = await run_ready_slice(worker, limit=32, now=100)
            assert result is not None and result.kind is SliceOutcomeKind.TERMINAL
            assert (await service.read(7, 8)).image_bytes == image
            assert conn.execute(
                "SELECT attempt_generation,remaining,setup_remaining FROM message_image_jobs"
            ).fetchone() == (1, 28 if failure == "transferring" else 29, 14)
    finally:
        await worker.close()
        await writer.run(sources.unbind)
        await writer.aclose()
        conn.close()
