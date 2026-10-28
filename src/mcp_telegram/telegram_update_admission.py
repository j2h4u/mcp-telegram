"""Keep vendor gap bookkeeping alive while the gate withholds acquisition."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from datetime import datetime
from typing import cast

from telethon._updates import GapError, MessageBox, PrematureEndReason  # type: ignore[import-untyped]
from telethon._updates.messagebox import ENTRY_ACCOUNT, ENTRY_SECRET  # type: ignore[import-untyped]
from telethon.tl import types  # type: ignore[import-untyped]
from telethon.tl.functions.updates import GetChannelDifferenceRequest, GetDifferenceRequest  # type: ignore[import-untyped]

from .flood import TelegramRpcThrottled
from .telegram_rpc_scheduler import TelegramRpcAdmissionDeferred, TelegramRpcMethodRecoveryPending

type Eligibility = TelegramRpcThrottled | TelegramRpcMethodRecoveryPending | None


class GateMessageBox(MessageBox):
    """Only delay requests; vendor ordering, gaps and fatal errors stay unchanged."""

    seq: int
    date: datetime

    def __init__(
        self,
        previous: MessageBox,
        eligibility: Callable[[str | None], Eligibility],
        method_key: Callable[[object], str],
    ) -> None:
        super().__init__(previous._log)
        for name in MessageBox.__slots__:
            setattr(self, name, getattr(previous, name))
        self._eligibility = eligibility
        self._method_key = method_key
        self._blocked: dict[str, TelegramRpcThrottled | TelegramRpcMethodRecoveryPending] = {}
        self._preserved_ends: set[int | None] = set()
        self.wakeup = object()
        self.wakeup_queued = False

    def get_difference(self) -> GetDifferenceRequest | None:
        return self._eligible_request(super().get_difference())

    def get_channel_difference(self, chat_hashes: object) -> GetChannelDifferenceRequest | None:
        return self._eligible_request(super().get_channel_difference(chat_hashes))

    def _eligible_request[T](self, request: T | None) -> T | None:
        if request is None:
            return None
        method = self._method_key(request)
        hint = self._eligibility(method)
        local = self._blocked.get(method)
        if hint is None and isinstance(local, TelegramRpcAdmissionDeferred) and local.reason is None:
            hint = local if local.eligible_at is not None and local.eligible_at > time.monotonic() else None
        if hint is None:
            self._blocked.pop(method, None)
            return request
        self._blocked[method] = hint
        return None

    def preserve_deferred(self, request: object, hint: TelegramRpcThrottled | TelegramRpcMethodRecoveryPending) -> None:
        channel = getattr(request, "channel", None)
        self._preserved_ends.add(None if channel is None else cast(int, channel.channel_id))
        self._blocked[self._method_key(request)] = hint

    def end_difference(self) -> None:
        if None in self._preserved_ends:
            self._preserved_ends.remove(None)
            return
        super().end_difference()

    def end_channel_difference(
        self, request: GetChannelDifferenceRequest, reason: PrematureEndReason, chat_hashes: object
    ) -> None:
        channel_id = cast(types.InputChannel, request.channel).channel_id
        if channel_id in self._preserved_ends:
            self._preserved_ends.remove(channel_id)
            return
        super().end_channel_difference(request, reason, chat_hashes)

    def check_deadlines(self) -> float:
        deadline = cast(float, super().check_deadlines())
        if deadline > time.monotonic() or not self._blocked:
            return deadline
        boundaries = [
            hint.eligible_at
            for hint in self._blocked.values()
            if isinstance(hint, TelegramRpcAdmissionDeferred) and hint.eligible_at is not None
        ]
        return min(boundaries, default=math.inf)

    def process_updates(
        self, updates: object, chat_hashes: object, result: list[object]
    ) -> tuple[list[object], list[object]]:
        if updates is self.wakeup:
            self.wakeup_queued = False
            raise GapError
        preserve_account = bool(self.getting_diff_for & {ENTRY_ACCOUNT, ENTRY_SECRET})
        seq, date = self.seq, self.date
        try:
            return cast(tuple[list[object], list[object]], super().process_updates(updates, chat_hashes, result))
        finally:
            if preserve_account:
                self.seq, self.date = seq, date
