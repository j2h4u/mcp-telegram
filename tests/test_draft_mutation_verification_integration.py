"""Draft commands verify their resulting state through the actual guarded sender."""

import asyncio
import sqlite3
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from telethon import types
from telethon.tl.functions.messages import GetAllDraftsRequest, SaveDraftRequest

from mcp_telegram import telegram_demand
from mcp_telegram.config import DraftRecoveryConfig
from mcp_telegram.drafts.commands import DraftCommandOwner
from mcp_telegram.drafts.contracts import DraftScope
from mcp_telegram.drafts.history import read_current_draft, read_draft_operation
from mcp_telegram.drafts.owner import DraftMessageOwner
from mcp_telegram.drafts.read_service import read_draft_request
from mcp_telegram.drafts.sqlite_projection import SQLiteDraftProjection
from mcp_telegram.event_handlers import UpdateProcessingBarrier
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.telegram_demand import DurableDemandAdapter, RpcAttemptBudget, RpcAttemptEvidence, demand_context
from mcp_telegram.telegram_demand_coordinator import TelegramDemandCoordinator
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import rpc_attempt_budget
from tests.test_telegram_demand_coordinator import _adapters
from tests.test_telegram_rpc_draft_mutation import _clock
from tests.test_telegram_rpc_safety import Clock
from tests.test_telegram_rpc_wire_integration import _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire


@dataclass
class _DraftRig:
    wire: _WireHarness
    conn: sqlite3.Connection
    writer: SQLiteWriter
    owner: DraftMessageOwner
    commands: DraftCommandOwner
    clock: Clock
    scope: DraftScope
    deadlines: list[float | None] = field(default_factory=list)
    evidence: list[RpcAttemptEvidence | None] = field(default_factory=list)


@pytest.fixture
async def drafts(wire: _WireHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_DraftRig]:
    path = tmp_path / "drafts.sqlite"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    writer = await SQLiteWriter.open(path)
    gate = wire[0]
    scope = DraftScope(42, 200)
    owner = DraftMessageOwner(
        gate,
        SQLiteDraftProjection(conn, DraftRecoveryConfig()),
        asyncio.Event(),
        UpdateProcessingBarrier(),
        writer=writer,
    )
    await owner.bind_account(42)

    async def peer(_scope: DraftScope) -> object:
        assert _scope == scope
        return types.InputPeerUser(200, 123)

    commands = DraftCommandOwner(
        gate,
        conn,
        writer=writer,
        account_id=42,
        resolve_peer=peer,
        preflight=owner.refresh_for_mutation,
        postflight=owner.refresh_after_mutation,
        request_recovery=owner.request_recovery,
    )
    try:
        clock = _clock(wire, monkeypatch)
        monkeypatch.setattr(telegram_demand, "time", clock)
        yield _DraftRig(wire, conn, writer, owner, commands, clock, scope)
    finally:
        await writer.aclose()
        conn.close()


def _snapshot(draft: types.DraftMessage | None) -> types.Updates:
    updates: list[types.TypeUpdate] = [] if draft is None else [types.UpdateDraftMessage(types.PeerUser(200), draft)]
    return types.Updates(updates, [], [], datetime.now(UTC), 1)


async def _answer(rig: _DraftRig, expected: type, response: object) -> object:
    gate, sender, connection, _ = rig.wire
    state = await asyncio.wait_for(sender._send_queue.next_state(), 3)
    assert type(state.request) is expected
    await asyncio.wait_for(sender._prepare_state(state), 3)
    packet = connection._send_queue.get_nowait()
    owner = gate._require_wire_owner(packet.state.owner)
    assert owner.scope.attempt_budget is not None and owner.scope.attempt_budget.limit == 1
    rig.deadlines.append(owner.scope.deadline)
    rig.evidence.append(owner.scope.attempt_evidence)
    assert sender.write_packet(packet, connection._send)
    assert owner.scope.attempt_budget.attempts == 1
    if isinstance(response, BaseException):
        state.future.set_exception(response)
    else:
        state.future.set_result(response)
    rig.clock.advance(3)
    gate._admission_scheduler.notify_eligibility_changed()
    return state.request


def _draft(text: str) -> types.DraftMessage:
    return types.DraftMessage(text, datetime.now(UTC))


async def _execute(
    rig: _DraftRig, request: dict[str, object], before: types.DraftMessage | None, after: types.DraftMessage | None
) -> dict[str, object]:
    start = len(rig.wire[3].frames)
    start_clock = rig.clock.monotonic()
    task = asyncio.create_task(rig.commands.execute(request))
    try:
        await _answer(rig, GetAllDraftsRequest, _snapshot(before))
        save = cast(SaveDraftRequest, await _answer(rig, SaveDraftRequest, True))
        await _answer(rig, GetAllDraftsRequest, _snapshot(after))
        result = await asyncio.wait_for(task, 3)
        assert len(rig.wire[3].frames) == start + 3
        deadlines = rig.deadlines[start : start + 3]
        assert deadlines[0] is not None and deadlines[0] == deadlines[1] == deadlines[2]
        evidence = rig.evidence[start : start + 3]
        assert evidence[0] is not None and evidence[0] is evidence[1] is evidence[2]
        assert evidence[0].draft_preflight_dispatched
        assert evidence[0].draft_mutation_dispatched and evidence[0].draft_postflight_dispatched
        assert evidence[0].actual_attempts == 3
        elapsed = rig.clock.monotonic() - start_clock
        assert elapsed <= 9 or elapsed == pytest.approx(9, abs=1e-9, rel=0)
        assert result["ok"] is True and result["outcome"] == "acknowledged"
        assert result["save_acknowledged"] is True
        assert result["result_persisted"] is True and result["current_is_operation_confirmation"] is True
        assert result["verification"] == {"status": "verified", "basis": "post_write_snapshot"}
        current = cast(dict[str, object], result["current"])
        local = read_draft_request(rig.conn, {"dialog_id": 200}, account_id=42)
        assert local["current"] == {key: value for key, value in current.items() if value is not None}
        assert current["expected_version"] == read_current_draft(rig.conn, rig.scope).expected_version
        assert len(rig.wire[3].frames) == start + 3
        assert save.message == ("" if after is None else after.message)
        if after is not None and after.entities:
            assert save.entities is not None
            assert [bytes(entity) for entity in save.entities] == [bytes(entity) for entity in after.entities]
        if after is not None and after.reply_to is not None:
            assert save.reply_to is not None and bytes(save.reply_to) == bytes(after.reply_to)
        return result
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_create_update_clear_restore_verify_canonical_current(drafts: _DraftRig) -> None:
    first, second = _draft("first"), _draft("second")
    created = await _execute(
        drafts, {"action": "create", "dialog_id": 200, "operation_key": "create", "text": "first"}, None, first
    )
    current = cast(dict[str, object], created["current"])
    updated = await _execute(
        drafts,
        {
            "action": "update",
            "dialog_id": 200,
            "operation_key": "update",
            "text": "second",
            "expected_version": current["expected_version"],
        },
        first,
        second,
    )
    current = cast(dict[str, object], updated["current"])
    cleared = await _execute(
        drafts,
        {
            "action": "clear",
            "dialog_id": 200,
            "operation_key": "clear",
            "expected_version": current["expected_version"],
        },
        second,
        None,
    )
    current = cast(dict[str, object], cleared["current"])
    history_id = cleared["backup_history_id"]
    await _execute(
        drafts,
        {
            "action": "restore",
            "dialog_id": 200,
            "operation_key": "restore",
            "history_id": history_id,
            "expected_version": current["expected_version"],
        },
        None,
        second,
    )


async def test_unchanged_only_performs_authoritative_preflight(drafts: _DraftRig) -> None:
    async def preflight() -> bool:
        with demand_context(DemandKind.DRAFT_MUTATION), rpc_attempt_budget(RpcAttemptBudget(1)):
            return await drafts.owner.refresh_for_mutation()

    preflight_task = asyncio.create_task(preflight())
    await _answer(drafts, GetAllDraftsRequest, _snapshot(None))
    assert await preflight_task
    current = read_current_draft(drafts.conn, drafts.scope)
    start = len(drafts.wire[3].frames)
    task = asyncio.create_task(
        drafts.commands.execute(
            {
                "action": "clear",
                "dialog_id": 200,
                "operation_key": "unchanged",
                "expected_version": current.expected_version,
            }
        )
    )
    await _answer(drafts, GetAllDraftsRequest, _snapshot(None))
    result = await asyncio.wait_for(task, 3)
    assert result["outcome"] == "unchanged"
    assert result["verification"] == {"status": "verified", "basis": "preflight"}
    assert result["current_is_operation_confirmation"] is True
    assert len(drafts.wire[3].frames) == start + 1


async def test_authoritative_concurrent_edit_returns_conflict_without_overwrite(drafts: _DraftRig) -> None:
    task = asyncio.create_task(
        drafts.commands.execute({"action": "create", "dialog_id": 200, "operation_key": "conflict", "text": "ours"})
    )
    try:
        await _answer(drafts, GetAllDraftsRequest, _snapshot(None))
        await _answer(drafts, SaveDraftRequest, True)
        await _answer(drafts, GetAllDraftsRequest, _snapshot(_draft("newer edit")))
        result = await asyncio.wait_for(task, 3)
        assert result["outcome"] == "acknowledged" and result["save_acknowledged"] is True
        assert cast(dict[str, object], result["verification"])["status"] == "conflict"
        assert cast(dict[str, object], result["current"])["text"] == "newer edit"
        assert read_current_draft(drafts.conn, drafts.scope).text == "newer edit"
        assert len(drafts.wire[3].frames) == 3
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_same_text_with_new_format_and_reply_is_a_conflict(drafts: _DraftRig) -> None:
    request = {"action": "create", "dialog_id": 200, "operation_key": "same-text", "text": "linked"}
    actual = types.DraftMessage(
        "linked", datetime.now(UTC), entities=[types.MessageEntityBold(0, 6)], reply_to=types.InputReplyToMessage(88)
    )
    task = asyncio.create_task(drafts.commands.execute(request))
    try:
        await _answer(drafts, GetAllDraftsRequest, _snapshot(None))
        await _answer(drafts, SaveDraftRequest, True)
        await _answer(drafts, GetAllDraftsRequest, _snapshot(actual))
        result = await asyncio.wait_for(task, 3)
        assert result["outcome"] == "acknowledged" and result["save_acknowledged"] is True
        assert cast(dict[str, object], result["verification"])["status"] == "conflict"
        assert cast(dict[str, object], result["current"])["text"] == "linked"
        local = read_current_draft(drafts.conn, drafts.scope)
        assert local.restore_snapshot is not None
        from mcp_telegram.drafts.snapshot import decode_restore_snapshot

        observed = decode_restore_snapshot(local.restore_snapshot)
        assert isinstance(observed, types.DraftMessage)
        assert observed.entities and isinstance(observed.entities[0], types.MessageEntityBold)
        assert isinstance(observed.reply_to, types.InputReplyToMessage) and observed.reply_to.reply_to_msg_id == 88
        assert len(drafts.wire[3].frames) == 3
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_restore_replays_entities_and_reply_in_complete_composition(drafts: _DraftRig) -> None:
    rich = types.DraftMessage(
        "linked",
        datetime.now(UTC),
        no_webpage=True,
        entities=[types.MessageEntityTextUrl(0, 6, "https://example.invalid/")],
        reply_to=types.InputReplyToMessage(88),
    )

    async def seed() -> bool:
        with demand_context(DemandKind.DRAFT_MUTATION), rpc_attempt_budget(RpcAttemptBudget(1)):
            return await drafts.owner.refresh_for_mutation()

    task = asyncio.create_task(seed())
    await _answer(drafts, GetAllDraftsRequest, _snapshot(rich))
    assert await task
    initial = read_current_draft(drafts.conn, drafts.scope)
    assert initial.expected_version is not None
    cleared = await _execute(
        drafts,
        {
            "action": "clear",
            "dialog_id": 200,
            "operation_key": "rich-clear",
            "expected_version": initial.expected_version,
        },
        rich,
        None,
    )
    current = cast(dict[str, object], cleared["current"])
    await _execute(
        drafts,
        {
            "action": "restore",
            "dialog_id": 200,
            "operation_key": "rich-restore",
            "history_id": cleared["backup_history_id"],
            "expected_version": current["expected_version"],
        },
        None,
        rich,
    )


async def test_postflight_timeout_preserves_ack_and_replay_never_sends_again(drafts: _DraftRig) -> None:
    request: dict[str, object] = {"action": "create", "dialog_id": 200, "operation_key": "timeout", "text": "new"}
    task = asyncio.create_task(drafts.commands.execute(request))
    try:
        await _answer(drafts, GetAllDraftsRequest, _snapshot(None))
        await _answer(drafts, SaveDraftRequest, True)
        await _answer(drafts, GetAllDraftsRequest, TimeoutError("injected response timeout"))
        result = await asyncio.wait_for(task, 3)
        assert result["outcome"] == "acknowledged" and result["save_acknowledged"] is True
        assert cast(dict[str, object], result["verification"])["status"] == "unverified"
        assert result["current_is_operation_confirmation"] is False
        before = len(drafts.wire[3].frames)
        replay = await drafts.commands.execute(request)
        assert replay["outcome"] == "acknowledged" and replay["verification"] == result["verification"]
        assert len(drafts.wire[3].frames) == before == 3
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_postflight_cancellation_retains_ack_for_replay_without_resending(drafts: _DraftRig) -> None:
    request: dict[str, object] = {"action": "create", "dialog_id": 200, "operation_key": "cancel", "text": "new"}
    task = asyncio.create_task(drafts.commands.execute(request))
    try:
        await _answer(drafts, GetAllDraftsRequest, _snapshot(None))
        await _answer(drafts, SaveDraftRequest, True)
        gate, sender, connection, sink = drafts.wire
        state = await asyncio.wait_for(sender._send_queue.next_state(), 3)
        assert isinstance(state.request, GetAllDraftsRequest)
        await sender._prepare_state(state)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await gate._ensure_safety_ledger().drain()
        receipt = read_draft_operation(drafts.conn, 42, "cancel")
        assert receipt is not None and receipt.outcome == "acknowledged"
        assert cast(dict[str, object], receipt.result["verification"])["status"] == "unverified"

        async def resolve(scope: DraftScope) -> object:
            assert scope == drafts.scope
            return types.InputPeerUser(200, 123)

        restarted = DraftCommandOwner(
            gate,
            drafts.conn,
            writer=drafts.writer,
            account_id=42,
            resolve_peer=resolve,
            preflight=drafts.owner.refresh_for_mutation,
            postflight=drafts.owner.refresh_after_mutation,
            request_recovery=drafts.owner.request_recovery,
        )
        result = await restarted.execute(request)
        assert result["outcome"] == "acknowledged" and result["save_acknowledged"] is True
        assert len(sink.frames) == 3
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_busy_selected_snapshot_retries_same_revision_through_real_coordinator(drafts: _DraftRig) -> None:
    await drafts.owner.request_recovery("integration_test")
    drafts.clock.advance(1)
    kind = DemandKind.DRAFT_SNAPSHOT
    adapters: dict[DemandKind, DurableDemandAdapter] = dict(_adapters())
    adapters[kind] = drafts.owner
    coordinator = TelegramDemandCoordinator(adapters, clock=drafts.clock.time, monotonic_clock=drafts.clock.monotonic)
    drafts.owner.bind_demand_sink(coordinator)
    coordinator.scan()
    status = drafts.owner.status(drafts.clock.time())
    assert status is not None and status.selected is not None
    selected = status.selected
    assert kind in coordinator.ready_kinds
    await drafts.owner._snapshot_lock.acquire()
    try:
        await coordinator._execute_slice(kind)
        budget = coordinator._invocations[selected].budget
        assert budget.attempts == 0 and not drafts.wire[3].frames
        assert drafts.owner.status(drafts.clock.time()) is None
        assert coordinator._exclusion_reasons[selected] == "retryable_failure"
    finally:
        drafts.owner._snapshot_lock.release()
    coordinator._active_kind = None
    resumed = drafts.owner.status(drafts.clock.time())
    assert resumed is not None and resumed.selected == selected
    drafts.clock.advance(29)
    coordinator.scan()
    assert kind not in coordinator.ready_kinds
    drafts.clock.advance(1)
    coordinator.scan()
    assert kind in coordinator.ready_kinds
    task = asyncio.create_task(coordinator._execute_slice(kind))
    try:
        await _answer(drafts, GetAllDraftsRequest, _snapshot(_draft("recovered")))
        await asyncio.wait_for(task, 3)
        assert read_current_draft(drafts.conn, drafts.scope).text == "recovered"
        assert budget.attempts == 1 and len(drafts.wire[3].frames) == 1
        assert drafts.owner.status(drafts.clock.time()) is None
        assert selected not in coordinator._transient_stages
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_old_background_snapshot_after_ack_cannot_confirm_write(drafts: _DraftRig) -> None:
    request = {"action": "create", "dialog_id": 200, "operation_key": "old-snapshot", "text": "fresh"}
    command = asyncio.create_task(drafts.commands.execute(request))
    background: asyncio.Task[object] | None = None
    gate, sender, connection, sink = drafts.wire
    try:
        await _answer(drafts, GetAllDraftsRequest, _snapshot(None))
        save = await asyncio.wait_for(sender._send_queue.next_state(), 3)
        assert isinstance(save.request, SaveDraftRequest)
        await sender._prepare_state(save)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        drafts.clock.advance(3)
        gate._admission_scheduler.notify_eligibility_changed()

        await drafts.owner.request_recovery("old_background")
        status = drafts.owner.status(drafts.clock.time())
        assert status is not None and status.selected is not None
        background = asyncio.create_task(drafts.owner.run_slice(RpcAttemptBudget(1, selected=status.selected)))
        old = await asyncio.wait_for(sender._send_queue.next_state(), 3)
        assert isinstance(old.request, GetAllDraftsRequest)
        await sender._prepare_state(old)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        drafts.clock.advance(3)
        gate._admission_scheduler.notify_eligibility_changed()

        save.future.set_result(True)
        async with asyncio.timeout(3):
            while drafts.conn.execute("SELECT reason FROM draft_sync_state WHERE account_id=42").fetchone() != (
                "draft_mutation",
            ):
                await asyncio.sleep(0)
        receipt = read_draft_operation(drafts.conn, 42, "old-snapshot")
        assert receipt is not None and receipt.outcome == "acknowledged"
        assert cast(dict[str, object], receipt.result["verification"])["reason"] == "verification_in_progress"
        pending = cast(
            tuple[int | None, int | None] | None,
            drafts.conn.execute(
                "SELECT recovery_due_at,recovery_claimed_at FROM draft_projection_runtime WHERE singleton=1"
            ).fetchone(),
        )
        assert pending is not None and pending[0] is not None and pending[0] > time.time() and pending[1] is None
        old.future.set_result(_snapshot(None))
        await asyncio.wait_for(background, 3)
        assert not command.done()
        await _answer(drafts, GetAllDraftsRequest, _snapshot(_draft("fresh")))
        result = await asyncio.wait_for(command, 3)
        assert result["outcome"] == "acknowledged" and result["save_acknowledged"] is True
        assert cast(dict[str, object], result["verification"])["status"] == "verified"
        assert read_current_draft(drafts.conn, drafts.scope).text == "fresh"
        assert len(sink.frames) == 4
    finally:
        for task in (command, background):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
