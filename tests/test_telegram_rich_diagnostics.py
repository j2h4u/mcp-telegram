"""Observe native rich-hydration anomalies before Telethon normalizes them."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

import pytest
from telethon import TelegramClient, functions, types
from telethon.client.messages import _IDsIter
from telethon.errors import MessageIdsEmptyError

from mcp_telegram.config import RuntimeObservationConfig
from mcp_telegram.hydration_queue import HydrationJob
from mcp_telegram.media_hydration import MediaFactHydrationHandler, RichCompositionHydrationHandler
from mcp_telegram.peer_access import peer_target
from mcp_telegram.rpc_admission_observations import RpcAdmissionObservationAggregator
from mcp_telegram.telegram_demand import AcquisitionKind, RpcAttemptBudget
from mcp_telegram.telegram_demand_progress import WorkKey, WorkRevision
from mcp_telegram.telegram_rich_diagnostics import classify_rich_lookup
from mcp_telegram.telegram_rpc_consumers import DemandKind, demand_contract
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcScope
from tests.test_rpc_admission_observations import _Recorder
from tests.test_telegram_rpc import _gate


def test_classify_native_response_keeps_only_bounded_counts() -> None:
    request = functions.messages.GetMessagesRequest([types.InputMessageID(i) for i in (1, 2, 3, 4, 5)])
    response = types.messages.Messages(
        messages=[
            cast(
                types.TypeMessage, types.Message(1, types.PeerUser(11), date=datetime.now(UTC), message="private body")
            ),
            cast(types.TypeMessage, types.MessageEmpty(2, None)),
            cast(types.TypeMessage, types.Message(3, types.PeerUser(12), date=datetime.now(UTC), message="other peer")),
            cast(types.TypeMessage, types.Message(3, types.PeerUser(11), date=datetime.now(UTC), message="")),
            cast(types.TypeMessage, types.Message(99, types.PeerUser(11), date=datetime.now(UTC), message="")),
        ],
        topics=[],
        chats=[],
        users=[],
    )
    assert classify_rich_lookup(request, response, 11) == {
        "requested": 5,
        "matched": 1,
        "explicit_empty": 1,
        "wrong_peer": 1,
        "absent": 2,
        "unexpected": 2,
    }


@pytest.mark.parametrize(
    "case",
    [
        "channel_empty",
        "channel_exception",
        "ordinary_wrong_peer",
        "unrelated",
        "wrong_owner",
        "not_modified",
        "invalid",
    ],
)
async def test_real_telethon_ids_iterator_preserves_behavior_and_observes_native_anomalies(  # noqa: PLR0914, PLR0915
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    channel = case.startswith("channel")
    peer = types.InputPeerChannel(11, 77) if channel else types.InputPeerUser(11, 77)
    target = -1000000000011 if channel else 11
    kind = DemandKind.BACKFILL_HYDRATION_BATCH
    selected_kind = "media_metadata" if case == "unrelated" else "rich_composition"
    selected_demand = DemandKind.MESSAGE_FACT_REFRESH if case == "wrong_owner" else kind
    selected = WorkRevision(WorkKey(selected_demand, 42, f"{selected_kind}:{target}:123"), "revision")
    scope = TelegramRpcScope(
        demand_contract(kind).source,
        demand_contract(kind).service_class,
        10**12,
        asyncio.current_task(),
        demand_kind=kind,
        acquisition_kind=AcquisitionKind.MESSAGE_LOOKUP,
        attempt_budget=RpcAttemptBudget(1, selected=selected),
    )
    gate = _gate()
    recorder = _Recorder()
    aggregator = RpcAdmissionObservationAggregator(recorder, policy=RuntimeObservationConfig(), clock=lambda: 0.0)
    gate.set_rpc_request_observer(aggregator.observe_rpc_event)
    monkeypatch.setattr(gate, "_raise_if_transport_unavailable", lambda: None)
    monkeypatch.setattr(gate, "check_circuit", lambda: None)

    async def no_wait(_scope: object) -> None:
        pass

    monkeypatch.setattr(gate, "_wait_for_account_cooldown", no_wait)
    calls: list[object] = []

    async def native_response(_client: object, _sender: object, request: object, *, ordered: bool) -> object:
        del ordered
        calls.append(request)
        if case == "channel_exception":
            raise MessageIdsEmptyError(request)
        if case == "not_modified":
            return types.messages.MessagesNotModified(1)
        if case == "invalid":
            return object()
        item = (
            types.Message(123, types.PeerUser(12), date=datetime.now(UTC), message="")
            if case == "ordinary_wrong_peer"
            else types.MessageEmpty(123, None)
        )
        return types.messages.Messages(messages=[cast(types.TypeMessage, item)], topics=[], chats=[], users=[])

    monkeypatch.setattr(TelegramClient, "_call", native_response)

    class IteratorClient:
        async def get_input_entity(self, _entity: object) -> object:
            return peer

        async def _get_peer(self, _entity: object) -> object:
            return types.PeerUser(11)

        async def __call__(self, request: object) -> object:
            return await gate._dispatch_attempt(request, ordered=False, scope=scope)

        async def get_messages(self, entity: object, ids: list[int]) -> object:
            return await _IDsIter(self, len(ids), entity=entity, ids=ids).collect()

    handler = (
        MediaFactHydrationHandler(batch_size=1)
        if case == "unrelated"
        else RichCompositionHydrationHandler(batch_size=1)
    )
    job = HydrationJob(selected_kind, target, 123, 0, 0)
    if case == "invalid":
        with pytest.raises(AttributeError):
            await handler.request(IteratorClient(), [job])
    else:
        assert await handler.request(IteratorClient(), [job]) == [None]
    assert len(calls) == 1
    rows = [row for row in recorder.rows if row.get("outcome") == "rich_lookup_anomaly"]
    if case in {"unrelated", "wrong_owner"}:
        assert rows == []
        return
    assert len(rows) == 1
    payload = cast(dict[str, object], rows[0]["payload"])
    assert payload["requested"] == 1 and payload["matched"] == 0
    expected_reason = {
        "channel_exception": "message_ids_empty",
        "not_modified": "native_not_modified",
        "invalid": "native_invalid",
    }.get(case, "native_messages")
    assert payload["reason"] == expected_reason
    if case in {"channel_exception", "not_modified", "invalid"}:
        assert payload["absent"] == 0
    assert payload["wrong_peer"] == (1 if case == "ordinary_wrong_peer" else 0)
    assert payload["explicit_empty"] == (1 if case == "channel_empty" else 0)
    assert "123" not in str(payload) and str(target) not in str(payload)


async def test_gateway_keeps_healthy_result_and_observer_failures_passive(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _gate()
    kind = DemandKind.LIVE_HYDRATION_BATCH
    selected = WorkRevision(WorkKey(kind, 42, "rich_composition:11:123"), "revision")
    scope = TelegramRpcScope(
        demand_contract(kind).source,
        demand_contract(kind).service_class,
        10**12,
        asyncio.current_task(),
        demand_kind=kind,
        acquisition_kind=AcquisitionKind.MESSAGE_LOOKUP,
        attempt_budget=RpcAttemptBudget(1, selected=selected),
    )
    request = functions.messages.GetMessagesRequest([types.InputMessageID(123)])
    healthy = types.messages.Messages(
        messages=[
            cast(types.TypeMessage, types.Message(123, types.PeerUser(11), date=datetime.now(UTC), message="sensitive"))
        ],
        topics=[],
        chats=[],
        users=[],
    )
    current: list[object] = [healthy]
    events: list[dict[str, object]] = []
    gate.set_rpc_request_observer(lambda **event: events.append(event))
    monkeypatch.setattr(gate, "_raise_if_transport_unavailable", lambda: None)
    monkeypatch.setattr(gate, "check_circuit", lambda: None)

    async def no_wait(_scope: object) -> None:
        pass

    async def native_response(_client: object, _sender: object, _request: object, *, ordered: bool) -> object:
        del ordered
        value = current[0]
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(gate, "_wait_for_account_cooldown", no_wait)
    monkeypatch.setattr(TelegramClient, "_call", native_response)
    with peer_target(11):
        assert await gate._dispatch_attempt(request, ordered=False, scope=scope) is healthy
    assert events == []
    current[0] = types.messages.Messages(
        messages=[cast(types.TypeMessage, types.MessageEmpty(123, None))], topics=[], chats=[], users=[]
    )
    with peer_target(11):
        assert await gate._dispatch_attempt(request, ordered=False, scope=scope) is current[0]
    safe_scope = cast(TelegramRpcScope, events[0]["scope"])
    assert safe_scope.attempt_budget is None and safe_scope.owner_task is None
    assert "123" not in str(events[0]["rich_lookup_counts"])
    gate.set_rpc_request_observer(lambda **_event: (_ for _ in ()).throw(RuntimeError("observer failure")))
    with peer_target(11):
        assert await gate._dispatch_attempt(request, ordered=False, scope=scope) is current[0]
    error = asyncio.CancelledError()
    current[0] = error
    with peer_target(11), pytest.raises(asyncio.CancelledError) as caught:
        await gate._dispatch_attempt(request, ordered=False, scope=scope)
    assert caught.value is error
