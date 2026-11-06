"""Keep vendor gap bookkeeping alive while the gate withholds acquisition."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from datetime import datetime
from typing import cast

from telethon import utils  # type: ignore[import-untyped]
from telethon._updates import GapError, MessageBox, PrematureEndReason  # type: ignore[import-untyped]
from telethon._updates.messagebox import ENTRY_ACCOUNT, ENTRY_SECRET, PtsInfo  # type: ignore[import-untyped]
from telethon.tl import types  # type: ignore[import-untyped]
from telethon.tl.functions.updates import GetChannelDifferenceRequest, GetDifferenceRequest  # type: ignore[import-untyped]

from .flood import TelegramRpcThrottled
from .image_authorization import ScopeToken
from .message_delivery_observations import DeliveryTrace, MessageDeliveryObservations
from .telegram_rpc_scheduler import TelegramRpcAdmissionDeferred, TelegramRpcMethodRecoveryPending
from .telegram_update_ring import TelegramUpdateRing

type Eligibility = TelegramRpcThrottled | TelegramRpcMethodRecoveryPending | None


class TelegramUpdateEligibilityDeferredError(ValueError):
    """A preserved update gap is waiting for local RPC eligibility."""


def _user_invalidations(envelope: object) -> list[types.UpdateUser]:
    updates = getattr(envelope, "updates", None)
    if not isinstance(updates, list):
        updates = [getattr(envelope, "update", envelope)]
    return [update for update in updates if type(update) is types.UpdateUser]


def _synthetic_notification(item: object) -> bool:
    if isinstance(item, types.UpdatesTooLong):
        return True
    if not isinstance(item, types.UpdateShort) or not getattr(item, "_self_outgoing", False):
        return False
    update = item.update
    return (
        isinstance(update, types.UpdateDeleteMessages)
        and not update.messages
        and item.date is not None
        and item.date.timestamp() == 0
    )


def _has_channel_update(envelope: object) -> bool:
    items = getattr(envelope, "updates", None)
    if not isinstance(items, list):
        items = [getattr(envelope, "update", envelope)]
    for update in items:
        message = getattr(update, "message", None)
        peer = getattr(message, "peer_id", None)
        if isinstance(peer, types.PeerChannel) or getattr(update, "channel_id", None) is not None:
            return True
    return False


class UpdateIntakeQueue(asyncio.Queue[object]):
    """Count real vendor envelopes without counting local eligibility wakeups."""

    def __init__(self, wakeup: object, notify: Callable[[], None]) -> None:
        super().__init__()
        self._wakeup = wakeup
        self._notify = notify
        self.real_count = 0
        self.observer: MessageDeliveryObservations | None = None
        self.update_ring: TelegramUpdateRing | None = None
        self.capture_scope: Callable[[], ScopeToken | None] | None = None
        self.on_real_dequeue: Callable[[int], None] | None = None

    def put_nowait(self, item: object) -> None:
        ring_sequence = self._capture_ring(item)
        if self.capture_scope is not None:
            for update in _user_invalidations(item):
                if not hasattr(update, "_mcp_receipt_scope_token"):
                    vars(update)["_mcp_receipt_scope_token"] = self.capture_scope()
        super().put_nowait(item)
        if item is not self._wakeup:
            self.real_count += 1
            self._notify()
            if self.observer is not None:
                try:
                    self._observe_intake(item)
                    trace = getattr(item, "_mcp_delivery_trace", None)
                    if isinstance(trace, DeliveryTrace) and ring_sequence is not None and self.update_ring is not None:
                        self.update_ring.associate_trace(ring_sequence, trace.trace_id)
                except Exception:  # noqa: BLE001 - diagnostic failure cannot reject a queued update
                    pass

    def _capture_ring(self, item: object) -> int | None:
        try:
            if item is not self._wakeup and self.update_ring is not None:
                previous = getattr(item, "_mcp_ring_sequence", None)
                if type(previous) is int:
                    return previous
                source = getattr(item, "_mcp_delivery_source", "unknown")
                if source == "unknown" and _synthetic_notification(item):
                    source = "library_synthetic"
                return self.update_ring.capture(
                    item, source=source if source in {"push", "rpc_response", "library_synthetic"} else "unknown"
                )
        except Exception:  # noqa: BLE001 - diagnostic work never blocks intake
            pass
        return None

    def capture_rpc_notification(self, item: object) -> None:
        """Capture a decoded RPC notification before vendor filtering."""
        if self.update_ring is None:
            return
        try:
            if isinstance(item, (types.messages.InvitedUsers, types.messages.ChatInviteJoinResultOk)):
                item = item.updates
            if not isinstance(
                item,
                (
                    types.Updates,
                    types.UpdatesCombined,
                    types.UpdateShort,
                    types.UpdateShortMessage,
                    types.UpdateShortChatMessage,
                    types.UpdateShortSentMessage,
                    types.UpdatesTooLong,
                ),
            ):
                return
            vars(item)["_mcp_delivery_source"] = "rpc_response"
            vars(item)["_mcp_ring_sequence"] = self.update_ring.capture(item, source="rpc_response")
        except Exception:  # noqa: BLE001 - diagnostic work never blocks RPC result delivery
            pass

    def _observe_intake(self, item: object) -> None:
        observer = self.observer
        assert observer is not None
        scope = "channel" if _has_channel_update(item) else "account"
        at = int(time.time() * 1000)
        trace = observer.intake(source=getattr(item, "_mcp_delivery_source", "unknown"), scope=scope, observed_at_ms=at)
        vars(item)["_mcp_delivery_trace"] = trace
        vars(item)["_mcp_delivery_scope"] = scope
        items = getattr(item, "updates", None)
        if not isinstance(items, list):
            items = [getattr(item, "update", item)]
        for update in items:
            if update is not None:
                vars(update)["_mcp_delivery_trace"] = trace
                vars(update)["_mcp_delivery_scope"] = scope
                self._observe_identity(observer, trace, update)
        observer.stage(trace, scope=scope, stage="queued", observed_at_ms=at)

    @staticmethod
    def _observe_identity(observer: MessageDeliveryObservations, trace: DeliveryTrace | None, update: object) -> None:
        if isinstance(update, (types.UpdateShortMessage, types.UpdateShortChatMessage)):
            dialog_id = update.user_id if isinstance(update, types.UpdateShortMessage) else -update.chat_id
            sent = update.date
            sent_at_ms = int(sent.timestamp() * 1000) if sent is not None and sent.tzinfo else None
            observer.identity(trace, dialog_id, update.id, sent_at_ms)
            return
        message = getattr(update, "message", None)
        peer, message_id = cast(object, getattr(message, "peer_id", None)), getattr(message, "id", None)
        if trace is None or not isinstance(peer, (types.PeerUser, types.PeerChat, types.PeerChannel)):
            return
        if not isinstance(message_id, int):
            return
        sent = cast(object, getattr(message, "date", None))
        sent_at_ms = int(sent.timestamp() * 1000) if isinstance(sent, datetime) and sent.tzinfo else None
        observer.identity(trace, int(utils.get_peer_id(peer)), message_id, sent_at_ms)

    def get_nowait(self) -> object:
        item = super().get_nowait()
        if item is not self._wakeup:
            if self.on_real_dequeue is not None:
                self.on_real_dequeue(self.real_count)
            self.real_count -= 1
        return item


class GateMessageBox(MessageBox):
    """Only delay requests; vendor ordering, gaps and fatal errors stay unchanged."""

    seq: int
    date: datetime

    def __init__(
        self,
        previous: MessageBox,
        eligibility: Callable[[str | None], Eligibility],
        method_key: Callable[[object], str],
        contact_scope_valid: Callable[[object], bool] | None = None,
    ) -> None:
        super().__init__(previous._log)
        for name in MessageBox.__slots__:
            setattr(self, name, getattr(previous, name))
        self._eligibility = eligibility
        self._method_key = method_key
        self._contact_scope_valid = contact_scope_valid
        self._blocked: dict[str, TelegramRpcThrottled | TelegramRpcMethodRecoveryPending] = {}
        self._preserved_ends: set[int | None] = set()
        self._access_loss_ends: dict[int, Callable[[], bool]] = {}
        self.wakeup = object()
        self.wakeup_queued = False
        self.intake_queue: UpdateIntakeQueue | None = None
        self.difference_credit = False
        self._intake_remaining: int | None = None
        self.observed_gaps: set[object] = set()
        self._promoted_gap_expirations: dict[object, float] = {}
        self.observer: MessageDeliveryObservations | None = None
        self._pts_invocations = 0
        self._pts_accepted = 0

    def load(self, session_state: object, channel_states: object) -> None:
        super().load(session_state, channel_states)
        self.observed_gaps.clear()
        self._promoted_gap_expirations.clear()

    def try_begin_get_diff(self, entry: object, reason: str) -> None:
        gap = self.possible_gaps.get(entry)
        had_gap = gap is not None
        deadline = gap.deadline if gap is not None else None
        super().try_begin_get_diff(entry, reason)
        if entry in self.getting_diff_for and (
            had_gap
            or reason
            in {
                "detected gap",
                "received updatesTooLong",
                "received updateChannelTooLong",
            }
        ):
            self.observed_gaps.add(entry)
            if deadline is not None:
                self._promoted_gap_expirations[entry] = deadline

    def apply_pts_info(self, update: object, *, reset_deadlines: object) -> object:
        try:
            pts = PtsInfo.from_update(update)
            fetching = pts is not None and pts.entry in self.getting_diff_for
        except Exception:  # noqa: BLE001 - diagnostic classification is best effort
            pts = None
            fetching = False
        result = super().apply_pts_info(update, reset_deadlines=reset_deadlines)
        self._pts_invocations += 1
        try:
            stage = self._classify_pts(update, result, pts, fetching)
            if stage == "pts_accepted":
                self._pts_accepted += 1
            self._observe_box(update, [], stage)
        except Exception:  # noqa: BLE001 - vendor result remains authoritative
            pass
        return result

    def _classify_pts(self, update: object, result: object, pts: object, fetching: bool) -> str:
        if isinstance(update, types.UpdateChannelTooLong):
            return "channel_too_long"
        if result is not None:
            return "pts_accepted"
        if fetching:
            return "getting_difference"
        if not isinstance(pts, PtsInfo):
            return "suppressed"
        gap = self.possible_gaps.get(pts.entry)
        return "possible_gap" if gap is not None and any(item is update for item in gap.updates) else "pts_duplicate"

    def end_get_diff(self, entry: object) -> None:
        super().end_get_diff(entry)
        self.observed_gaps.discard(entry)
        self._promoted_gap_expirations.pop(entry, None)

    def stream_snapshot(self, entry: object, *, now: float, in_flight: bool) -> dict[str, object]:
        state = self.map.get(entry)
        if state is None:
            return {"gap_state": "unknown", "catch_up_state": "unknown", "reason": "owner_unavailable"}
        gap = self.possible_gaps.get(entry)
        observed_gap = gap is not None or entry in self.observed_gaps
        result: dict[str, object] = {"gap_state": "observed" if observed_gap else "none_observed"}
        if in_flight:
            result.update(catch_up_state="in_flight", reason="physical_claim")
            return result
        gap_deadline = gap.deadline if gap is not None else self._promoted_gap_expirations.get(entry)
        deadline = min(state.deadline, gap_deadline) if gap_deadline is not None else state.deadline
        blocked = self._snapshot_eligibility(entry, now=now)
        if blocked is not None:
            result.update(blocked)
            self._add_overdue(result, now=now, deadline=deadline)
            return result
        due = entry in self.getting_diff_for or deadline <= now
        if not due:
            result.update(catch_up_state="not_due", reason="not_due")
            return result
        self._describe_due(result, now=now, deadline=deadline, possible_gap=observed_gap)
        return result

    def _describe_due(self, result: dict[str, object], *, now: float, deadline: float, possible_gap: bool) -> None:
        self._add_overdue(result, now=now, deadline=deadline)
        if self.intake_due():
            result.update(catch_up_state="blocked", reason="intake_pending")
        else:
            reason = "possible_gap" if possible_gap else "deadline" if deadline <= now else "unknown"
            result.update(catch_up_state="due", reason=reason)

    @staticmethod
    def _add_overdue(result: dict[str, object], *, now: float, deadline: float) -> None:
        if deadline <= now:
            result["overdue_seconds"] = now - deadline

    def _snapshot_eligibility(self, entry: object, *, now: float) -> dict[str, object] | None:
        method_type = GetChannelDifferenceRequest if isinstance(entry, int) else GetDifferenceRequest
        key = f"{method_type.__module__}.{method_type.__name__}:{method_type.CONSTRUCTOR_ID:08x}"
        try:
            hint = self._eligibility(key)
        except Exception:  # noqa: BLE001 - unavailable diagnostics cannot claim eligibility
            return {"catch_up_state": "unknown", "reason": "unknown"}
        if hint is None:
            return None
        if isinstance(hint, TelegramRpcAdmissionDeferred) and hint.eligible_at is not None and hint.eligible_at <= now:
            return None
        result: dict[str, object] = {"catch_up_state": "blocked", "reason": "eligibility"}
        reason = getattr(hint, "reason", None)
        result["eligibility_reason"] = (
            reason if reason in {"server", "method", "recovery", "pending_receipt"} else "unknown"
        )
        if isinstance(hint, TelegramRpcAdmissionDeferred) and hint.eligible_at is not None:
            result["eligible_in_seconds"] = max(0.0, hint.eligible_at - now)
        return result

    def delivery_snapshot(
        self, channel_id: int | None, *, now: float, account_in_flight: bool, channel_in_flight: bool = False
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "account": self.stream_snapshot(ENTRY_ACCOUNT, now=now, in_flight=account_in_flight),
            "intake_queue_depth": self.intake_queue.real_count if self.intake_queue is not None else 0,
            "pending_channels": len(self.getting_diff_for)
            - int(ENTRY_ACCOUNT in self.getting_diff_for)
            - int(ENTRY_SECRET in self.getting_diff_for),
        }
        if channel_id is not None:
            result["channel"] = self.stream_snapshot(channel_id, now=now, in_flight=channel_in_flight)
        return result

    def observe_recovery_updates(self, updates: list[object], scope: str, source: str) -> None:
        if self.observer is None or not updates:
            return
        try:
            trace = self.observer.intake(source=source, scope=scope, observed_at_ms=int(time.time() * 1000))
            for update in updates:
                vars(update).setdefault("_mcp_delivery_trace", trace)
                vars(update).setdefault("_mcp_delivery_scope", scope)
                UpdateIntakeQueue._observe_identity(self.observer, trace, update)
        except Exception:  # noqa: BLE001 - diagnostics cannot affect recovery
            pass

    def observe_replay_updates(self, updates: list[object]) -> None:
        try:
            self.observe_recovery_updates(
                [item for item in updates if not _has_channel_update(item)], "account", "replay"
            )
            self.observe_recovery_updates([item for item in updates if _has_channel_update(item)], "channel", "replay")
        except Exception:  # noqa: BLE001 - replay must not depend on diagnostics
            pass

    def apply_difference(
        self, diff: object, chat_hashes: object
    ) -> tuple[list[object], list[object], list[object]] | None:
        sequence = self._capture_difference(diff, "account_difference")
        result = cast(
            tuple[list[object], list[object], list[object]] | None, super().apply_difference(diff, chat_hashes)
        )
        if result is not None:
            self.observe_recovery_updates(result[0], "account", "recovery")
            self._associate_update_traces(sequence, result[0])
        return result

    def apply_channel_difference(
        self, request: object, diff: object, chat_hashes: object
    ) -> tuple[list[object], list[object], list[object]] | None:
        sequence = self._capture_difference(diff, "channel_difference")
        result = cast(
            tuple[list[object], list[object], list[object]] | None,
            super().apply_channel_difference(request, diff, chat_hashes),
        )
        if result is not None:
            self.observe_recovery_updates(result[0], "channel", "recovery")
            self._associate_update_traces(sequence, result[0])
        return result

    def _capture_difference(self, diff: object, source: str) -> int | None:
        try:
            if self.intake_queue is not None and self.intake_queue.update_ring is not None:
                return self.intake_queue.update_ring.capture(diff, source=source)
        except Exception:  # noqa: BLE001 - diagnostic work never blocks recovery
            pass
        return None

    def _associate_update_traces(self, sequence: int | None, updates: list[object]) -> None:
        try:
            if sequence is None or self.intake_queue is None or self.intake_queue.update_ring is None:
                return
            for update in updates:
                trace = getattr(update, "_mcp_delivery_trace", None)
                if isinstance(trace, DeliveryTrace):
                    self.intake_queue.update_ring.associate_trace(sequence, trace.trace_id)
        except Exception:  # noqa: BLE001 - diagnostic work never blocks recovery
            pass

    def intake_due(self) -> bool:
        return self.intake_queue is not None and self.intake_queue.real_count > 0 and not self.difference_credit

    def difference_claimed(self) -> None:
        self.difference_credit = False
        self._intake_remaining = None

    def real_dequeued(self, queued_count: int) -> None:
        if self.difference_credit:
            return
        if self._intake_remaining is None:
            self._intake_remaining = queued_count
        self._intake_remaining -= 1
        if self._intake_remaining == 0:
            self.difference_credit = True

    def get_difference(self) -> GetDifferenceRequest | None:
        if self.intake_due():
            return None
        if ENTRY_ACCOUNT not in self.map:
            return None
        now = asyncio.get_running_loop().time()
        for entry in (ENTRY_ACCOUNT, ENTRY_SECRET):
            state = self.map.get(entry)
            gap = self.possible_gaps.get(entry)
            if (
                state is not None
                and entry not in self.getting_diff_for
                and (state.deadline <= now or (gap is not None and gap.deadline <= now))
            ):
                self.try_begin_get_diff(entry, "account deadline reached")
        return self._eligible_request(super().get_difference())

    def get_channel_difference(self, chat_hashes: object) -> GetChannelDifferenceRequest | None:
        if self.intake_due():
            return None
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
            self._observe_catchup(request, method, "catchup_selected")
            return request
        self._blocked[method] = hint
        self._observe_catchup(request, method, "catchup_blocked")
        return None

    def _observe_catchup(self, request: object, method: str, stage: str) -> None:
        if self.observer is None:
            return
        try:
            scope = "channel" if "GetChannelDifferenceRequest" in method else "account"
            now = time.monotonic()
            observed_at_ms = int(time.time() * 1000)
            self.observer.stage(None, scope=scope, stage=stage, observed_at_ms=observed_at_ms)
            channel = cast(object, getattr(request, "channel", None))
            entry = cast(object, getattr(channel, "channel_id", None)) if scope == "channel" else ENTRY_ACCOUNT
            snapshot = self.stream_snapshot(entry, now=now, in_flight=False)
            snapshot.update(
                decision=stage,
                observed_at_ms=observed_at_ms,
                intake_queue_depth=self.intake_queue.real_count if self.intake_queue is not None else 0,
                pending_channels=len(self.getting_diff_for)
                - int(ENTRY_ACCOUNT in self.getting_diff_for)
                - int(ENTRY_SECRET in self.getting_diff_for),
            )
            self.observer.decision(scope, snapshot)
        except Exception:  # noqa: BLE001 - admission cannot depend on observation
            pass

    def observe_difference_claim(self, channel_id: int | None) -> None:
        if self.observer is None:
            return
        try:
            scope = "account" if channel_id is None else "channel"
            now = time.monotonic()
            entry = ENTRY_ACCOUNT if channel_id is None else channel_id
            snapshot = self.stream_snapshot(entry, now=now, in_flight=True)
            snapshot.update(
                decision="physical_claim",
                observed_at_ms=int(time.time() * 1000),
                intake_queue_depth=self.intake_queue.real_count if self.intake_queue is not None else 0,
                pending_channels=len(self.getting_diff_for)
                - int(ENTRY_ACCOUNT in self.getting_diff_for)
                - int(ENTRY_SECRET in self.getting_diff_for),
            )
            self.observer.decision(scope, snapshot)
        except Exception:  # noqa: BLE001 - claim cannot depend on observation
            pass

    def preserve_deferred(self, request: object, hint: TelegramRpcThrottled | TelegramRpcMethodRecoveryPending) -> None:
        channel = getattr(request, "channel", None)
        self._preserved_ends.add(None if channel is None else cast(int, channel.channel_id))
        self._blocked[self._method_key(request)] = hint

    def end_difference(self) -> None:
        if None in self._preserved_ends:
            self._preserved_ends.remove(None)
            return
        super().end_difference()

    def retire_access_lost(self, request: GetChannelDifferenceRequest, confirmed: Callable[[], bool]) -> None:
        channel_id = cast(types.InputChannel, request.channel).channel_id
        self._access_loss_ends[channel_id] = confirmed

    def end_channel_difference(
        self, request: GetChannelDifferenceRequest, reason: PrematureEndReason, chat_hashes: object
    ) -> None:
        channel_id = cast(types.InputChannel, request.channel).channel_id
        if channel_id in self._preserved_ends:
            self._preserved_ends.remove(channel_id)
            return
        confirmed = self._access_loss_ends.pop(channel_id, None)
        if confirmed is not None:
            if not confirmed():
                return
            reason = PrematureEndReason.BANNED
        super().end_channel_difference(request, reason, chat_hashes)

    def check_deadlines(self) -> float:
        deadline = cast(float, super().check_deadlines())
        if self.intake_due():
            return math.inf
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
        if self._contact_scope_valid is not None and not self._contact_scope_valid(updates):
            raise GapError
        preserve_account = bool(self.getting_diff_for & {ENTRY_ACCOUNT, ENTRY_SECRET})
        seq, date = self.seq, self.date
        seq_rejection = self._sequence_rejection(updates, seq)
        pts_before = self._pts_invocations
        accepted_before = self._pts_accepted
        try:
            try:
                outcome = cast(tuple[list[object], list[object]], super().process_updates(updates, chat_hashes, result))
            except GapError:
                self._observe_box(updates, result, "gap")
                if seq_rejection == "gap" and self._rescue_user_invalidations(updates, result):
                    return [], []
                raise
            self._observe_processed(updates, result, seq, pts_before, accepted_before)
            if seq_rejection == "duplicate" and self._rescue_user_invalidations(updates, result):
                return [], []
            return outcome
        finally:
            if preserve_account:
                self.seq, self.date = seq, date

    @staticmethod
    def _sequence_rejection(updates: object, current_seq: int) -> str | None:
        if not isinstance(updates, (types.Updates, types.UpdatesCombined)) or getattr(updates, "_self_outgoing", False):
            return None
        if updates.date is None:
            return None
        seq_start = getattr(updates, "seq_start", None)
        if seq_start is None:
            seq_start = updates.seq
        if not seq_start:
            return None
        if seq_start <= current_seq:
            return "duplicate"
        return "gap" if seq_start > current_seq + 1 else None

    @staticmethod
    def _rescue_user_invalidations(updates: object, result: list[object]) -> bool:
        rescued = [item for item in _user_invalidations(updates) if hasattr(item, "_mcp_receipt_scope_token")]
        result.extend(rescued)
        return bool(rescued)

    def _observe_processed(
        self, updates: object, result: list[object], seq: int, pts_before: int, accepted_before: int
    ) -> None:
        try:
            seq_start = getattr(updates, "seq_start", None)
            if seq_start is None:
                seq_start = getattr(updates, "seq", 0) or 0
            if self._pts_invocations == pts_before and seq_start and seq_start <= seq:
                stage = "seq_duplicate"
            elif getattr(updates, "_self_outgoing", False) and self._pts_accepted > accepted_before:
                stage = "self_outgoing"
            else:
                stage = "accepted" if result else "suppressed"
            self._observe_box(updates, result, stage)
        except Exception:  # noqa: BLE001 - diagnostics cannot change vendor results
            pass

    def _observe_box(self, envelope: object, result: list[object], stage: str) -> None:
        if self.observer is None:
            return
        try:
            trace = getattr(envelope, "_mcp_delivery_trace", None)
            trace = trace if isinstance(trace, DeliveryTrace) else None
            scope = getattr(envelope, "_mcp_delivery_scope", "account")
            self.observer.stage(trace, scope=scope, stage=stage, observed_at_ms=int(time.time() * 1000))
            for update in result:
                vars(update).setdefault("_mcp_delivery_trace", trace)
                vars(update).setdefault("_mcp_delivery_scope", scope)
        except Exception:  # noqa: BLE001 - diagnostics cannot change vendor results
            pass
