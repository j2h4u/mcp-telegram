"""Explicit image preparation and local, account-fenced image delivery."""

import asyncio
import hashlib
import os
import sqlite3
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import cast

from ..entity_profile.repository import bound_profile_account
from ..image_authorization import ScopeToken
from ..sqlite_writer import SQLiteWriter
from ..sync_transactions import write_transaction
from .contracts import ImageReadResult, ImageScope
from .limits import PHOTO_MAX_BYTES
from .repository import EnqueueOptions, ImageSource, current_source, enqueue

_ARTIFACT_FILENAME_LENGTH = 36


class MessageImageService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        writer: SQLiteWriter,
        directory: Path,
        scope: ImageScope,
        offer: Callable[[], None],
    ) -> None:
        self._conn = conn
        self._writer = writer
        self._directory = directory
        self._scope = scope
        self._offer = offer

    def _account(self, conn: sqlite3.Connection, captured: ScopeToken | None) -> int | None:
        if (
            captured is None
            or bound_profile_account(conn) != captured.scope.account_id
            or not self._scope.is_current(captured)
        ):
            return None
        return captured.scope.account_id

    async def prepare(self, dialog_id: int, message_id: int) -> dict[str, object]:
        captured = self._scope.capture()
        if self._account(self._conn, captured) is None:
            raise RuntimeError("verified account unavailable")
        prior = await self.read(dialog_id, message_id)
        content = prior.status["content"]
        force = isinstance(content, dict) and content.get("reason") == "missing_or_corrupt"

        def queue(conn: sqlite3.Connection) -> bool:
            with write_transaction(conn):
                if captured is None or bound_profile_account(conn) != captured.scope.account_id:
                    return False
                account = captured.scope.account_id
                return enqueue(
                    conn,
                    account,
                    dialog_id,
                    message_id,
                    options=EnqueueOptions(
                        force=force,
                        revoke=lambda: self._scope.sources.revoke(account, dialog_id, message_id),
                        claim=partial(self._scope.is_current, captured),
                    ),
                )

        if await self._writer.run(queue):
            self._offer()
        return (await self.read(dialog_id, message_id)).status

    async def read(self, dialog_id: int, message_id: int) -> ImageReadResult:
        captured = self._scope.capture()
        status: dict[str, object] = {
            "dialog_id": dialog_id,
            "message_id": message_id,
            "content": {"state": "missing"},
            "acquisition": {"state": "idle"},
        }
        account = self._account(self._conn, captured)
        if account is None:
            raise RuntimeError("verified account unavailable")
        source = current_source(self._conn, dialog_id, message_id)
        if source is None:
            status["content"] = {"state": "missing", "reason": _missing_reason(self._conn, dialog_id, message_id)}
            return ImageReadResult(status)
        if not source.supported:
            status["content"] = {"state": "unavailable", "reason": "unsupported_media"}
            return ImageReadResult(status)
        job = cast(
            tuple[str, str | None, int, int | None] | None,
            self._conn.execute(
                "SELECT state,reason,source_generation,photo_id FROM message_image_jobs "
                "WHERE account_id=? AND dialog_id=? AND message_id=?",
                (account, dialog_id, message_id),
            ).fetchone(),
        )
        if job is not None and _job_matches_source(job, source):
            status["acquisition"] = _acquisition_status((job[0], job[1]))
        artifact = cast(
            tuple[str, str, int, int | None, int | None] | None,
            self._conn.execute(
                "SELECT filename,sha256,byte_size,width,height FROM message_image_artifacts "
                "WHERE account_id=? AND dialog_id=? AND message_id=? AND photo_id=?",
                (account, dialog_id, message_id, source.photo_id),
            ).fetchone(),
        )
        if artifact is None:
            return ImageReadResult(status)
        filename, digest, size, width, height = artifact
        image = await asyncio.to_thread(self._read_file, account, filename, digest, size)
        if self._account(self._conn, captured) != account:
            raise RuntimeError("verified account unavailable")
        if current_source(self._conn, dialog_id, message_id) != source:
            return ImageReadResult({**status, "content": {"state": "missing"}})
        if image is None:
            status["content"] = {"state": "unavailable", "reason": "missing_or_corrupt"}
            return ImageReadResult(status)
        status["content"] = _available_content(size, width, height)
        return ImageReadResult(status, image, "image/jpeg")

    def _read_file(self, account: int, filename: str, digest: str, size: int) -> bytes | None:
        if not _valid_artifact_name(filename) or not 0 < size <= PHOTO_MAX_BYTES:
            return None
        try:
            path = self._directory / str(account) / filename
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as file:
                data = file.read(PHOTO_MAX_BYTES + 1)
        except OSError:
            return None
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            return None
        if not (data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")):
            return None
        return data


def _valid_artifact_name(filename: str) -> bool:
    return (
        filename.isascii()
        and len(filename) == _ARTIFACT_FILENAME_LENGTH
        and filename.endswith(".jpg")
        and all(c in "0123456789abcdef" for c in filename[:-4])
    )


def _missing_reason(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> str:
    row = cast(
        tuple[int] | None,
        conn.execute(
            "SELECT is_deleted FROM messages WHERE dialog_id=? AND message_id=?", (dialog_id, message_id)
        ).fetchone(),
    )
    return "message_deleted" if row is not None and row[0] else "message_not_found"


def _acquisition_status(job: tuple[str, str | None]) -> dict[str, object]:
    status: dict[str, object] = {"state": job[0]}
    if job[1] is not None:
        status["reason"] = job[1]
    return status


def _job_matches_source(job: tuple[str, str | None, int, int | None], source: ImageSource) -> bool:
    return job[2] == source.generation or (source.photo_id is not None and job[3] == source.photo_id)


def _available_content(size: int, width: int | None, height: int | None) -> dict[str, object]:
    content: dict[str, object] = {
        "state": "available",
        "mime_type": "image/jpeg",
        "byte_size": size,
        "representation": "telegram_photo",
    }
    if width is not None:
        content["width"] = width
    if height is not None:
        content["height"] = height
    return content
