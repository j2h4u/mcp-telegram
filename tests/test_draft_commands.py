"""Draft commands preserve user state and never repeat an ambiguous write."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from telethon.errors import ChatWriteForbiddenError, FloodWaitError  # type: ignore[import-untyped]
from telethon.tl import types  # type: ignore[import-untyped]
from telethon.tl.functions.messages import GetAllDraftsRequest, SaveDraftRequest  # type: ignore[import-untyped]

from mcp_telegram.config import DraftRecoveryConfig
from mcp_telegram.drafts import commands
from mcp_telegram.drafts.commands import DraftCommandError, DraftCommandOwner
from mcp_telegram.drafts.contracts import DraftObservationSource, DraftScope, SnapshotCoverage
from mcp_telegram.drafts.history import (
    get_draft_history,
    list_draft_history,
    read_current_draft,
    read_draft_operation,
)
from mcp_telegram.drafts.owner import DraftMessageOwner
from mcp_telegram.drafts.sqlite_projection import SQLiteDraftProjection
from mcp_telegram.drafts.telethon_adapter import normalize_update_draft
from mcp_telegram.event_handlers import UpdateProcessingBarrier
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.telegram_demand import current_demand_token
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcScope, current_rpc_scope
from tests.sqlite_writer_helpers import inline_writer
from tests.test_telegram_rpc_flood_outcome import _server_response
from tests.test_telegram_rpc_wire_integration import _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire


class _Harness:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.scope = DraftScope(100, 200)
        self.repository = SQLiteDraftProjection(conn, DraftRecoveryConfig())
        self.repository.bind_account(100)
        self.clock = int(time.time())
        self.remote: types.DraftMessage | None = None
        self.preflights = 0
        self.requests: list[SaveDraftRequest] = []
        self.recoveries: list[str] = []
        self.before_error: BaseException | None = None
        self.after_error: BaseException | None = None
        self.after_write: Callable[[], None] | None = None
        self.preflight_result = True
        self.peer: object = types.InputPeerUser(200, 123)
        self.after_preflight: Callable[[], None] | None = None
        self.response: object = True
        self.publish()
        self.owner = self.new_owner()

    def new_owner(self, client: object | None = None) -> DraftCommandOwner:
        return DraftCommandOwner(
            self if client is None else client,
            self.conn,
            writer=inline_writer(self.conn),
            account_id=100,
            resolve_peer=self.resolve_peer,
            preflight=self.preflight,
            request_recovery=self.recover,
        )

    def set_remote(self, text: str, **fields: object) -> None:
        self.clock += 1
        self.remote = types.DraftMessage(text, datetime.fromtimestamp(self.clock, UTC))
        for name, value in fields.items():
            setattr(self.remote, name, value)

    def publish(self) -> None:
        self.clock += 1
        at = datetime.fromtimestamp(self.clock, UTC)
        observations = []
        if self.remote is not None:
            raw_peer: object = types.PeerUser(self.scope.dialog_id)
            if self.scope.dialog_id < 0:
                raw_peer = types.PeerChannel(-self.scope.dialog_id - 1_000_000_000_000)
            observation = normalize_update_draft(
                types.UpdateDraftMessage(raw_peer, self.remote, top_msg_id=self.scope.top_message_id),
                account_id=100,
                source=DraftObservationSource.SNAPSHOT,
                observed_at=at,
            )
            assert observation is not None
            observations.append(observation)
        self.repository.mark_recovery_needed(reason="test", observed_at=at)
        claim = self.repository.claim_recovery(now=self.clock)
        assert claim is not None
        applied = self.repository.apply_snapshot(
            observations,
            SnapshotCoverage(100, True, len(observations), source_order_at=at),
            claim_token=claim,
        )
        assert applied.accepted

    async def resolve_peer(self, scope: DraftScope) -> object:
        assert scope == self.scope
        return self.peer

    async def preflight(self) -> bool:
        assert current_demand_token().kind is DemandKind.DRAFT_MUTATION
        budget = current_rpc_scope().attempt_budget
        assert budget is not None and budget.limit == 1
        budget.debit()
        self.preflights += 1
        if self.preflight_result:
            self.publish()
        if self.after_preflight is not None:
            self.after_preflight()
        return self.preflight_result

    async def recover(self, reason: str) -> None:
        self.recoveries.append(reason)

    async def __call__(self, request: object) -> object:
        assert isinstance(request, SaveDraftRequest)
        assert current_demand_token().kind is DemandKind.DRAFT_MUTATION
        assert current_rpc_scope().acquisition_kind is None
        if self.before_error is not None:
            raise self.before_error
        budget = current_rpc_scope().attempt_budget
        assert budget is not None and budget.limit == 1
        budget.debit()
        self.requests.append(request)
        if request.message:
            self.set_remote(
                request.message,
                entities=request.entities,
                reply_to=request.reply_to,
                no_webpage=request.no_webpage,
                invert_media=request.invert_media,
                media=request.media,
                effect=request.effect,
                suggested_post=request.suggested_post,
            )
        else:
            self.remote = None
        if self.after_write is not None:
            self.after_write()
        if self.after_error is not None:
            raise self.after_error
        return self.response

    def command(self, action: str, key: str, **fields: object) -> dict[str, object]:
        request: dict[str, object] = {
            "action": action,
            "dialog_id": self.scope.dialog_id,
            "operation_key": key,
        }
        if self.scope.top_message_id is not None:
            request["topic_id"] = self.scope.top_message_id
        if action != "create":
            request["expected_version"] = read_current_draft(self.conn, self.scope).expected_version
        return {**request, **fields}


@pytest.fixture()
def harness(tmp_path: Path) -> Iterator[_Harness]:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        yield _Harness(conn)
    finally:
        conn.close()


async def test_create_acknowledges_without_fabricating_observation(harness: _Harness) -> None:
    result = await harness.owner.execute(harness.command("create", "create-one", text="draft text"))
    assert result["ok"] is True and result["outcome"] == "acknowledged"
    assert result["save_acknowledged"] is True and result["mutation_dispatched"] is True
    assert result["result_persisted"] is True
    assert result["current_is_operation_confirmation"] is False
    assert cast(dict[str, object], result["current"])["presence"] == "absent"
    assert read_current_draft(harness.conn, harness.scope).presence == "absent"
    assert harness.remote is not None and harness.remote.message == "draft text"
    assert harness.recoveries == ["draft_mutation"]
    harness.publish()
    assert read_current_draft(harness.conn, harness.scope).text == "draft text"


async def test_update_clear_restore_retains_exact_preimages(harness: _Harness) -> None:
    harness.set_remote("old", entities=[types.MessageEntityTextUrl(0, 3, "https://example.invalid/private")])
    harness.publish()
    updated = await harness.owner.execute(harness.command("update", "update", text="new", drop_formatting=True))
    assert updated["outcome"] == "acknowledged"
    original_id = cast(int, updated["backup_history_id"])
    original = get_draft_history(harness.conn, harness.scope, original_id)
    assert original is not None and original.text == "old"
    harness.publish()
    cleared = await harness.owner.execute(harness.command("clear", "clear"))
    assert cleared["outcome"] == "acknowledged" and harness.remote is None
    harness.publish()
    restored = await harness.owner.execute(harness.command("restore", "restore", history_id=original_id))
    assert restored["outcome"] == "acknowledged"
    assert harness.remote is not None and harness.remote.message == "old"
    assert harness.remote.entities[0].url == "https://example.invalid/private"
    assert len(harness.requests) == 3
    assert len(list_draft_history(harness.conn, harness.scope)) == 2


async def test_existing_blank_composition_conflicts_with_create(harness: _Harness) -> None:
    harness.set_remote("", reply_to=types.InputReplyToMessage(25))
    result = await harness.owner.execute(harness.command("create", "existing", text="replacement"))
    assert result["outcome"] == "conflict" and result["error"] == "draft_exists"
    assert harness.requests == []
    assert harness.remote is not None and isinstance(harness.remote.reply_to, types.InputReplyToMessage)
    assert harness.remote.reply_to.reply_to_msg_id == 25


async def test_preflight_detects_unobserved_remote_edit(harness: _Harness) -> None:
    harness.set_remote("old")
    harness.publish()
    request = harness.command("update", "stale", text="mine")
    harness.set_remote("edited on phone")
    result = await harness.owner.execute(request)
    assert result["error"] == "version_conflict"
    assert cast(dict[str, object], result["current"])["text"] == "edited on phone"
    assert harness.requests == []


async def test_repeated_key_and_restarted_owner_never_write_again(harness: _Harness) -> None:
    request = harness.command("create", "same-key", text="first")
    first = await harness.owner.execute(request)
    harness.owner = harness.new_owner()
    assert await harness.owner.execute(request) == first
    conflict = await harness.owner.execute({**request, "text": "different"})
    assert conflict["error"] == "operation_conflict"
    assert len(harness.requests) == harness.preflights == 1


async def test_unknown_outcome_does_not_replay_or_permanently_lock_scope(harness: _Harness) -> None:
    harness.after_error = TimeoutError("private failure payload")
    request = harness.command("create", "uncertain", text="maybe saved")
    first = await harness.owner.execute(request)
    assert first["outcome"] == "unknown" and first["save_acknowledged"] is None
    assert first["mutation_dispatched"] is True
    assert first["current_is_operation_confirmation"] is False
    assert await harness.new_owner().execute(request) == first
    assert len(harness.requests) == 1
    harness.after_error = None
    harness.publish()
    next_result = await harness.owner.execute(harness.command("update", "new-intent", text="observed and changed"))
    assert next_result["outcome"] == "acknowledged"
    assert len(harness.requests) == 2


@pytest.mark.parametrize("after_dispatch", [False, True])
async def test_cancellation_records_actual_dispatch_boundary(harness: _Harness, after_dispatch: bool) -> None:
    if after_dispatch:
        harness.after_error = asyncio.CancelledError()
    else:
        harness.before_error = asyncio.CancelledError()
    request = harness.command("create", "cancelled", text="draft")
    with pytest.raises(asyncio.CancelledError):
        await harness.owner.execute(request)
    receipt = read_draft_operation(harness.conn, 100, "cancelled")
    assert receipt is not None
    assert receipt.outcome == ("unknown" if after_dispatch else "rejected")
    assert receipt.result["mutation_dispatched"] is after_dispatch
    repeat = await harness.owner.execute(request)
    assert repeat == receipt.result
    assert len(harness.requests) == int(after_dispatch)


@pytest.mark.parametrize("outcome", ["acknowledged", "unknown"])
async def test_receipt_write_failure_preserves_remote_outcome(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    def fail_receipt(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("private database details")

    monkeypatch.setattr(commands, "finish_draft_operation", fail_receipt)
    if outcome == "unknown":
        harness.after_error = TimeoutError()
    request = harness.command("create", "receipt-failure", text="sent to composer")
    result = await harness.owner.execute(request)
    assert result["ok"] is True and result["outcome"] == outcome
    assert result["save_acknowledged"] is (True if outcome == "acknowledged" else None)
    assert result["mutation_dispatched"] is True and result["result_persisted"] is False
    receipt = read_draft_operation(harness.conn, 100, "receipt-failure")
    assert receipt is not None and receipt.outcome == "pending"
    repeated = await harness.new_owner().execute(request)
    assert repeated["outcome"] == "unknown" and repeated["mutation_dispatched"] is None
    assert repeated["result_persisted"] is False
    assert repeated["current_is_operation_confirmation"] is False
    assert len(harness.requests) == 1


async def test_postwrite_read_failure_cannot_negate_acknowledgement(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_read(*_args: object) -> None:
        raise sqlite3.OperationalError("read failed")

    harness.after_write = lambda: monkeypatch.setattr(commands, "read_current_draft", fail_read)
    result = await harness.owner.execute(harness.command("create", "read-failure", text="saved"))
    assert result["outcome"] == "acknowledged" and result["save_acknowledged"] is True
    assert result["result_persisted"] is True
    assert result["current_is_operation_confirmation"] is False
    assert cast(dict[str, object], result["current"])["presence"] == "unknown"


async def test_definite_telegram_rejection_is_not_unknown(harness: _Harness) -> None:
    harness.after_error = ChatWriteForbiddenError(request=None)
    result = await harness.owner.execute(harness.command("create", "forbidden", text="draft"))
    assert result["ok"] is False and result["outcome"] == "rejected"
    assert result["error"] == "CHAT_WRITE_FORBIDDEN" and result["save_acknowledged"] is False
    assert result["mutation_dispatched"] is True
    receipt = read_draft_operation(harness.conn, 100, "forbidden")
    assert receipt is not None and receipt.outcome == "rejected"


@pytest.mark.parametrize("latched", [False, True])
async def test_server_flood_wait_is_persisted_as_rejected_through_actual_gate(
    harness: _Harness, wire: _WireHarness, monkeypatch: pytest.MonkeyPatch, latched: bool
) -> None:
    gate = wire[0]
    _server_response(wire, monkeypatch, FloodWaitError(None, capture=20))
    if latched:
        observe = gate._observe_flood

        async def observe_then_latch(exc: BaseException, *, scope: TelegramRpcScope | None = None) -> int:
            await observe(exc, scope=scope)
            raise TelegramRpcThrottled(latched=True)

        monkeypatch.setattr(gate, "_observe_flood", observe_then_latch)
    owner = harness.new_owner(gate)
    request = harness.command("create", "server-flood", text="still unsent")
    result = await owner.execute(request)
    assert result["ok"] is False and result["outcome"] == "rejected", result
    assert result["mutation_dispatched"] is True and result["save_acknowledged"] is False
    assert result["result_persisted"] is True
    assert result["details"] == {
        "retry_after_seconds": None if latched else 20,
        "latched": latched,
        "request_rejected": True,
    }
    assert ("account_protection" if latched else "20 seconds") in str(result["message"])
    assert "new operation_key" in str(result["required_action"])
    receipt = read_draft_operation(harness.conn, 100, "server-flood")
    assert receipt is not None and receipt.outcome == "rejected" and receipt.result == result
    assert await harness.new_owner(gate).execute(request) == result
    assert len(wire[3].frames) == 1 and harness.remote is None


@pytest.mark.parametrize("latched", [False, True])
async def test_generic_throttle_after_physical_dispatch_remains_unknown(
    harness: _Harness, wire: _WireHarness, monkeypatch: pytest.MonkeyPatch, latched: bool
) -> None:
    _server_response(
        wire,
        monkeypatch,
        TelegramRpcThrottled(None if latched else 20, latched=latched),
    )
    owner = harness.new_owner(wire[0])
    request = harness.command("create", "uncertain-throttle", text="maybe saved")
    result = await owner.execute(request)
    assert result["outcome"] == "unknown" and result["save_acknowledged"] is None
    assert result["mutation_dispatched"] is True and result["result_persisted"] is True
    assert result["current_is_operation_confirmation"] is False
    assert await harness.new_owner(wire[0]).execute(request) == result
    assert len(wire[3].frames) == 1


@pytest.mark.parametrize("latched", [False, True])
async def test_preflight_throttle_preserves_wait_and_same_key_retry(harness: _Harness, latched: bool) -> None:
    def throttle() -> None:
        raise TelegramRpcThrottled(None if latched else 20, latched=latched)

    harness.after_preflight = throttle
    request = harness.command("create", "preflight-throttle", text="draft")
    result = await harness.owner.execute(request)
    assert result["outcome"] == "rejected"
    assert result["error"] == ("flood_wait_kill_switch_open" if latched else "telegram_rpc_throttled")
    assert result["details"] == {
        "retry_after_seconds": None if latched else 20,
        "latched": latched,
        "request_rejected": False,
    }
    assert "same operation_key" in str(result["required_action"])
    assert read_draft_operation(harness.conn, 100, "preflight-throttle") is None
    assert harness.requests == []
    harness.after_preflight = None
    assert (await harness.owner.execute(request))["outcome"] == "acknowledged"


@pytest.mark.parametrize("action", ["update", "clear"])
async def test_noop_does_not_create_history(harness: _Harness, action: str) -> None:
    fields: dict[str, object] = {}
    if action == "update":
        harness.set_remote("unchanged")
        harness.publish()
        fields["text"] = "unchanged"
    result = await harness.owner.execute(harness.command(action, "noop", **fields))
    assert result["outcome"] == "unchanged"
    assert result["current_is_operation_confirmation"] is True
    assert result["mutation_dispatched"] is False and result["backup_history_id"] is None
    assert harness.conn.execute("SELECT COUNT(*) FROM draft_versions").fetchone() == (0,)
    assert harness.requests == []


async def test_noop_does_not_confirm_an_unavailable_observation(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        harness.owner,
        "_current_receipt",
        lambda _scope: {
            "presence": "unknown",
            "expected_version": None,
            "text": None,
            "observed_at": None,
            "uncertain": True,
        },
    )
    result = await harness.owner.execute(harness.command("clear", "noop-unavailable"))
    assert result["outcome"] == "unchanged"
    assert result["current_is_operation_confirmation"] is False


async def test_update_requires_explicit_formatting_drop(harness: _Harness) -> None:
    harness.set_remote("bold", entities=[types.MessageEntityBold(0, 4)])
    harness.publish()
    result = await harness.owner.execute(harness.command("update", "formatting", text="new text"))
    assert result["ok"] is False and result["error"] == "draft_formatting_conflict"
    assert "drop_formatting=true" in str(result["message"])
    assert harness.requests == []


async def test_unrestorable_current_draft_blocks_clear(harness: _Harness) -> None:
    harness.set_remote("with media", media=types.InputMediaPhoto(types.InputPhoto(1, 2, b"reference")))
    harness.publish()
    result = await harness.owner.execute(harness.command("clear", "unrestorable"))
    assert result["ok"] is False and result["error"] == "restore_unavailable"
    assert harness.requests == [] and harness.remote is not None


async def test_incomplete_preflight_cannot_create_from_missing_row(harness: _Harness) -> None:
    harness.preflight_result = False
    result = await harness.owner.execute(harness.command("create", "incomplete", text="draft"))
    assert result["error"] == "draft_state_unknown" and harness.requests == []
    assert read_draft_operation(harness.conn, 100, "incomplete") is None


@pytest.mark.parametrize("topic_id", [1, 42])
async def test_forum_create_and_clear_preserve_exact_route(harness: _Harness, topic_id: int) -> None:
    harness.scope = DraftScope(100, -1_000_000_000_777, topic_id)
    harness.peer = types.InputPeerChannel(777, 123)
    created = await harness.owner.execute(harness.command("create", "topic-create", text="topic draft"))
    assert created["outcome"] == "acknowledged"
    reply = harness.requests[0].reply_to
    assert isinstance(reply, types.InputReplyToMessage)
    assert reply.reply_to_msg_id == 0 and reply.top_msg_id == topic_id
    harness.publish()
    cleared = await harness.owner.execute(harness.command("clear", "topic-clear"))
    assert cleared["outcome"] == "acknowledged"
    assert harness.requests[1].message == ""
    cleared_reply = harness.requests[1].reply_to
    assert isinstance(cleared_reply, types.InputReplyToMessage)
    assert cleared_reply.reply_to_msg_id == 0
    assert cleared_reply.top_msg_id == topic_id


@pytest.mark.parametrize(
    "override,reason",
    [
        ({"account_id": 999}, "account_mismatch"),
        ({"account_id": True}, "account_mismatch"),
        ({"dialog_id": True}, "invalid_arguments"),
        ({"topic_id": -1}, "invalid_arguments"),
        ({"subdialog_peer_id": 200}, "unsupported_scope"),
        ({"text": "  "}, "invalid_arguments"),
        ({"text": "\ud800"}, "invalid_arguments"),
        ({"operation_key": ""}, "invalid_arguments"),
        ({"action": "send"}, "invalid_arguments"),
        ({"action": "update"}, "version_required"),
        ({"history_id": 1}, "invalid_arguments"),
        ({"drop_formatting": "yes"}, "invalid_arguments"),
    ],
)
async def test_invalid_commands_never_acquire(harness: _Harness, override: dict[str, object], reason: str) -> None:
    result = await harness.owner.execute({**harness.command("create", "invalid", text="draft"), **override})
    assert result["error"] == reason and harness.preflights == 0 and harness.requests == []


@pytest.mark.parametrize("peer", [None, types.InputPeerUser(999, 123), object()])
async def test_exact_peer_is_required_before_preflight(harness: _Harness, peer: object) -> None:
    harness.peer = peer
    result = await harness.owner.execute(harness.command("create", "peer", text="draft"))
    assert result["error"] in {"peer_unavailable", "peer_mismatch"}
    assert harness.preflights == 0 and harness.requests == []


async def test_domain_target_error_is_preserved(harness: _Harness) -> None:
    async def resolve(_scope: DraftScope) -> object:
        raise DraftCommandError("topic_closed", "The selected forum topic is closed.")

    owner = DraftCommandOwner(
        harness,
        harness.conn,
        writer=inline_writer(harness.conn),
        account_id=100,
        resolve_peer=resolve,
        preflight=harness.preflight,
        request_recovery=harness.recover,
    )
    result = await owner.execute(harness.command("create", "closed-topic", text="draft"))
    assert result["error"] == "topic_closed" and harness.preflights == 0


async def test_real_snapshot_owner_and_adapter_share_command_budgets(harness: _Harness) -> None:
    acquired = []

    async def client(request: object) -> object:
        if isinstance(request, GetAllDraftsRequest):
            scope = current_rpc_scope()
            assert scope.attempt_budget is not None and scope.attempt_budget.limit == 1
            scope.attempt_budget.debit()
            assert scope.acquisition_kind is not None
            assert current_demand_token().kind is DemandKind.DRAFT_MUTATION
            acquired.append(type(request).__name__)
            harness.clock += 1
            return types.Updates([], [], [], datetime.fromtimestamp(harness.clock, UTC), 1)
        return await harness(request)

    draft_owner = DraftMessageOwner(
        client,
        harness.repository,
        asyncio.Event(),
        UpdateProcessingBarrier(),
        writer=inline_writer(harness.conn),
    )
    await draft_owner.bind_account(100)
    owner = DraftCommandOwner(
        client,
        harness.conn,
        writer=inline_writer(harness.conn),
        account_id=100,
        resolve_peer=harness.resolve_peer,
        preflight=draft_owner.refresh_for_mutation,
        request_recovery=draft_owner.request_recovery,
    )
    request = harness.command("create", "actual-owner", text="native draft")
    claim = harness.repository.claim_mutation_preflight(now=time.time())
    assert claim is not None
    busy = await owner.execute(request)
    assert busy["error"] == "draft_reconciliation_busy"
    assert "Retry the same command" in str(busy["message"])
    assert acquired == [] and harness.requests == []
    assert read_draft_operation(harness.conn, 100, "actual-owner") is None
    assert harness.repository.release_recovery_claim(claim_token=claim, due_at=time.time())
    result = await owner.execute(request)
    assert result["outcome"] == "acknowledged", result
    assert acquired == ["GetAllDraftsRequest"] and len(harness.requests) == 1
    assert harness.conn.execute("SELECT status FROM draft_sync_state WHERE account_id=100").fetchone() == (
        "recovery_needed",
    )
