"""Telegram media-fact hydration strategy."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Protocol, cast

from telethon.tl.functions.messages import GetRichMessageRequest  # type: ignore[import-untyped]
from telethon.tl.types import TypeInputPeer  # type: ignore[import-untyped]
from telethon.utils import get_peer_id  # type: ignore[import-untyped]

from .fact_hydration import AppliedFacts, HydrationDropObservation, MediaPublication, _has_terminal_rpc_symbol
from .hydration_queue import MEDIA_METADATA_KIND, RICH_COMPOSITION_KIND, HydrationJob, HydrationQueueRepository
from .messages.sqlite_hydration import enqueue_transcription_for_hydrated_media
from .messages.sqlite_hydration_jobs import (
    media_fact_hydration_eligible,
    rich_composition_hydration_eligible,
)
from .peer_access import peer_target
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

    async def get_input_entity(self, dialog_id: int) -> TypeInputPeer: ...

    async def __call__(self, request: object, **kwargs: object) -> object: ...


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
        with peer_target(jobs[0].dialog_id):
            return await telegram.get_messages(entity=jobs[0].dialog_id, ids=[job.message_id for job in jobs])

    def apply(  # noqa: PLR0913 - request boundary accompanies the existing fact application contract
        self,
        conn: sqlite3.Connection,
        queue: HydrationQueueRepository,
        jobs: Sequence[HydrationJob],
        result: object,
        *,
        now: int,
        observation_order: int | None = None,
    ) -> AppliedFacts:
        del observation_order
        if not isinstance(result, MediaPublication):
            raise TypeError("media hydration requires canonical publication result")
        by_id, valid_response = _response_map(result.response)
        hydrated = completed = 0
        dropped = 0
        drop_observations: list[HydrationDropObservation] = []
        if not valid_response:
            drop_observations.extend(_drop_unavailable_media(queue, job, result, "invalid_result") for job in jobs)
            return AppliedFacts(dropped=dropped + len(jobs), drop_observations=tuple(drop_observations))
        for job in jobs:
            message = by_id.get(job.message_id)
            if message is None:
                dropped += 1
                drop_observations.append(_drop_unavailable_media(queue, job, result, "missing_response"))
                continue
            if job.message_id not in result.accepted_message_ids:
                queue.requeue_not_applied(job, job.due_at)
                dropped += 1
                drop_observations.append(
                    HydrationDropObservation("not_applied", job.message_id, job.kind, job.dialog_id, job.attempts)
                )
                continue
            queue.remove(job)
            completed += 1
            if extract_media_fact(getattr(message, "media", None)) is not None:
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


class RichCompositionHydrationHandler(MediaFactHydrationHandler):
    """Recover a retained message and expand a Telegram partial rich message."""

    kind = RICH_COMPOSITION_KIND
    request_cost = 3

    def __init__(self) -> None:
        super().__init__(batch_size=1)

    def eligible(self, conn: sqlite3.Connection, job: HydrationJob) -> bool:
        return rich_composition_hydration_eligible(conn, job.dialog_id, job.message_id)

    async def request(self, client: object, jobs: Sequence[HydrationJob]) -> object:
        result = await super().request(client, jobs)
        by_id, valid = _response_map(result)
        message = by_id.get(jobs[0].message_id) if valid else None
        if message is not None:
            _require_rich_peer(message, jobs[0].dialog_id)
        rich = getattr(message, "rich_message", None)
        if not getattr(rich, "part", False):
            return result
        telegram = cast(MediaHydrationClient, client)
        with peer_target(jobs[0].dialog_id):
            peer = await telegram.get_input_entity(jobs[0].dialog_id)
            full = await telegram(GetRichMessageRequest(peer=peer, id=jobs[0].message_id))
        messages = getattr(full, "messages", None)
        by_id, valid = _response_map(messages)
        expanded = by_id.get(jobs[0].message_id) if valid else None
        expanded_rich = cast(object, getattr(expanded, "rich_message", None))
        if expanded_rich is None or getattr(expanded_rich, "part", False):
            raise ValueError("Telegram returned incomplete rich message")
        _require_rich_peer(expanded, jobs[0].dialog_id)
        return [expanded]


def _require_rich_peer(message: object, dialog_id: int) -> None:
    try:
        peer_id = get_peer_id(getattr(message, "peer_id", None))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Telegram returned a message without a valid peer") from exc
    if peer_id != dialog_id:
        raise ValueError("Telegram returned a message from a different peer")


def _drop_unavailable_media(
    queue: HydrationQueueRepository, job: HydrationJob, result: MediaPublication, reason: str
) -> HydrationDropObservation:
    if job.message_id in result.superseded_message_ids:
        queue.requeue_not_applied(job, job.due_at)
        reason = "not_applied"
    else:
        queue.mark_terminal(job)
    return HydrationDropObservation(reason, job.message_id, job.kind, job.dialog_id, job.attempts)


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
