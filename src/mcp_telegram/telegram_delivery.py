"""Versioned local TL envelope; no RPC or canonical read projection."""

import base64
from datetime import datetime
from typing import cast

from pydantic import BaseModel, ConfigDict
from telethon import types  # type: ignore[import-untyped]
from telethon.extensions import BinaryReader  # type: ignore[import-untyped]


class ProtocolSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    pts: int
    qts: int
    date: datetime
    seq: int
    channels: dict[int, int]
    channel_hashes: dict[int, int]


class DeliveryEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    version: int = 1
    captured_at: float
    checkpoint_after: ProtocolSnapshot
    tl: str

    @classmethod
    def capture(
        cls,
        snapshot: ProtocolSnapshot,
        captured_at: float,
        updates: list[object],
        users: list[object],
        chats: list[object],
    ) -> DeliveryEnvelope:
        raw = bytes(
            types.Updates(
                cast(list[types.TypeUpdate], updates),
                cast(list[types.TypeUser], users),
                cast(list[types.TypeChat], chats),
                snapshot.date,
                snapshot.seq,
            )
        )
        return cls(captured_at=captured_at, checkpoint_after=snapshot, tl=base64.b64encode(raw).decode("ascii"))

    def unpack(self) -> tuple[list[object], list[object], list[object]]:
        try:
            if self.version != 1:
                raise ValueError("unsupported format")
            raw = base64.b64decode(self.tl, validate=True)
            with BinaryReader(raw) as reader:
                container = reader.tgread_object()
                if not isinstance(container, types.Updates) or reader.tell_position() != len(raw):
                    raise ValueError("invalid TL envelope")
            return (
                cast(list[object], container.updates),
                cast(list[object], container.users),
                cast(list[object], container.chats),
            )
        except Exception:  # noqa: BLE001 - sanitize vendor/validation errors that can embed private payloads
            raise RuntimeError("invalid retained Telegram update envelope") from None


def load_delivery_envelope(payload: bytes) -> DeliveryEnvelope:
    try:
        envelope = DeliveryEnvelope.model_validate_json(payload)
        envelope.unpack()
        return envelope
    except Exception:  # noqa: BLE001 - sanitize vendor/validation errors that can embed private payloads
        raise RuntimeError("invalid retained Telegram update envelope") from None


def load_protocol_snapshot(payload: str) -> ProtocolSnapshot:
    try:
        return ProtocolSnapshot.model_validate_json(payload)
    except Exception:  # noqa: BLE001 - sanitize vendor/validation errors that can embed private payloads
        raise RuntimeError("invalid retained Telegram protocol checkpoint") from None
