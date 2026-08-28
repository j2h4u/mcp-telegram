"""Telegram voice-transcription hydration strategy."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Protocol, cast

from telethon.errors.rpcerrorlist import (  # type: ignore[import-untyped]
    MessageIdInvalidError,
    PeerIdInvalidError,
    PremiumAccountRequiredError,
)
from telethon.tl.functions.messages import TranscribeAudioRequest  # type: ignore[import-untyped]
from telethon.tl.types import TypeInputPeer  # type: ignore[import-untyped]

from .fact_hydration import AppliedFacts
from .hydration_queue import TRANSCRIPTION_HYDRATION_KIND, HydrationJob, HydrationQueueRepository
from .messages.sqlite_repository import (
    apply_message_transcription_if_absent,
    transcription_hydration_eligible,
)


class TranscriptionHydrationClient(Protocol):
    async def get_input_entity(self, dialog_id: int) -> TypeInputPeer: ...

    async def __call__(self, request: object, **kwargs: object) -> object: ...


class TranscriptionHydrationHandler:
    """Request Telegram's persisted or on-demand voice transcription."""

    kind = TRANSCRIPTION_HYDRATION_KIND
    flood_source = "transcription_hydration"
    batch_size = 1
    request_cost = 2

    def __init__(self, *, recheck_delay_seconds: int) -> None:
        self.pending_delay_seconds = recheck_delay_seconds

    def eligible(self, conn: sqlite3.Connection, job: HydrationJob) -> bool:
        return transcription_hydration_eligible(conn, job.dialog_id, job.message_id)

    async def request(self, client: object, jobs: Sequence[HydrationJob]) -> object:
        telegram = cast(TranscriptionHydrationClient, client)
        job = jobs[0]
        peer = cast(TypeInputPeer, await telegram.get_input_entity(job.dialog_id))
        return await telegram(
            TranscribeAudioRequest(peer=peer, msg_id=job.message_id),
            flood_sleep_threshold=0,
        )

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
            return AppliedFacts(pending=True)
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
        applied = apply_message_transcription_if_absent(
            conn,
            job.dialog_id,
            job.message_id,
            transcribed_text=text,
            transcription_id=transcription_id,
            received_at=now,
        )
        if applied == "already_applied":
            queue.remove(job)
            return AppliedFacts(completed=1)
        if applied != "applied":
            queue.remove(job)
            return AppliedFacts(dropped=1)
        return AppliedFacts(hydrated=1, completed=1)

    def is_terminal_error(self, exc: BaseException) -> bool:
        if isinstance(
            exc,
            (
                MessageIdInvalidError,
                PremiumAccountRequiredError,
                PeerIdInvalidError,
            ),
        ):
            return True
        # Telethon exposes these RPC errors only as generic RPCError in the
        # installed release; use its stable symbolic message, not a fallback
        # retry taxonomy.
        message = getattr(exc, "message", None)
        return isinstance(message, str) and message in {
            "MSG_ID_INVALID",
            "MSG_VOICE_MISSING",
            "MSG_VOICE_TOO_LONG",
            "PEER_ID_INVALID",
            "PREMIUM_ACCOUNT_REQUIRED",
            "TRANSCRIPTION_FAILED",
        }


__all__ = ["TRANSCRIPTION_HYDRATION_KIND", "TranscriptionHydrationClient", "TranscriptionHydrationHandler"]
