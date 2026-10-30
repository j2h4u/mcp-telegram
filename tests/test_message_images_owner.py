"""Durable image primitives use canonical account and source fences."""

import sqlite3
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from telethon import types
from telethon.errors import FileReferenceExpiredError

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.image_authorization import ActiveSources, ScopeCell
from mcp_telegram.message_contracts import ExtractedMessage
from mcp_telegram.message_images.contracts import ImageScope
from mcp_telegram.message_images.fence import DurableBudget, SourceFence
from mcp_telegram.message_images.repository import current_source
from mcp_telegram.message_images.service import MessageImageService
from mcp_telegram.message_images.store import publish_image
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
    PhotoUnsupportedError,
    PhotoWork,
)
from tests.demand_adapter_helpers import run_ready_slice


def _image_scope(scope: TelegramAuthScope) -> tuple[ImageScope, ScopeCell]:
    cell = ScopeCell()
    assert cell.publish(scope)
    return ImageScope(cell.capture, cell.claim, ActiveSources()), cell


async def _bind_image_scope(writer: SQLiteWriter, scope: TelegramAuthScope) -> tuple[ImageScope, ScopeCell]:
    image_scope, cell = _image_scope(scope)
    await writer.run(image_scope.sources.bind)
    return image_scope, cell


async def _assert_corrupt_requires_explicit_prepare(
    service: MessageImageService, conn: sqlite3.Connection, path: Path, offered: list[bool]
) -> None:
    path.write_bytes(b"corrupt")
    broken = await service.read(7, 8)
    assert broken.image_bytes is None
    assert broken.status["content"] == {"state": "unavailable", "reason": "missing_or_corrupt"}
    assert len(offered) == 1
    await service.prepare(7, 8)
    assert len(offered) == 2
    assert conn.execute("SELECT attempt_generation FROM message_image_jobs").fetchone() == (2,)


async def _assert_unsupported_stays_terminal(service: MessageImageService, conn: sqlite3.Connection) -> None:
    result = await service.read(7, 8)
    assert result.status["acquisition"] == {"state": "failed", "reason": "unsupported_photo"}
    await service.prepare(7, 8)
    assert conn.execute("SELECT attempt_generation FROM message_image_jobs").fetchone() == (1,)
    with write_transaction(conn):
        conn.execute("UPDATE message_image_jobs SET photo_id=99 WHERE account_id=42 AND dialog_id=7 AND message_id=8")
        conn.execute("INSERT INTO message_image_sources VALUES(7,8,99,2,1)")
        conn.execute("UPDATE message_observations SET observation_order=2 WHERE dialog_id=7 AND message_id=8")
    await service.prepare(7, 8)
    assert conn.execute("SELECT attempt_generation FROM message_image_jobs").fetchone() == (1,)


async def _observe_same_photo(writer: SQLiteWriter, message: types.Message, order: int) -> None:
    def observe(db: sqlite3.Connection) -> None:
        with write_transaction(db):
            insert_messages_with_fts(db, [replace(extract_message_row(7, message), observation_order=order)])
            db.execute("UPDATE daemon_state SET value=? WHERE key='canonical_observation_sequence'", (str(order),))

    await writer.run(observe)


async def _assert_worker_result(
    service: MessageImageService,
    conn: sqlite3.Connection,
    path: Path,
    race: str,
    counters: tuple[list[int], list[int]],
) -> None:
    result = await service.read(7, 8)
    if race == "unsupported":
        await _assert_unsupported_stays_terminal(service, conn)
    if race in {"none", "reference", "multi", "same_photo", "same_photo_resolve"}:
        assert result.image_bytes == b"\xff\xd8\xfftest\xff\xd9"
        assert result.status["acquisition"] == {"state": "complete"}
        if race in {"same_photo", "same_photo_resolve"}:
            assert conn.execute("SELECT attempt_generation FROM message_image_jobs").fetchone() == (1,)
            assert counters[1][0] == 1
        if race == "reference":
            assert counters[0][0] == 2
            assert conn.execute("SELECT reference_refreshes FROM message_image_jobs").fetchone() == (1,)
    else:
        assert result.image_bytes is None
        assert conn.execute("SELECT COUNT(*) FROM message_image_artifacts").fetchone() == (0,)
        assert not list((path / "images").rglob("*.jpg"))


@pytest.mark.asyncio
async def test_local_image_read_and_budget_use_real_writer(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(db_path)
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute(
            "INSERT INTO messages(dialog_id,message_id,sent_at,media_kind,media_payload) VALUES(7,8,1,'photo','{}')"
        )
        conn.execute("INSERT INTO message_observations(dialog_id,message_id,observation_order) VALUES(7,8,10)")
        conn.execute("INSERT INTO message_image_sources VALUES(7,8,99,10,1)")
    writer = await SQLiteWriter.open(db_path)
    image_scope, cell = await _bind_image_scope(writer, TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 100))
    offered: list[bool] = []
    service = MessageImageService(conn, writer, tmp_path / "images", image_scope, lambda: offered.append(True))
    try:
        status = await service.prepare(7, 8)
        assert status["acquisition"] == {"state": "queued"}
        await service.prepare(7, 8)
        assert len(offered) == 1
        await DurableBudget(writer, 42, 7, 8, 1).reserve(setup=True)
        assert conn.execute("SELECT remaining,setup_remaining FROM message_image_jobs").fetchone() == (31, 15)
        source = current_source(conn, 7, 8)
        assert source is not None
        captured = cell.capture()
        assert captured is not None
        token = await SourceFence(writer, image_scope, captured, 7, 8, source, 1).prepare()
        assert token.is_current()
        stored = publish_image(tmp_path / "images", 42, b"\xff\xd8\xfftest\xff\xd9")
        with write_transaction(conn):
            conn.execute(
                "INSERT INTO message_image_artifacts VALUES(42,7,8,99,10,?,?,?,?,?)",
                (stored.filename, stored.sha256, stored.byte_size, 10, 20),
            )
            conn.execute("UPDATE message_image_jobs SET state='complete'")
        result = await service.read(7, 8)
        assert result.image_bytes == b"\xff\xd8\xfftest\xff\xd9"
        assert result.mime_type == "image/jpeg"
        await _assert_corrupt_requires_explicit_prepare(
            service, conn, tmp_path / "images" / "42" / stored.filename, offered
        )
        with write_transaction(conn):
            conn.execute("UPDATE message_image_sources SET photo_id=100 WHERE dialog_id=7 AND message_id=8")
        assert not token.is_current()
        assert (await service.read(7, 8)).image_bytes is None
        cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 43, 2, 100))
        with pytest.raises(RuntimeError, match="verified account unavailable"):
            await service.read(7, 8)
    finally:
        await writer.run(image_scope.sources.unbind)
        await writer.aclose()
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "race",
    ["none", "delete", "replace", "key", "reference", "multi", "unsupported", "same_photo", "same_photo_resolve"],
)
async def test_worker_drains_explicit_legacy_photo_to_canonical_local_read(tmp_path: Path, race: str) -> None:
    db_path = tmp_path / "worker.db"
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(db_path)
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute(
            "INSERT INTO messages(dialog_id,message_id,sent_at,media_kind,media_payload) VALUES(7,8,1,'other','{}')"
        )
        conn.execute("INSERT INTO message_observations(dialog_id,message_id,observation_order) VALUES(7,8,1)")
        conn.execute("INSERT INTO daemon_state(key,value) VALUES('canonical_observation_sequence','1')")
        if race == "same_photo_resolve":
            conn.execute("INSERT INTO message_image_sources VALUES(7,8,99,1,1)")
    writer = await SQLiteWriter.open(db_path)
    image_scope, cell = await _bind_image_scope(writer, TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 100))
    image = b"\xff\xd8\xfftest\xff\xd9"
    photo = types.Photo(99, 555, b"private", datetime.now(UTC), [], 2)
    message = types.Message(
        8, types.PeerUser(7), date=datetime.now(UTC), message="", media=types.MessageMediaPhoto(photo=photo)
    )
    opens = [0]
    refreshed = [False]
    slices = [0]

    class Session:
        def chunks(self, *, offset: int) -> AsyncIterator[bytes]:
            async def stream() -> AsyncIterator[bytes]:
                slices[0] += 1
                if race == "reference" and opens[0] == 1:
                    raise FileReferenceExpiredError(request=None)
                if race == "key":
                    cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 101))
                if race == "same_photo" and not refreshed[0]:
                    refreshed[0] = True
                    await _observe_same_photo(writer, message, 3)
                if race in {"delete", "replace"}:

                    def change(conn: sqlite3.Connection) -> None:
                        with write_transaction(conn):
                            if race == "delete":
                                conn.execute("UPDATE messages SET is_deleted=1 WHERE dialog_id=7 AND message_id=8")
                            else:
                                conn.execute(
                                    "UPDATE message_image_sources SET photo_id=100 WHERE dialog_id=7 AND message_id=8"
                                )

                    await writer.run(change)
                yield image[offset : offset + 4] if race == "multi" else image[offset:]

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
            if race == "same_photo_resolve" and not refreshed[0]:
                refreshed[0] = True
                await _observe_same_photo(writer, message, 3)
            if race == "unsupported":
                raise PhotoUnsupportedError("unsupported_photo")
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
            opens[0] += 1
            return Session()

    service = MessageImageService(
        conn,
        writer,
        tmp_path / "images",
        image_scope,
        lambda: None,
    )
    worker = MessageImageWorker(conn, writer, tmp_path / "images", image_scope, Gateway())
    try:
        await service.prepare(7, 8)
        await run_ready_slice(worker, limit=32, now=100)
        if race == "reference":
            await run_ready_slice(worker, limit=32, now=100)
        if race == "same_photo_resolve":
            await run_ready_slice(worker, limit=32, now=100)
        if race == "same_photo":
            await _observe_same_photo(writer, message, 4)
            await run_ready_slice(worker, limit=32, now=100)
            await run_ready_slice(worker, limit=32, now=100)
        if race == "multi":
            await run_ready_slice(worker, limit=32, now=100)
            await run_ready_slice(worker, limit=32, now=100)
        await _assert_worker_result(service, conn, tmp_path, race, (opens, slices))
    finally:
        await worker.close()
        await writer.run(image_scope.sources.unbind)
        await writer.aclose()
        conn.close()


@pytest.mark.parametrize("photo_id", [100, -100])
def test_normal_message_ingestion_keeps_photo_id_private_and_orders_same_second_edits(
    tmp_path: Path, photo_id: int
) -> None:
    db_path = tmp_path / "ingest.db"
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(db_path)
    now = datetime.now(UTC)

    def observed(photo_id: int, order: int) -> ExtractedMessage:
        photo = types.Photo(photo_id, 555, b"private-ref", now, [], 2)
        message = types.Message(
            8, types.PeerUser(7), date=now, message="", edit_date=now, media=types.MessageMediaPhoto(photo=photo)
        )
        return replace(extract_message_row(7, message), observation_order=order)

    try:
        with write_transaction(conn):
            assert insert_messages_with_fts(conn, [observed(99, 2)]) == 1
            assert insert_messages_with_fts(conn, [observed(photo_id, 3)]) == 1
            assert insert_messages_with_fts(conn, [observed(101, 1)]) == 0
        source = current_source(conn, 7, 8)
        assert source is not None and (source.photo_id, source.generation) == (photo_id, 3)
        payload = cast(
            tuple[str] | None,
            conn.execute("SELECT media_payload FROM messages WHERE dialog_id=7 AND message_id=8").fetchone(),
        )
        assert payload == ("{}",)
        assert str(photo_id) not in repr(observed(photo_id, 3))
    finally:
        conn.close()


def test_startup_file_reconciliation_preserves_referenced_and_unrelated_files(tmp_path: Path) -> None:
    from mcp_telegram.message_images.store import cleanup_unpublished

    directory = tmp_path / "images" / "42"
    directory.mkdir(parents=True)
    live = directory / ("a" * 32 + ".jpg")
    orphan = directory / ("b" * 32 + ".jpg")
    partial = directory / ("c" * 32 + ".partial")
    unrelated = directory / "operator-notes.txt"
    for path in (live, orphan, partial, unrelated):
        path.write_bytes(b"x")
    cleanup_unpublished(tmp_path / "images", 42, frozenset({live.name}))
    assert live.exists() and unrelated.exists()
    assert not orphan.exists() and not partial.exists()


@pytest.mark.asyncio
async def test_local_read_rechecks_source_after_file_io(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import threading

    db_path = tmp_path / "read-race.db"
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(db_path)
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute(
            "INSERT INTO messages(dialog_id,message_id,sent_at,media_kind,media_payload) VALUES(7,8,1,'photo','{}')"
        )
        conn.execute("INSERT INTO message_observations(dialog_id,message_id,observation_order) VALUES(7,8,1)")
        conn.execute("INSERT INTO message_image_sources VALUES(7,8,99,1,1)")
    writer = await SQLiteWriter.open(db_path)
    scope = TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 100)
    image_scope, _cell = _image_scope(scope)
    service = MessageImageService(conn, writer, tmp_path / "images", image_scope, lambda: None)
    stored = publish_image(tmp_path / "images", 42, b"\xff\xd8\xfftest\xff\xd9")
    with write_transaction(conn):
        conn.execute(
            "INSERT INTO message_image_artifacts VALUES(42,7,8,99,1,?,?,?,?,?)",
            (stored.filename, stored.sha256, stored.byte_size, 10, 20),
        )
    started = threading.Event()
    proceed = threading.Event()
    original = service._read_file

    def delayed(account: int, filename: str, digest: str, size: int) -> bytes | None:
        started.set()
        if not proceed.wait(timeout=5):
            raise TimeoutError("test read did not resume")
        return original(account, filename, digest, size)

    monkeypatch.setattr(service, "_read_file", delayed)
    task = asyncio.create_task(service.read(7, 8))
    try:
        assert await asyncio.to_thread(started.wait, 5)

        def replace_photo(db: sqlite3.Connection) -> None:
            with write_transaction(db):
                db.execute("UPDATE message_image_sources SET photo_id=100 WHERE dialog_id=7 AND message_id=8")

        await writer.run(replace_photo)
        proceed.set()
        assert (await task).image_bytes is None
    finally:
        proceed.set()
        await task
        await writer.aclose()
        conn.close()


@pytest.mark.asyncio
async def test_budget_reservations_survive_owner_reconstruction(tmp_path: Path) -> None:
    db_path = tmp_path / "budget.db"
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(db_path)
    with write_transaction(conn):
        conn.execute(
            "INSERT INTO message_image_jobs(account_id,dialog_id,message_id,state,attempt_generation,source_generation,remaining,setup_remaining) VALUES(42,7,8,'queued',1,1,2,1)"
        )
    writer = await SQLiteWriter.open(db_path)
    try:
        await DurableBudget(writer, 42, 7, 8, 1).reserve(setup=True)
        await DurableBudget(writer, 42, 7, 8, 1).reserve_redirect(5)
        reconstructed = DurableBudget(writer, 42, 7, 8, 1)
        assert await reconstructed.current_redirect() == 5
        with pytest.raises(RuntimeError, match="redirect allowance exhausted"):
            await reconstructed.reserve_redirect(6)
        with pytest.raises(RuntimeError, match="allowance exhausted"):
            await reconstructed.reserve(setup=True)
        await reconstructed.reserve(setup=False)
        with pytest.raises(RuntimeError, match="allowance exhausted"):
            await reconstructed.reserve(setup=False)
        assert conn.execute("SELECT remaining,setup_remaining FROM message_image_jobs").fetchone() == (0, 0)
    finally:
        await writer.aclose()
        conn.close()


@pytest.mark.asyncio
async def test_expiring_photo_is_not_queued(tmp_path: Path) -> None:
    db_path = tmp_path / "ephemeral.db"
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(db_path)
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute(
            "INSERT INTO messages(dialog_id,message_id,sent_at,media_kind,media_payload) VALUES(7,8,1,'photo','{\"ttl_seconds\":30}')"
        )
    writer = await SQLiteWriter.open(db_path)
    scope = TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 100)
    offers: list[bool] = []
    service = MessageImageService(
        conn,
        writer,
        tmp_path / "images",
        _image_scope(scope)[0],
        lambda: offers.append(True),
    )
    try:
        status = await service.prepare(7, 8)
        assert status["content"] == {"state": "unavailable", "reason": "unsupported_media"}
        assert status["acquisition"] == {"state": "idle"}
        assert not offers
        assert conn.execute("SELECT COUNT(*) FROM message_image_jobs").fetchone() == (0,)
    finally:
        await writer.aclose()
        conn.close()


@pytest.mark.asyncio
async def test_shutdown_unbinds_source_registry_even_if_worker_close_fails(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from mcp_telegram.daemon import _stop_image_sources, _SyncMainContext

    db_path = tmp_path / "shutdown.db"
    ensure_sync_schema(db_path)
    writer = await SQLiteWriter.open(db_path)
    sources = ActiveSources()
    await writer.run(sources.bind)
    ctx = cast(
        _SyncMainContext,
        SimpleNamespace(
            writer=writer,
            image_sources=sources,
            image_sources_bound=True,
            image_worker=SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("close failed"))),
        ),
    )
    replacement = ActiveSources()
    try:
        with pytest.raises(RuntimeError, match="close failed"):
            await _stop_image_sources(ctx)
        await writer.run(replacement.bind)
        await writer.run(replacement.unbind)
    finally:
        await writer.aclose()


@pytest.mark.asyncio
async def test_source_claim_is_revoked_before_canonical_update_commit(tmp_path: Path) -> None:
    import asyncio
    import threading

    db_path = tmp_path / "claim-race.db"
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(db_path)
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute(
            "INSERT INTO messages(dialog_id,message_id,sent_at,media_kind,media_payload) VALUES(7,8,1,'photo','{}')"
        )
        conn.execute("INSERT INTO message_observations(dialog_id,message_id,observation_order) VALUES(7,8,1)")
        conn.execute("INSERT INTO message_image_sources VALUES(7,8,99,1,1)")
    writer = await SQLiteWriter.open(db_path)
    image_scope, cell = await _bind_image_scope(writer, TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 100))
    service = MessageImageService(conn, writer, tmp_path / "images", image_scope, lambda: None)
    staged = threading.Event()
    release = threading.Event()
    try:
        await service.prepare(7, 8)
        token = cell.capture()
        source = current_source(conn, 7, 8)
        assert token is not None and source is not None
        permit = await SourceFence(writer, image_scope, token, 7, 8, source, 1).prepare()
        assert permit.is_current()
        now = datetime.now(UTC)
        message = types.Message(
            8,
            types.PeerUser(7),
            date=now,
            message="",
            media=types.MessageMediaPhoto(photo=types.Photo(100, 555, b"private", now, [], 2)),
        )
        incoming = replace(extract_message_row(7, message), observation_order=2)

        def update(db: sqlite3.Connection) -> None:
            with write_transaction(db):
                insert_messages_with_fts(db, [incoming])
                staged.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("test writer did not resume")

        task = asyncio.create_task(writer.run(update))
        try:
            assert await asyncio.to_thread(staged.wait, 5)
            assert not permit.is_current()
        finally:
            release.set()
            await task
    finally:
        await writer.run(image_scope.sources.unbind)
        await writer.aclose()
        conn.close()
