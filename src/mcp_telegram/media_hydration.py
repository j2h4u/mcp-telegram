"""Telegram media-fact hydration strategy."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Protocol, cast

from .fact_hydration import AppliedFacts, HydrationDropObservation, _has_terminal_rpc_symbol
from .hydration_queue import MEDIA_METADATA_KIND, HydrationJob, HydrationQueueRepository
from .media_fact import encode_media_payload
from .messages.sqlite_repository import (
    apply_hydrated_media_fact,
    enqueue_transcription_for_hydrated_media,
    media_fact_hydration_eligible,
)
from .telethon_media import extract_media_fact

_TERMINAL_RPC_SYMBOLS = frozenset(
    {
        "MESSAGE_ID_INVALID",
        "MSG_ID_INVALID",
        "PEER_ID_INVALID",
        "CHANNEL_PRIVATE",
        "CHAT_ID_INVALID",
    }
)


class MediaHydrationClient(Protocol):
    async def get_messages(self, *_args: object, **_kwargs: object) -> object: ...


class MediaFactHydrationHandler:
    """Resolve unresolved media metadata through get_messages."""

    kind = MEDIA_METADATA_KIND
    request_cost = 1
    pending_delay_seconds = 0

    def __init__(self, *, batch_size: int) -> None:
        self.batch_size = batch_size

    def eligible(self, conn: sqlite3.Connection, job: HydrationJob) -> bool:
        return media_fact_hydration_eligible(conn, job.dialog_id, job.message_id)

    async def request(self, client: object, jobs: Sequence[HydrationJob]) -> object:
        telegram = cast(MediaHydrationClient, client)
        return await telegram.get_messages(entity=jobs[0].dialog_id, ids=[job.message_id for job in jobs])

    def apply(
        self,
        conn: sqlite3.Connection,
        queue: HydrationQueueRepository,
        jobs: Sequence[HydrationJob],
        result: object,
        *,
        now: int,
    ) -> AppliedFacts:
        by_id, valid_response = _response_map(result)
        hydrated = completed = dropped = 0
        drop_observations: list[HydrationDropObservation] = []
        if not valid_response:
            drop_observations.extend(
                HydrationDropObservation("invalid_result", job.message_id, job.kind, job.dialog_id, job.attempts)
                for job in jobs
            )
            for job in jobs:
                queue.mark_terminal(job)
            return AppliedFacts(dropped=len(jobs), drop_observations=tuple(drop_observations))
        for job in jobs:
            message = by_id.get(job.message_id)
            if message is None:
                queue.mark_terminal(job)
                dropped += 1
                drop_observations.append(
                    HydrationDropObservation("missing_response", job.message_id, job.kind, job.dialog_id, job.attempts)
                )
                continue
            fact = extract_media_fact(getattr(message, "media", None))
            kind = None if fact is None else fact.kind
            payload = encode_media_payload(fact)
            applied = apply_hydrated_media_fact(conn, job.dialog_id, job.message_id, kind, payload)
            queue.remove(job)
            if not applied:
                dropped += 1
                drop_observations.append(
                    HydrationDropObservation("not_applied", job.message_id, job.kind, job.dialog_id, job.attempts)
                )
            else:
                completed += 1
            if fact is not None and applied:
                hydrated += 1
                enqueue_transcription_for_hydrated_media(conn, job.dialog_id, job.message_id, due_at=now)
        return AppliedFacts(
            hydrated=hydrated,
            completed=completed,
            dropped=dropped,
            drop_observations=tuple(drop_observations),
        )

    def is_terminal_error(self, exc: BaseException) -> bool:
        return _has_terminal_rpc_symbol(exc, _TERMINAL_RPC_SYMBOLS)


def _response_map(result: object) -> tuple[dict[int, object], bool]:
    if result is None or isinstance(result, (str, bytes, dict)):
        return {}, False
    if getattr(result, "id", None) is not None:
        items: Sequence[object] = [result]
    else:
        try:
            items = list(cast(Sequence[object], result))
        except TypeError:
            return {}, False
    mapped: dict[int, object] = {}
    for item in items:
        raw_id = getattr(item, "id", None)
        if isinstance(raw_id, int) and raw_id > 0:
            mapped[raw_id] = item
    return mapped, True


__all__ = ["MEDIA_METADATA_KIND", "MediaFactHydrationHandler", "MediaHydrationClient"]
