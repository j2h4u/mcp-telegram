"""Private image job and canonical source metadata in the daemon database."""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast

from ..media_fact import decode_media_fact
from ..messages.sqlite_bundle import has_nonchannel_deletion
from .limits import PHOTO_MAX_ATTEMPTS, PHOTO_MAX_SETUP_ATTEMPTS


@dataclass(frozen=True, slots=True)
class ImageSource:
    photo_id: int | None = field(repr=False)
    generation: int
    kind: str | None
    supported: bool


@dataclass(frozen=True, slots=True)
class EnqueueOptions:
    force: bool
    revoke: Callable[[], None]
    claim: Callable[[], bool]


def current_source(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> ImageSource | None:
    if has_nonchannel_deletion(conn, dialog_id, message_id):
        return None
    row = cast(
        tuple[int | None, int, str | None, str | None, int | None] | None,
        conn.execute(
            "SELECT s.photo_id,COALESCE(o.observation_order,0),m.media_kind,m.media_payload,s.eligible FROM messages m "
            "LEFT JOIN message_observations o USING(dialog_id,message_id) "
            "LEFT JOIN message_image_sources s USING(dialog_id,message_id) "
            "WHERE m.dialog_id=? AND m.message_id=? AND m.is_deleted=0 AND COALESCE(o.is_deleted,0)=0",
            (dialog_id, message_id),
        ).fetchone(),
    )
    if row is None:
        return None
    photo_id, generation, kind, payload, eligible = row
    fact = decode_media_fact(kind, payload)
    supported = bool(
        eligible != 0
        and fact is not None
        and (
            (fact.kind == "other" and not fact.payload)
            or (fact.kind == "photo" and not fact.payload.get("ttl_seconds") and not fact.payload.get("live_photo"))
        )
    )
    return ImageSource(photo_id, generation, kind, supported)


def enqueue(
    conn: sqlite3.Connection,
    account_id: int,
    dialog_id: int,
    message_id: int,
    *,
    options: EnqueueOptions,
) -> bool:
    source = current_source(conn, dialog_id, message_id)
    if source is None or not source.supported:
        return False
    existing = cast(
        tuple[str, int, int, str | None, int | None] | None,
        conn.execute(
            "SELECT state,attempt_generation,source_generation,reason,photo_id FROM message_image_jobs "
            "WHERE account_id=? AND dialog_id=? AND message_id=?",
            (account_id, dialog_id, message_id),
        ).fetchone(),
    )
    if _already_pending(existing, source):
        return False
    if not options.force and (
        conn.execute(
            "SELECT 1 FROM message_image_artifacts WHERE account_id=? AND dialog_id=? AND message_id=? AND photo_id=?",
            (account_id, dialog_id, message_id, source.photo_id),
        ).fetchone()
        is not None
    ):
        return False
    attempt = int(existing[1]) + 1 if existing is not None else 1
    if not options.claim():
        return False
    options.revoke()
    conn.execute(
        "INSERT INTO message_image_jobs(account_id,dialog_id,message_id,state,attempt_generation,source_generation,"
        "photo_id,remaining,setup_remaining) VALUES(?,?,?,'queued',?,?,?,?,?) "
        "ON CONFLICT(account_id,dialog_id,message_id) DO UPDATE SET state='queued',"
        "attempt_generation=excluded.attempt_generation,source_generation=excluded.source_generation,"
        "photo_id=excluded.photo_id,remaining=excluded.remaining,setup_remaining=excluded.setup_remaining,offset=0,reference_refreshes=0,redirect_dc=NULL,reason=NULL",
        (
            account_id,
            dialog_id,
            message_id,
            attempt,
            source.generation,
            source.photo_id,
            PHOTO_MAX_ATTEMPTS,
            PHOTO_MAX_SETUP_ATTEMPTS,
        ),
    )
    return True


def _already_pending(existing: tuple[str, int, int, str | None, int | None] | None, source: ImageSource) -> bool:
    if existing is None:
        return False
    same_photo = source.photo_id is not None and existing[4] == source.photo_id
    if existing[0] in ("queued", "running", "blocked") and (existing[2] == source.generation or same_photo):
        return True
    return existing[3] == "unsupported_photo" and (existing[2] == source.generation or same_photo)
