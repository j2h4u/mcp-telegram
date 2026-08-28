"""Telegram voice-transcription hydration strategy."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Protocol, cast

from telethon.errors.rpcerrorlist import (  # type: ignore[import-untyped]
    MessageIdInvalidError,
    PremiumAccountRequiredError,
    PremiumCurrentlyUnavailableError,
    VoiceMessagesForbiddenError,
)
from telethon.tl.functions.messages import TranscribeAudioRequest  # type: ignore[import-untyped]

from .fact_hydration import AppliedFacts
from .hydration_queue import HydrationJob, HydrationQueueRepository
from .messages.sqlite_repository import apply_message_transcription, media_hydration_eligible

TRANSCRIPTION_HYDRATION_KIND = "transcription"


class TranscriptionHydrationClient(Protocol):
    async def get_input_entity(self, dialog_id: int) -> object: ...

    async def __call__(self, request: object) -> object: ...


class TranscriptionHydrationHandler:
    """Request Telegram's persisted or on-demand voice transcription."""

    kind = TRANSCRIPTION_HYDRATION_KIND
    flood_source = "transcription_hydration"
    batch_size = 1
    request_cost = 2

    def __init__(self, *, recheck_delay_seconds: int) -> None:
        self._recheck_delay_seconds = recheck_delay_seconds

    def eligible(self, conn: sqlite3.Connection, dialog_id: int) -> bool:
        return media_hydration_eligible(conn, dialog_id)

    async def request(self, client: object, jobs: Sequence[HydrationJob]) -> object:
        telegram = cast(TranscriptionHydrationClient, client)
        job = jobs[0]
        peer = await telegram.get_input_entity(job.dialog_id)
        return await telegram(TranscribeAudioRequest(peer=peer, msg_id=job.message_id))

    def apply(
        self,
        conn: sqlite3.Connection,
        queue: HydrationQueueRepository,
        jobs: Sequence[HydrationJob],
        result: object,
        *,
        now: int,
    ) -> AppliedFacts:
        job = jobs[0]
        if bool(getattr(result, "pending", False)):
            queue.reschedule(job, now + self._recheck_delay_seconds)
            return AppliedFacts(retried=1)
        text = getattr(result, "text", None)
        transcription_id = getattr(result, "transcription_id", None)
        if (
            not isinstance(text, str)
            or not text.strip()
            or not isinstance(transcription_id, int)
            or isinstance(transcription_id, bool)
        ):
            queue.remove(job)
            return AppliedFacts(dropped=1)
        applied = apply_message_transcription(
            conn,
            job.dialog_id,
            job.message_id,
            transcribed_text=text,
            transcription_id=transcription_id,
            received_at=now,
        )
        if not applied:
            queue.remove(job)
            return AppliedFacts(dropped=1)
        return AppliedFacts(hydrated=1, completed=1)

    def is_terminal_error(self, exc: BaseException) -> bool:
        if isinstance(
            exc,
            (
                MessageIdInvalidError,
                PremiumAccountRequiredError,
                PremiumCurrentlyUnavailableError,
                VoiceMessagesForbiddenError,
            ),
        ):
            return True
        # Telethon exposes these RPC errors only as generic RPCError in the
        # installed release; use its stable symbolic message, not a fallback
        # retry taxonomy.
        message = str(exc).upper()
        return any(marker in message for marker in ("TRANSCRIPTION_FAILED", "TRANSCRIPTION_TOO_LONG"))


__all__ = ["TRANSCRIPTION_HYDRATION_KIND", "TranscriptionHydrationClient", "TranscriptionHydrationHandler"]
