"""Snapshot distinguishes observed gaps from routine catch-up deadlines."""

from datetime import UTC, datetime
from typing import cast

import pytest
from telethon._updates import SessionState
from telethon._updates.messagebox import ENTRY_ACCOUNT, PossibleGap, State

from mcp_telegram.telegram_update_admission import GateMessageBox
from tests.test_telegram_rpc_wire_integration import _WireHarness
from tests.test_telegram_update_fairness import setup_gap
from tests.test_telegram_update_fairness import wire as wire


def test_snapshot_keeps_deadline_and_actual_gap_separate(wire: _WireHarness) -> None:
    box, _ = setup_gap(wire)
    box.load(SessionState(42, 2, False, 10, 0, int(datetime.now(UTC).timestamp()), 1, None), [])
    box.getting_diff_for.clear()
    box.map[ENTRY_ACCOUNT].deadline = 20
    box.map[123] = State(10, 30)
    snapshot = box.delivery_snapshot(123, now=25, account_in_flight=False)
    assert snapshot["account"] == {
        "gap_state": "none_observed",
        "catch_up_state": "due",
        "reason": "deadline",
        "overdue_seconds": 5,
    }
    assert snapshot["channel"] == {"gap_state": "none_observed", "catch_up_state": "not_due", "reason": "not_due"}
    box.possible_gaps[123] = PossibleGap(27, [])
    assert box.stream_snapshot(123, now=25, in_flight=False)["gap_state"] == "observed"
    box.try_begin_get_diff(123, "detected gap")
    assert box.stream_snapshot(123, now=25, in_flight=False)["gap_state"] == "observed"
    box.end_get_diff(123)
    assert box.stream_snapshot(123, now=25, in_flight=False)["gap_state"] == "none_observed"


def test_missing_stream_does_not_claim_delivery_health(wire: _WireHarness) -> None:
    box: GateMessageBox = setup_gap(wire)[0]
    assert box.stream_snapshot(321, now=0, in_flight=False) == {
        "gap_state": "unknown",
        "catch_up_state": "unknown",
        "reason": "owner_unavailable",
    }


def test_physical_claim_and_pending_are_distinct(wire: _WireHarness) -> None:
    box, _ = setup_gap(wire)
    box._eligibility = lambda _key: None
    pending = box.stream_snapshot(ENTRY_ACCOUNT, now=0, in_flight=False)
    active = box.stream_snapshot(ENTRY_ACCOUNT, now=0, in_flight=True)
    assert pending["catch_up_state"] == "due"
    assert active["catch_up_state"] == "in_flight"


def test_vendor_pts_dispositions_remain_distinct(wire: _WireHarness) -> None:
    from mcp_telegram.message_delivery_observations import MessageDeliveryObservations
    from tests.test_message_delivery_observations import Sink
    from tests.test_telegram_update_fairness_delivery import _update

    gate = wire[0]
    box, now = setup_gap(wire)
    box.getting_diff_for.clear()
    box.map[ENTRY_ACCOUNT].pts = 10
    sink = Sink()
    observer = MessageDeliveryObservations(sink)
    gate.set_message_delivery_observer(observer)
    for mid, pts in ((12, 9), (13, 13)):
        envelope = _update(now, mid, pts)
        gate._updates_queue.put_nowait(envelope)
        item = gate._updates_queue.get_nowait()
        box.process_updates(item, gate._mb_entity_cache, [])
    observer.flush()
    stages = [cast(dict[str, object], row["payload"]).get("stage") for row in sink.rows if row["outcome"] == "count"]
    assert "pts_duplicate" in stages
    assert "possible_gap" in stages


def test_short_chat_intake_retains_marked_peer_and_sent_time(wire: _WireHarness) -> None:
    from telethon import types

    from mcp_telegram.message_delivery_observations import MessageDeliveryObservations
    from tests.test_message_delivery_observations import Sink

    gate = wire[0]
    sink = Sink()
    observer = MessageDeliveryObservations(sink)
    gate.set_message_delivery_observer(observer)
    now = datetime.now(UTC)
    update = types.UpdateShortChatMessage(
        id=41, from_id=55, chat_id=123, message="fixture", pts=11, pts_count=1, date=now
    )
    gate._updates_queue.put_nowait(update)
    observer.flush()
    sample = cast(dict[str, object], next(row["payload"] for row in sink.rows if row["outcome"] == "sample"))
    assert (-123, 41, int(now.timestamp() * 1000)) in cast(list[tuple[int, int, int | None]], sample["identities"])


def test_due_account_with_channel_backlog_and_floor_is_recorded(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from telethon.tl.functions.updates import GetDifferenceRequest

    from mcp_telegram.message_delivery_observations import MessageDeliveryObservations
    from mcp_telegram.telegram_rpc_scheduler import TelegramRpcAdmissionDeferred
    from tests.test_message_delivery_observations import Sink

    gate = wire[0]
    box, _ = setup_gap(wire)
    box.map[ENTRY_ACCOUNT].deadline = 10
    for channel in range(100, 220):
        box.map[channel] = State(1, 100)
        box.getting_diff_for.add(channel)
    sink = Sink()
    observer = MessageDeliveryObservations(sink)
    gate.set_message_delivery_observer(observer)
    box._eligibility = lambda _key: TelegramRpcAdmissionDeferred(60, eligible_at=80)
    result = box.stream_snapshot(ENTRY_ACCOUNT, now=20, in_flight=False)
    assert result["catch_up_state"] == "blocked"
    assert result["eligible_in_seconds"] == 60
    assert result["overdue_seconds"] == 10
    monkeypatch.setattr("mcp_telegram.telegram_update_admission.time.monotonic", lambda: 20)
    request = GetDifferenceRequest(10, datetime.now(UTC), 0)
    assert box._eligible_request(request) is None
    observer.flush()
    row = cast(dict[str, object], next(r["payload"] for r in sink.rows if r["outcome"] == "catchup"))
    assert row["pending_channels"] == 120
    assert row["eligible_in_seconds"] == 60
