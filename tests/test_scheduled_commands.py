"""Focused contract tests for scalar scheduled-message command ownership."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from telethon.tl import types  # type: ignore[import-untyped]
from telethon.tl.functions.messages import (  # type: ignore[import-untyped]
    DeleteScheduledMessagesRequest,
    EditMessageRequest,
    SendMessageRequest,
)

from mcp_telegram.observation_order import allocate_observation_order
from mcp_telegram.scheduled_commands import (
    SCHEDULED_REPEAT_PERIODS,
    ScheduledCommandError,
    ScheduledCommandOwner,
    ScheduledOperationConflictError,
    _build_request,
    _correlated_response,
    _parse_command,
    ensure_scheduled_receipt_schema,
    prepare_scheduled_operation,
    read_scheduled_operation,
    settle_interrupted_scheduled_operations,
)
from mcp_telegram.scheduled_contracts import scheduled_message_version
from mcp_telegram.scheduled_messages import upsert_scheduled_message
from mcp_telegram.scheduled_write_fence import (
    ScheduledMutationFence,
    ScheduledWriteRejected,
    current_scheduled_mutation_check,
)
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope
from mcp_telegram.tools.scheduled import _scheduled_error_response
from tests.sqlite_writer_helpers import inline_writer


def _request(action: str = "create", **fields: object) -> dict[str, object]:
    request: dict[str, object] = {
        "action": action,
        "account_id": 100,
        "dialog_id": 200,
        "operation_key": "operation-one",
    }
    return {**request, **fields}


def _row(**updates: object) -> dict[str, object]:
    return {
        "message_state": "scheduled",
        "scheduled_at": 2_000_000_000,
        "text": "old caption",
        "formatting_entities": "[]",
        "media_kind": "photo",
        "media_payload": '{"photo_id":1}',
        "schedule_repeat_period": 604_800,
        "has_rich_message": 0,
        "invert_media": 0,
        "video_processing_pending": 0,
        "is_service": 0,
        "forum_topic_id": None,
        "reply_to_msg_id": 33,
        "reply_to_peer_id": 200,
        "grouped_id": None,
        "observation_order": 8,
        **updates,
    }


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"unsupported": True}, "invalid_arguments"),
        ({"action": None}, "invalid_arguments"),
        ({"account_id": True}, "account_mismatch"),
        ({"dialog_id": True}, "invalid_arguments"),
        ({"topic_id": 0}, "invalid_arguments"),
        ({"operation_key": " padded "}, "invalid_arguments"),
        ({"scheduled_message_id": True}, "invalid_arguments"),
        ({"expected_version": 1}, "invalid_arguments"),
        ({"text": "\ud800"}, "invalid_arguments"),
        ({"schedule_at_utc": True}, "invalid_arguments"),
        ({"repeat_period_seconds": None}, "invalid_arguments"),
        ({"repeat_period_seconds": True}, "invalid_arguments"),
        ({"repeat_period_seconds": 1}, "unsupported_repeat_period"),
        ({"drop_formatting": 0}, "invalid_arguments"),
        ({"scheduled_message_id": 55}, "invalid_arguments"),
        ({"action": "update"}, "version_required"),
        ({"action": "cancel", "scheduled_message_id": 55, "expected_version": "v"}, "invalid_arguments"),
    ],
)
def test_command_parser_retains_strict_field_and_action_boundaries(fields: dict[str, object], code: str) -> None:
    request = _request(text="hello", schedule_at_utc="2033-05-18T03:33:20Z")
    request.update(fields)
    with pytest.raises(ScheduledCommandError) as caught:
        _parse_command(request, 100)
    assert caught.value.code == code


def test_create_is_text_only_scheduled_and_never_clears_draft() -> None:
    command = _parse_command(_request("create", text="hello", schedule_at_utc="2033-05-18T03:33:20Z", topic_id=12), 100)
    request = _build_request(
        command,
        None,
        types.InputPeerChannel(300, 123),
        random_id=9001,
        premium=True,
    )
    assert isinstance(request, SendMessageRequest)
    assert request.message == "hello"
    assert request.schedule_date == datetime.fromtimestamp(2_000_000_000, UTC)
    assert request.clear_draft is None
    assert request.entities == []
    assert isinstance(request.reply_to, types.InputReplyToMessage)
    assert request.reply_to.reply_to_msg_id == 12
    assert request.reply_to.top_msg_id == 12

    with pytest.raises(ScheduledCommandError, match="forum channel"):
        _build_request(
            command,
            None,
            types.InputPeerUser(200, 123),
            random_id=9001,
            premium=True,
        )


def test_rich_requests_preserve_format_intent_and_omit_legacy_entities() -> None:
    create = _parse_command(
        _request("create", text="**hello**", format="rich_markdown", schedule_at_utc="2033-05-18T03:33:20Z"), 100
    )
    request = _build_request(create, None, types.InputPeerUser(200, 123), random_id=9001, premium=True)
    assert isinstance(request, SendMessageRequest)
    assert request.message == "" and request.entities is None
    assert isinstance(request.rich_message, types.InputRichMessageMarkdown)
    assert request.rich_message.markdown == "**hello**"
    plain = _parse_command(_request("create", text="**hello**", schedule_at_utc="2033-05-18T03:33:20Z"), 100)
    assert create.fingerprint() != plain.fingerprint()
    edit = _parse_command(
        _request("update", scheduled_message_id=55, expected_version="v", text="old caption", format="rich_html"), 100
    )
    changed = _build_request(
        edit,
        _row(text="old caption", media_kind=None, media_payload=None),
        types.InputPeerUser(200, 123),
        random_id=None,
        premium=True,
    )
    assert isinstance(changed, EditMessageRequest)
    assert changed.message == "" and changed.entities is None
    assert isinstance(changed.rich_message, types.InputRichMessageHTML)
    with pytest.raises(ScheduledCommandError):
        _parse_command(_request("cancel", scheduled_message_id=55, expected_version="v", format="plain"), 100)

    dependent = _row(
        text="old caption",
        media_kind=None,
        media_payload=None,
        has_rich_message=1,
        rich_message='{"_":"RichMessage","blocks":[{"_":"PageBlockPhoto","photo_id":7}],"photos":[],"documents":[]}',
    )
    with pytest.raises(ScheduledCommandError, match="media dependencies"):
        _build_request(edit, dependent, types.InputPeerUser(200, 123), random_id=None, premium=True)
    unknown = _row(text="old caption", media_kind=None, media_payload=None, has_rich_message=None)
    with pytest.raises(ScheduledCommandError, match="incomplete"):
        _build_request(edit, unknown, types.InputPeerUser(200, 123), random_id=None, premium=True)


def test_update_keeps_media_and_advanced_composition_and_resends_repeat() -> None:
    command = _parse_command(
        _request(
            "update",
            scheduled_message_id=55,
            expected_version="version",
            schedule_at_utc="2033-05-18T04:33:20+00:00",
        ),
        100,
    )
    request = _build_request(
        command,
        _row(),
        types.InputPeerUser(200, 123),
        random_id=None,
        premium=True,
    )
    assert isinstance(request, EditMessageRequest)
    assert request.id == 55
    assert request.message is None
    assert request.entities is None and request.rich_message is None
    assert request.media is None
    assert request.reply_markup is None
    assert request.schedule_repeat_period == 604_800
    assert request.schedule_date == datetime.fromtimestamp(2_000_003_600, UTC)

    legacy_unknown = _row(has_rich_message=None, invert_media=None, video_processing_pending=None)
    legacy_date_edit = _parse_command(
        _request(
            "update",
            scheduled_message_id=55,
            expected_version="version",
            schedule_at_utc="2033-05-18T04:33:20+00:00",
        ),
        100,
    )
    preserved = _build_request(
        legacy_date_edit,
        legacy_unknown,
        types.InputPeerUser(200, 123),
        random_id=None,
        premium=True,
    )
    assert isinstance(preserved, EditMessageRequest) and preserved.message is None and preserved.media is None


def test_text_change_conflicts_with_formatting_unless_explicitly_dropped() -> None:
    formatted = _row(
        formatting_entities='[{"_":"MessageEntityBold","offset":0,"length":3}]',
    )
    command = _parse_command(
        _request("update", scheduled_message_id=55, expected_version="version", text="new caption"), 100
    )
    with pytest.raises(ScheduledCommandError, match="formatting"):
        _build_request(command, formatted, types.InputPeerUser(200, 123), random_id=None, premium=True)
    dropped = _parse_command(
        _request(
            "update",
            scheduled_message_id=55,
            expected_version="version",
            text="new caption",
            drop_formatting=True,
        ),
        100,
    )
    request = _build_request(dropped, formatted, types.InputPeerUser(200, 123), random_id=None, premium=True)
    assert isinstance(request, EditMessageRequest)
    assert request.message == "new caption"
    assert request.entities == []
    assert request.media is None


def test_text_replacement_requires_known_safe_composition_and_suppresses_new_preview() -> None:
    command = _parse_command(
        _request("update", scheduled_message_id=55, expected_version="version", text="new text"), 100
    )
    unknown_rich = _row(has_rich_message=None)
    with pytest.raises(ScheduledCommandError, match="composition"):
        _build_request(command, unknown_rich, types.InputPeerUser(200, 123), random_id=None, premium=True)
    preview = _row(media_kind="link_preview", media_payload='{"url":"https://example.test"}')
    with pytest.raises(ScheduledCommandError, match="link preview"):
        _build_request(command, preview, types.InputPeerUser(200, 123), random_id=None, premium=True)
    plain = _row(media_kind=None, media_payload=None, invert_media=None)
    request = _build_request(command, plain, types.InputPeerUser(200, 123), random_id=None, premium=True)
    assert isinstance(request, EditMessageRequest)
    assert request.no_webpage is True
    assert request.media is None
    caption = _row(invert_media=1)
    caption_request = _build_request(command, caption, types.InputPeerUser(200, 123), random_id=None, premium=True)
    assert isinstance(caption_request, EditMessageRequest) and caption_request.invert_media is True

    pending_video = _row(
        media_kind="video",
        media_payload='{"round_message":false}',
        video_processing_pending=1,
    )
    with pytest.raises(ScheduledCommandError, match="processing"):
        _build_request(command, pending_video, types.InputPeerUser(200, 123), random_id=None, premium=True)


def test_repeat_omitted_preserves_zero_disables_and_positive_requires_premium() -> None:
    omitted = _parse_command(_request("update", scheduled_message_id=55, expected_version="v", text="old caption"), 100)
    preserved = _build_request(omitted, _row(), types.InputPeerUser(200, 123), random_id=None, premium=True)
    assert preserved is None

    disabled = _parse_command(
        _request("update", scheduled_message_id=55, expected_version="v", repeat_period_seconds=0), 100
    )
    request = _build_request(disabled, _row(), types.InputPeerUser(200, 123), random_id=None, premium=True)
    assert isinstance(request, EditMessageRequest)
    assert request.schedule_repeat_period is None
    assert request.schedule_date == datetime.fromtimestamp(2_000_000_000, UTC)

    already_disabled = _parse_command(
        _request("update", scheduled_message_id=55, expected_version="v", repeat_period_seconds=0), 100
    )
    assert (
        _build_request(
            already_disabled,
            _row(schedule_repeat_period=None),
            types.InputPeerUser(200, 123),
            random_id=None,
            premium=True,
        )
        is None
    )

    repeating = _parse_command(
        _request("update", scheduled_message_id=55, expected_version="v", repeat_period_seconds=86_400), 100
    )
    with pytest.raises(ScheduledCommandError, match="Premium"):
        _build_request(
            repeating, _row(schedule_repeat_period=None), types.InputPeerUser(200, 123), random_id=None, premium=False
        )


def test_repeating_native_album_is_refused() -> None:
    command = _parse_command(
        _request("update", scheduled_message_id=55, expected_version="v", repeat_period_seconds=86_400), 100
    )
    with pytest.raises(ScheduledCommandError, match="albums"):
        _build_request(
            command,
            _row(schedule_repeat_period=None, grouped_id=123),
            types.InputPeerUser(200, 123),
            random_id=None,
            premium=True,
        )


def test_native_repeat_allowlist_can_be_used_on_create_and_requires_premium() -> None:
    assert {86_400, 604_800, 1_209_600, 2_592_000, 7_862_400, 15_724_800, 31_536_000} == SCHEDULED_REPEAT_PERIODS
    command = _parse_command(
        _request(
            "create",
            text="repeat",
            schedule_at_utc="2033-05-18T03:33:20Z",
            repeat_period_seconds=1_209_600,
        ),
        100,
    )
    request = _build_request(command, None, types.InputPeerUser(200, 123), random_id=9001, premium=True)
    assert isinstance(request, SendMessageRequest) and request.schedule_repeat_period == 1_209_600
    with pytest.raises(ScheduledCommandError, match="Premium"):
        _build_request(command, None, types.InputPeerUser(200, 123), random_id=9001, premium=False)

    date_edit = _parse_command(
        _request(
            "update",
            scheduled_message_id=55,
            expected_version="v",
            schedule_at_utc="2033-05-18T04:33:20Z",
        ),
        100,
    )
    with pytest.raises(ScheduledCommandError, match="repeat setting"):
        _build_request(
            date_edit,
            _row(schedule_repeat_period=1234),
            types.InputPeerUser(200, 123),
            random_id=None,
            premium=True,
        )


def test_cancel_is_a_queue_delete_and_not_a_sent_message_delete() -> None:
    command = _parse_command(_request("cancel", scheduled_message_id=77, expected_version="v"), 100)
    update = types.UpdateDeleteScheduledMessages(types.PeerUser(200), [77])
    unrelated_sent = types.UpdateNewMessage(
        types.Message(id=77, peer_id=types.PeerUser(200), message="published", date=datetime.now(UTC)),
        pts=1,
        pts_count=1,
    )
    assert (
        _correlated_response(command, type("Response", (), {"updates": [unrelated_sent]})(), random_id=None)[2] is None
    )
    request = _build_request(command, _row(), types.InputPeerUser(200, 123), random_id=None, premium=True)
    assert isinstance(request, DeleteScheduledMessagesRequest)
    assert request.id == [77]
    assert _correlated_response(command, type("Response", (), {"updates": [update]})(), random_id=None)[2] is update


def test_saved_messages_peer_is_bound_to_the_authenticated_account() -> None:
    from mcp_telegram.scheduled_commands import _peer_matches

    assert _peer_matches(types.InputPeerSelf(), 100, 100)
    assert not _peer_matches(types.InputPeerSelf(), 200, 100)


def test_creation_requires_random_id_and_matching_scheduled_update() -> None:
    command = _parse_command(_request("create", text="new", schedule_at_utc="2033-05-18T03:33:20Z"), 100)
    sent_id_collision = types.UpdateNewMessage(
        types.Message(id=88, peer_id=types.PeerUser(200), message="new", date=datetime.now(UTC)),
        pts=1,
        pts_count=1,
    )
    mapped = types.UpdateMessageID(id=88, random_id=9001)
    wrong_queue = types.UpdateNewScheduledMessage(
        types.Message(id=89, peer_id=types.PeerUser(200), message="new", date=datetime.now(UTC))
    )
    response = type("Response", (), {"updates": [mapped, sent_id_collision, wrong_queue]})()
    _, scheduled_id, evidence = _correlated_response(command, response, random_id=9001)
    assert scheduled_id is None
    assert evidence is None


def test_update_short_scheduled_evidence_is_correlated_but_sent_short_is_not() -> None:
    command = _parse_command(_request("update", scheduled_message_id=55, expected_version="v", text="edited"), 100)
    message = types.Message(id=55, peer_id=types.PeerUser(200), message="edited", date=datetime.now(UTC))
    scheduled_update = types.UpdateNewScheduledMessage(message)
    response = types.UpdateShort(update=scheduled_update, date=datetime.now(UTC))
    assert _correlated_response(command, response, random_id=None) == (
        "acknowledged",
        55,
        message,
    )

    sent_short = types.UpdateShortSentMessage(id=55, pts=1, pts_count=1, date=datetime.now(UTC))
    assert _correlated_response(command, sent_short, random_id=None) == ("acknowledged", 55, None)


def test_receipt_replay_conflict_and_interrupted_pending_never_resend(tmp_path: Path) -> None:
    path = tmp_path / "receipts.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_scheduled_receipt_schema(conn)
    command = _parse_command(_request("create", text="hello", schedule_at_utc="2033-05-18T03:33:20Z"), 100)
    with write_transaction(conn):
        first = prepare_scheduled_operation(conn, command, 9001, now=10)
    conn.close()

    reopened = sqlite3.connect(path)
    reopened.row_factory = sqlite3.Row
    assert read_scheduled_operation(reopened, 100, "operation-one") == first
    with pytest.raises(ScheduledOperationConflictError):
        with write_transaction(reopened):
            prepare_scheduled_operation(
                reopened,
                _parse_command(_request("create", text="other", schedule_at_utc="2033-05-18T03:33:20Z"), 100),
                9002,
            )
    with write_transaction(reopened):
        assert settle_interrupted_scheduled_operations(reopened, 100, now=11) == 1
    receipt = read_scheduled_operation(reopened, 100, "operation-one")
    assert receipt is not None and receipt.outcome == "unknown" and receipt.random_id == 9001
    with write_transaction(reopened):
        assert prepare_scheduled_operation(reopened, command, 9999, now=12) == receipt
    audit_row = cast(
        sqlite3.Row,
        reopened.execute(
            "SELECT created_at,updated_at FROM scheduled_operation_receipts WHERE account_id=? AND operation_key=?",
            (100, "operation-one"),
        ).fetchone(),
    )
    assert tuple(audit_row) == (10, 11)
    reopened.close()


class _OwnerHarness:
    def __init__(self, path: Path, *, schedule_at: int) -> None:
        ensure_sync_schema(path)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.dialog_id = 200
        self.account_id = 100
        self.schedule_at = schedule_at
        self.fence = ScheduledMutationFence()
        ensure_scheduled_receipt_schema(self.conn)
        with write_transaction(self.conn):
            self.conn.execute("UPDATE scheduled_sync_state SET account_id=? WHERE key='account'", (self.account_id,))
            order = allocate_observation_order(self.conn)
            self.conn.execute(
                "INSERT INTO scheduled_reconciliation_state(dialog_id,updated_at,snapshot_observation_order,"
                "baseline_observed_at,coverage_state) VALUES(?,?,?,?,'complete')",
                (self.dialog_id, schedule_at - 100, order, schedule_at - 100),
            )
        self.client = _OwnerClient(self)
        self.account_is_premium = True
        self.fail_recovery = False
        self.first_use_requests: list[tuple[int, int]] = []
        self.first_use_accept = False
        self.owner = self.new_owner(self.client)

    def new_owner(self, client: object) -> ScheduledCommandOwner:
        return ScheduledCommandOwner(
            client,
            self.conn,
            writer=inline_writer(self.conn),
            account_id=self.account_id,
            fence=self.fence,
            resolve_peer=self.resolve_peer,
            account_is_premium=lambda: self.account_is_premium,
            request_recovery=self.recover,
            prioritize_baseline=self.first_use_repair,
        )

    async def first_use_repair(self, _account_id: int, _dialog_id: int) -> bool:
        self.first_use_requests.append((_account_id, _dialog_id))
        return self.first_use_accept

    async def resolve_peer(self, dialog_id: int, topic_id: int | None) -> object:
        assert dialog_id == self.dialog_id
        assert topic_id is None
        return types.InputPeerUser(dialog_id, 123)

    async def recover(self, _dialog_id: int, _reason: str) -> None:
        if self.fail_recovery:
            raise RuntimeError("recovery sink unavailable")

    def close(self) -> None:
        self.conn.close()


class _OwnerClient:
    def __init__(self, harness: _OwnerHarness) -> None:
        self.harness = harness
        self.requests: list[object] = []
        self.fail_after_dispatch = False
        self.stale_after_validation = False
        self.invalidate_coverage_before_revalidate = False
        self.dispatched_count = 0
        self.event_before_response = False
        self.response: object | None = None

    async def __call__(self, request: object) -> object:
        self.requests.append(request)
        check = current_scheduled_mutation_check()
        assert check is not None
        if self.invalidate_coverage_before_revalidate:
            with write_transaction(self.harness.conn):
                self.harness.conn.execute(
                    "UPDATE scheduled_reconciliation_state SET coverage_state='unknown' WHERE dialog_id=?",
                    (self.harness.dialog_id,),
                )
        token = await check.revalidate()
        if self.stale_after_validation:
            token.invalidated.set()
            raise ScheduledWriteRejected("scheduled_version_changed")
        budget = current_rpc_scope().attempt_budget
        assert budget is not None
        budget.debit()
        self.dispatched_count += 1
        if self.fail_after_dispatch:
            raise TimeoutError("acknowledgment lost")
        if self.response is not None:
            return self.response
        queue_id = cast(int, getattr(request, "id", 456))
        if self.event_before_response:
            changed = types.Message(
                id=queue_id,
                peer_id=types.PeerUser(self.harness.dialog_id),
                date=datetime.fromtimestamp(self.harness.schedule_at, UTC),
                message="newer event wins",
                from_scheduled=True,
            )
            with write_transaction(self.harness.conn):
                self.harness.fence.invalidate_message(self.harness.account_id, self.harness.dialog_id, queue_id)
                order = allocate_observation_order(self.harness.conn)
                upsert_scheduled_message(
                    self.harness.conn,
                    self.harness.dialog_id,
                    changed,
                    now=self.harness.schedule_at,
                    observation_order=order,
                )
        scheduled_message = types.Message(
            id=queue_id,
            peer_id=types.PeerUser(self.harness.dialog_id),
            date=datetime.fromtimestamp(self.harness.schedule_at, UTC),
            message=cast(str, getattr(request, "message", "created")),
            from_scheduled=True,
        )
        updates: list[object] = []
        if isinstance(request, SendMessageRequest):
            updates.append(types.UpdateMessageID(id=queue_id, random_id=request.random_id))
        updates.append(types.UpdateNewScheduledMessage(scheduled_message))
        return SimpleNamespace(updates=updates)


def _owner_create_request(harness: _OwnerHarness, key: str = "create") -> dict[str, object]:
    return {
        "action": "create",
        "account_id": harness.account_id,
        "dialog_id": harness.dialog_id,
        "operation_key": key,
        "text": "created from command",
        "schedule_at_utc": datetime.fromtimestamp(harness.schedule_at, UTC).isoformat().replace("+00:00", "Z"),
    }


@pytest.mark.asyncio
async def test_owner_creation_applies_only_correlated_scheduled_evidence_and_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _OwnerHarness(tmp_path / "sync.db", schedule_at=int(datetime.now(UTC).timestamp()) + 900)
    try:
        request = _owner_create_request(harness)
        first = await harness.owner.execute(request)
        assert first["outcome"] == "acknowledged" and first["current_is_operation_confirmation"] is True
        assert first["scheduled_message_id"] == 456
        row = cast(
            sqlite3.Row,
            harness.conn.execute(
                "SELECT message_state,text FROM scheduled_messages WHERE dialog_id=? AND message_id=?",
                (harness.dialog_id, 456),
            ).fetchone(),
        )
        assert row["message_state"] == "scheduled" and row["text"] == "created from command"
        retry_client = _OwnerClient(harness)
        harness.owner = harness.new_owner(retry_client)
        monkeypatch.setattr("mcp_telegram.scheduled_commands.time.time", lambda: harness.schedule_at + 3600)
        assert await harness.owner.execute(request) == first
        assert len(harness.client.requests) == 1 and retry_client.requests == []
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_rich_create_returns_observed_canonical_snapshot_and_replays(tmp_path: Path) -> None:
    harness = _OwnerHarness(tmp_path / "sync.db", schedule_at=int(datetime.now(UTC).timestamp()) + 900)

    class RichClient:
        async def __call__(self, request: object) -> object:
            check = current_scheduled_mutation_check()
            assert check is not None
            await check.revalidate()
            budget = current_rpc_scope().attempt_budget
            assert budget is not None
            budget.debit()
            message = types.Message(
                id=456,
                peer_id=types.PeerUser(200),
                date=datetime.fromtimestamp(harness.schedule_at, UTC),
                message="",
                from_scheduled=True,
                rich_message=types.RichMessage(blocks=[], photos=[], documents=[]),
            )
            return SimpleNamespace(
                updates=[
                    types.UpdateMessageID(id=456, random_id=cast(SendMessageRequest, request).random_id),
                    types.UpdateNewScheduledMessage(message),
                ]
            )

    try:
        harness.owner = harness.new_owner(RichClient())
        request = _owner_create_request(harness, "rich-create") | {"format": "rich_markdown", "text": "**hello**"}
        result = await harness.owner.execute(request)
        assert result["outcome"] == "acknowledged"
        assert result["verification"] == {"status": "observed", "basis": "post_write_snapshot"}
        assert result["current_is_operation_confirmation"] is False
        assert cast(dict[str, object], result["current"])["rich_message_status"] == "complete"
        assert await harness.owner.execute(request) == result
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_rich_create_with_mapping_uses_postflight_snapshot(tmp_path: Path) -> None:
    harness = _OwnerHarness(tmp_path / "sync.db", schedule_at=int(datetime.now(UTC).timestamp()) + 900)

    class MappingClient:
        async def __call__(self, request: object) -> object:
            check = current_scheduled_mutation_check()
            assert check is not None
            await check.revalidate()
            budget = current_rpc_scope().attempt_budget
            assert budget is not None
            budget.debit()
            return SimpleNamespace(
                updates=[types.UpdateMessageID(id=456, random_id=cast(SendMessageRequest, request).random_id)]
            )

    async def postflight(dialog_id: int) -> bool:
        assert dialog_id == 200
        message = types.Message(
            id=456,
            peer_id=types.PeerUser(200),
            date=datetime.fromtimestamp(harness.schedule_at, UTC),
            message="",
            from_scheduled=True,
            rich_message=types.RichMessage(blocks=[], photos=[], documents=[]),
        )
        with write_transaction(harness.conn):
            upsert_scheduled_message(harness.conn, dialog_id, message, now=harness.schedule_at)
        return True

    try:
        harness.owner = harness.new_owner(MappingClient())
        harness.owner._postflight = postflight
        request = _owner_create_request(harness, "mapped-rich") | {"format": "rich_html", "text": "<b>hello</b>"}
        result = await harness.owner.execute(request)
        assert result["scheduled_message_id"] == 456
        assert result["verification"] == {"status": "observed", "basis": "post_write_snapshot"}
        assert await harness.owner.execute(request) == result
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_large_observation_keeps_acknowledgement_and_safe_replay(tmp_path: Path) -> None:
    harness = _OwnerHarness(tmp_path / "sync.db", schedule_at=int(datetime.now(UTC).timestamp()) + 900)

    class LargeClient:
        async def __call__(self, request: object) -> object:
            check = current_scheduled_mutation_check()
            assert check is not None
            await check.revalidate()
            budget = current_rpc_scope().attempt_budget
            assert budget is not None
            budget.debit()
            message = types.Message(
                id=456,
                peer_id=types.PeerUser(200),
                date=datetime.fromtimestamp(harness.schedule_at, UTC),
                message="",
                rich_message=types.RichMessage([types.PageBlockParagraph(types.TextPlain("x" * 20000))], [], []),
            )
            return SimpleNamespace(
                updates=[
                    types.UpdateMessageID(id=456, random_id=cast(SendMessageRequest, request).random_id),
                    types.UpdateNewScheduledMessage(message),
                ]
            )

    try:
        harness.owner = harness.new_owner(LargeClient())
        request = _owner_create_request(harness, "large-rich") | {"format": "rich_html", "text": "hello"}
        result = await harness.owner.execute(request)
        assert result["acknowledged"] is True and result["receipt_persisted"] is True
        assert "current" not in result
        assert isinstance(result["current_version"], str)
        assert result["verification"] == {"status": "unverified", "basis": "none", "reason": "snapshot_too_large"}
        assert (
            len(
                cast(
                    str,
                    harness.conn.execute("SELECT rich_message FROM scheduled_messages WHERE message_id=456").fetchone()[
                        0
                    ],
                )
            )
            > 20000
        )
        assert await harness.owner.execute(request) == result
    finally:
        harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("partial_kind", ["flag", "unsupported_block"])
async def test_partial_rich_ack_retains_ack_when_postflight_fails(tmp_path: Path, partial_kind: str) -> None:
    harness = _OwnerHarness(tmp_path / "sync.db", schedule_at=int(datetime.now(UTC).timestamp()) + 900)

    class PartialClient:
        async def __call__(self, request: object) -> object:
            check = current_scheduled_mutation_check()
            assert check is not None
            await check.revalidate()
            budget = current_rpc_scope().attempt_budget
            assert budget is not None
            budget.debit()
            rich = (
                types.RichMessage([], [], [], part=True)
                if partial_kind == "flag"
                else types.RichMessage([types.PageBlockUnsupported()], [], [])
            )
            message = types.Message(
                id=456,
                peer_id=types.PeerUser(200),
                date=datetime.fromtimestamp(harness.schedule_at, UTC),
                message="",
                rich_message=rich,
            )
            return SimpleNamespace(
                updates=[
                    types.UpdateMessageID(id=456, random_id=cast(SendMessageRequest, request).random_id),
                    types.UpdateNewScheduledMessage(message),
                ]
            )

    async def unavailable(_dialog_id: int) -> bool:
        raise TimeoutError

    try:
        harness.owner = harness.new_owner(PartialClient())
        harness.owner._postflight = unavailable
        request = _owner_create_request(harness, "partial-rich") | {"format": "rich_markdown", "text": "hello"}
        result = await harness.owner.execute(request)
        assert result["acknowledged"] is True and result["receipt_persisted"] is True
        assert cast(dict[str, object], result["verification"])["status"] == "unverified"
        assert harness.conn.execute("SELECT 1 FROM scheduled_messages WHERE message_id=456").fetchone() is None
        assert await harness.owner.execute(request) == result
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_cancelled_postflight_keeps_acknowledged_receipt(tmp_path: Path) -> None:
    harness = _OwnerHarness(tmp_path / "sync.db", schedule_at=int(datetime.now(UTC).timestamp()) + 900)
    started = asyncio.Event()

    async def waiting(_dialog_id: int) -> bool:
        started.set()
        await asyncio.Event().wait()
        return False

    try:
        harness.client.response = SimpleNamespace(updates=[])
        harness.owner._postflight = waiting
        # Update has a stable selected ID even when Telegram returns no full update.
        with write_transaction(harness.conn):
            message = types.Message(
                id=55, peer_id=types.PeerUser(200), date=datetime.fromtimestamp(harness.schedule_at, UTC), message="old"
            )
            upsert_scheduled_message(harness.conn, 200, message)
        order = cast(
            int,
            harness.conn.execute("SELECT observation_order FROM scheduled_messages WHERE message_id=55").fetchone()[0],
        )
        request = _request(
            "update",
            operation_key="interrupted-postflight",
            scheduled_message_id=55,
            expected_version=scheduled_message_version(100, 200, 55, order),
            text="new",
        )
        task = asyncio.create_task(harness.owner.execute(request))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        receipt = read_scheduled_operation(harness.conn, 100, "interrupted-postflight")
        assert receipt is not None and receipt.outcome == "acknowledged"
        replay = await harness.owner.execute(request)
        assert (
            replay["acknowledged"] is True and cast(dict[str, object], replay["verification"])["status"] == "unverified"
        )
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_time_only_update_retains_full_rich_tree_and_media(tmp_path: Path) -> None:
    harness = _OwnerHarness(tmp_path / "sync.db", schedule_at=int(datetime.now(UTC).timestamp()) + 900)
    rich = types.RichMessage([types.PageBlockParagraph(types.TextPlain("original body"))], [], [])
    media = types.MessageMediaPhoto(photo=types.PhotoEmpty(123))

    def message(at: int) -> types.Message:
        return types.Message(
            id=55,
            peer_id=types.PeerUser(200),
            date=datetime.fromtimestamp(at, UTC),
            message="",
            rich_message=rich,
            media=media,
        )

    try:
        with write_transaction(harness.conn):
            upsert_scheduled_message(harness.conn, 200, message(harness.schedule_at))
        order = cast(
            int,
            harness.conn.execute("SELECT observation_order FROM scheduled_messages WHERE message_id=55").fetchone()[0],
        )
        target = harness.schedule_at + 300
        harness.client.response = SimpleNamespace(updates=[types.UpdateNewScheduledMessage(message(target))])
        request = _request(
            "update",
            operation_key="time-rich",
            scheduled_message_id=55,
            expected_version=scheduled_message_version(100, 200, 55, order),
            schedule_at_utc=datetime.fromtimestamp(target, UTC).isoformat(),
        )
        result = await harness.owner.execute(request)
        dispatched = cast(EditMessageRequest, harness.client.requests[0])
        assert dispatched.message is None and dispatched.entities is None and dispatched.rich_message is None
        assert dispatched.media is None
        assert result["verification"] == {"status": "verified", "basis": "post_write_snapshot"}
        current = cast(dict[str, object], result["current"])
        assert current["rich_message_status"] == "complete" and current["media_kind"] == "photo"
        assert current["scheduled_at"] == target and current["expected_version"] != request["expected_version"]
        assert await harness.owner.execute(request) == result
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_rich_edit_observes_canonical_body_and_checks_schedule(tmp_path: Path) -> None:
    harness = _OwnerHarness(tmp_path / "sync.db", schedule_at=int(datetime.now(UTC).timestamp()) + 900)
    try:
        old = types.Message(
            id=55, peer_id=types.PeerUser(200), date=datetime.fromtimestamp(harness.schedule_at, UTC), message="hello"
        )
        with write_transaction(harness.conn):
            upsert_scheduled_message(harness.conn, 200, old)
        order = cast(
            int,
            harness.conn.execute("SELECT observation_order FROM scheduled_messages WHERE message_id=55").fetchone()[0],
        )
        target = harness.schedule_at + 300
        canonical = types.Message(
            id=55,
            peer_id=types.PeerUser(200),
            date=datetime.fromtimestamp(target, UTC),
            message="",
            rich_message=types.RichMessage([types.PageBlockParagraph(types.TextPlain("hello"))], [], []),
        )
        harness.client.response = SimpleNamespace(updates=[types.UpdateNewScheduledMessage(canonical)])
        request = _request(
            "update",
            operation_key="rich-edit",
            scheduled_message_id=55,
            expected_version=scheduled_message_version(100, 200, 55, order),
            text="hello",
            format="rich_html",
            schedule_at_utc=datetime.fromtimestamp(target, UTC).isoformat(),
        )
        result = await harness.owner.execute(request)
        assert isinstance(cast(EditMessageRequest, harness.client.requests[0]).rich_message, types.InputRichMessageHTML)
        assert result["verification"] == {"status": "observed", "basis": "post_write_snapshot"}
        assert result["current_is_operation_confirmation"] is False
        assert cast(dict[str, object], result["current"])["scheduled_at"] == target
        assert await harness.owner.execute(request) == result
        second = request | {
            "operation_key": "rich-schedule-conflict",
            "expected_version": cast(dict[str, object], result["current"])["expected_version"],
            "schedule_at_utc": datetime.fromtimestamp(target + 300, UTC).isoformat(),
        }
        conflict = await harness.owner.execute(second)
        assert conflict["verification"] == {"status": "conflict", "basis": "post_write_snapshot"}
        assert conflict["current_is_operation_confirmation"] is False
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_owner_lost_ack_stays_unknown_after_new_owner_without_resending(tmp_path: Path) -> None:
    harness = _OwnerHarness(tmp_path / "sync.db", schedule_at=int(datetime.now(UTC).timestamp()) + 900)
    try:
        harness.client.fail_after_dispatch = True
        harness.fail_recovery = True
        request = _owner_create_request(harness, "lost-ack")
        first = await harness.owner.execute(request)
        assert first["outcome"] == "unknown" and first["mutation_dispatched"] is True
        retry_client = _OwnerClient(harness)
        harness.owner = harness.new_owner(retry_client)
        assert await harness.owner.execute(request) == first
        assert len(harness.client.requests) == 1 and retry_client.requests == []
        receipt = read_scheduled_operation(harness.conn, 100, "lost-ack")
        assert receipt is not None and receipt.outcome == "unknown"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_owner_rejects_stale_version_and_invalidated_token_without_a_mutation(tmp_path: Path) -> None:
    schedule_at = int(datetime.now(UTC).timestamp()) + 900
    harness = _OwnerHarness(tmp_path / "sync.db", schedule_at=schedule_at)
    try:
        with write_transaction(harness.conn):
            order = allocate_observation_order(harness.conn)
            harness.conn.execute(
                "INSERT INTO scheduled_messages(dialog_id,message_id,scheduled_at,text,formatting_entities,"
                "schedule_repeat_period,has_rich_message,invert_media,video_processing_pending,"
                "first_seen_at,updated_at,observation_order) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (200, 55, schedule_at, "old", "[]", 604_800, 0, 0, 0, schedule_at - 500, schedule_at - 500, order),
            )
        current_version = scheduled_message_version(100, 200, 55, order)
        stale = await harness.owner.execute(
            {
                "action": "update",
                "account_id": 100,
                "dialog_id": 200,
                "scheduled_message_id": 55,
                "expected_version": "stale-version",
                "operation_key": "stale",
                "text": "new",
            }
        )
        assert stale["error"] == "version_conflict" and harness.client.requests == []

        harness.client.stale_after_validation = True
        rejected = await harness.owner.execute(
            {
                "action": "update",
                "account_id": 100,
                "dialog_id": 200,
                "scheduled_message_id": 55,
                "expected_version": current_version,
                "operation_key": "invalidated",
                "text": "new",
            }
        )
        assert rejected["outcome"] == "rejected" and rejected["mutation_dispatched"] is False
        assert len(harness.client.requests) == 1
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_newly_unknown_coverage_is_a_typed_pre_dispatch_rejection(tmp_path: Path) -> None:
    schedule_at = int(datetime.now(UTC).timestamp()) + 900
    harness = _OwnerHarness(tmp_path / "sync.db", schedule_at=schedule_at)
    try:
        harness.client.invalidate_coverage_before_revalidate = True
        result = await harness.owner.execute(_owner_create_request(harness, "coverage-invalidated"))
        assert result["error"] == "scheduled_state_unknown"
        assert result["outcome"] == "rejected"
        assert result["mutation_dispatched"] is False
        assert harness.client.requests and harness.client.dispatched_count == 0
        receipt = read_scheduled_operation(harness.conn, harness.account_id, "coverage-invalidated")
        assert receipt is not None and receipt.outcome == "rejected"
    finally:
        harness.close()


async def test_missing_baseline_requests_repair_before_any_receipt_or_write(tmp_path: Path) -> None:
    harness = _OwnerHarness(tmp_path / "sync.db", schedule_at=int(datetime.now(UTC).timestamp()) + 900)
    try:
        with write_transaction(harness.conn):
            harness.conn.execute("DELETE FROM scheduled_reconciliation_state WHERE dialog_id=?", (harness.dialog_id,))
        harness.first_use_accept = True
        request = _owner_create_request(harness, "first-use")
        first, second = await asyncio.gather(harness.owner.execute(request), harness.owner.execute(request))
        for result in (first, second):
            assert result["error"] == "scheduled_state_unknown"
            assert result["operation_key"] == "first-use"
            assert result["mutation_dispatched"] is False and result["receipt_persisted"] is False
        projected = _scheduled_error_response(first)
        assert projected is not None and projected.is_error and projected.structured_content is not None
        assert projected.structured_content["details"] == {
            "operation_key": "first-use",
            "mutation_dispatched": False,
            "receipt_persisted": False,
        }
        assert harness.first_use_requests == [(100, 200), (100, 200)]
        assert harness.client.requests == []
        assert read_scheduled_operation(harness.conn, 100, "first-use") is None
        with write_transaction(harness.conn):
            order = allocate_observation_order(harness.conn)
            harness.conn.execute(
                "INSERT INTO scheduled_reconciliation_state(dialog_id,updated_at,snapshot_observation_order,coverage_state) "
                "VALUES(?,?,?,'complete')",
                (200, harness.schedule_at - 100, order),
            )
        completed = await harness.owner.execute(request)
        assert completed["outcome"] == "acknowledged" and completed["mutation_dispatched"] is True
        assert await harness.owner.execute(request) == completed
        assert harness.client.dispatched_count == 1
    finally:
        harness.close()


@pytest.mark.parametrize("state", ["inaccessible", "unsupported"])
async def test_terminal_baseline_does_not_request_first_use_repair(tmp_path: Path, state: str) -> None:
    harness = _OwnerHarness(tmp_path / "sync.db", schedule_at=int(datetime.now(UTC).timestamp()) + 900)
    try:
        with write_transaction(harness.conn):
            harness.conn.execute("UPDATE scheduled_reconciliation_state SET coverage_state=?", (state,))
        harness.first_use_accept = True
        result = await harness.owner.execute(_owner_create_request(harness, "terminal"))
        assert result["error"] == "scheduled_state_unavailable"
        assert harness.first_use_requests == [] and harness.client.requests == []
        assert read_scheduled_operation(harness.conn, 100, "terminal") is None
    finally:
        harness.close()


@pytest.mark.parametrize("account", [None, 999])
async def test_unbound_or_different_account_cannot_prioritize_baseline(tmp_path: Path, account: int | None) -> None:
    harness = _OwnerHarness(tmp_path / "sync.db", schedule_at=int(datetime.now(UTC).timestamp()) + 900)
    try:
        with write_transaction(harness.conn):
            harness.conn.execute("UPDATE scheduled_sync_state SET account_id=? WHERE key='account'", (account,))
        harness.first_use_accept = True
        result = await harness.owner.execute(_owner_create_request(harness, "unbound"))
        assert result["error"] == "account_state_unknown"
        assert harness.first_use_requests == [] and harness.client.requests == []
        assert read_scheduled_operation(harness.conn, 100, "unbound") is None
    finally:
        harness.close()


async def test_failed_baseline_priority_does_not_claim_it_was_scheduled(tmp_path: Path) -> None:
    harness = _OwnerHarness(tmp_path / "sync.db", schedule_at=int(datetime.now(UTC).timestamp()) + 900)
    try:
        with write_transaction(harness.conn):
            harness.conn.execute("DELETE FROM scheduled_reconciliation_state WHERE dialog_id=200")

        async def fail_priority(_account_id: int, _dialog_id: int) -> bool:
            raise sqlite3.OperationalError("injected storage failure")

        harness.owner._prioritize_baseline = fail_priority
        result = await harness.owner.execute(_owner_create_request(harness, "priority-failure"))
        assert result["error"] == "preflight_unavailable"
        assert harness.client.requests == []
        assert read_scheduled_operation(harness.conn, 100, "priority-failure") is None
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_current_premium_profile_is_checked_when_preparing_the_request(tmp_path: Path) -> None:
    harness = _OwnerHarness(tmp_path / "sync.db", schedule_at=int(datetime.now(UTC).timestamp()) + 900)
    try:
        harness.account_is_premium = False
        request = _owner_create_request(harness, "profile-drift")
        request["repeat_period_seconds"] = 86_400
        result = await harness.owner.execute(request)
        assert result["error"] == "premium_required"
        assert harness.client.requests == []
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_older_rpc_response_cannot_overwrite_newer_event(tmp_path: Path) -> None:
    schedule_at = int(datetime.now(UTC).timestamp()) + 900
    harness = _OwnerHarness(tmp_path / "sync.db", schedule_at=schedule_at)
    try:
        with write_transaction(harness.conn):
            order = allocate_observation_order(harness.conn)
            harness.conn.execute(
                "INSERT INTO scheduled_messages(dialog_id,message_id,scheduled_at,text,formatting_entities,"
                "has_rich_message,invert_media,video_processing_pending,first_seen_at,updated_at,observation_order) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (200, 55, schedule_at, "old", "[]", 0, 0, 0, schedule_at - 500, schedule_at - 500, order),
            )
        version = scheduled_message_version(100, 200, 55, order)
        harness.client.event_before_response = True
        result = await harness.owner.execute(
            {
                "action": "update",
                "account_id": 100,
                "dialog_id": 200,
                "scheduled_message_id": 55,
                "expected_version": version,
                "operation_key": "event-race",
                "text": "command response",
            }
        )
        current = cast(
            sqlite3.Row,
            harness.conn.execute(
                "SELECT text,observation_order FROM scheduled_messages WHERE dialog_id=? AND message_id=?",
                (200, 55),
            ).fetchone(),
        )
        assert result["outcome"] == "acknowledged" and result["current_is_operation_confirmation"] is False
        assert current["text"] == "newer event wins"
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_cancel_keeps_the_matching_sent_id_for_the_selected_queue_id(tmp_path: Path) -> None:
    schedule_at = int(datetime.now(UTC).timestamp()) + 900
    harness = _OwnerHarness(tmp_path / "sync.db", schedule_at=schedule_at)
    try:
        with write_transaction(harness.conn):
            order = allocate_observation_order(harness.conn)
            harness.conn.execute(
                "INSERT INTO scheduled_messages(dialog_id,message_id,scheduled_at,text,formatting_entities,"
                "has_rich_message,invert_media,video_processing_pending,first_seen_at,updated_at,observation_order) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (200, 55, schedule_at, "old", "[]", 0, 0, 0, schedule_at - 500, schedule_at - 500, order),
            )
        version = scheduled_message_version(100, 200, 55, order)
        update = types.UpdateDeleteScheduledMessages(
            types.PeerUser(200),
            [44, 55],
            sent_messages=[144, 155],
        )
        harness.client.response = SimpleNamespace(updates=[update])
        result = await harness.owner.execute(
            {
                "action": "cancel",
                "account_id": 100,
                "dialog_id": 200,
                "scheduled_message_id": 55,
                "expected_version": version,
                "operation_key": "cancel-second",
            }
        )
        current = cast(
            sqlite3.Row,
            harness.conn.execute(
                "SELECT message_state,publication_hint_message_id FROM scheduled_messages "
                "WHERE dialog_id=? AND message_id=?",
                (200, 55),
            ).fetchone(),
        )
        assert isinstance(harness.client.requests[0], DeleteScheduledMessagesRequest)
        assert current["message_state"] == "unknown_missing"
        assert current["publication_hint_message_id"] == 155
        assert result["current_is_operation_confirmation"] is False
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_cancel_with_malformed_sent_id_vector_stays_unconfirmed(tmp_path: Path) -> None:
    schedule_at = int(datetime.now(UTC).timestamp()) + 900
    harness = _OwnerHarness(tmp_path / "sync.db", schedule_at=schedule_at)
    try:
        with write_transaction(harness.conn):
            order = allocate_observation_order(harness.conn)
            harness.conn.execute(
                "INSERT INTO scheduled_messages(dialog_id,message_id,scheduled_at,text,formatting_entities,"
                "has_rich_message,invert_media,video_processing_pending,first_seen_at,updated_at,observation_order) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (200, 55, schedule_at, "old", "[]", 0, 0, 0, schedule_at - 500, schedule_at - 500, order),
            )
        version = scheduled_message_version(100, 200, 55, order)
        malformed_delete = types.UpdateDeleteScheduledMessages(
            types.PeerUser(200),
            [44, 55],
            sent_messages=[155],
        )
        harness.client.response = SimpleNamespace(updates=[malformed_delete])
        result = await harness.owner.execute(
            {
                "action": "cancel",
                "account_id": 100,
                "dialog_id": 200,
                "scheduled_message_id": 55,
                "expected_version": version,
                "operation_key": "cancel-malformed-vector",
            }
        )
        current = cast(
            sqlite3.Row,
            harness.conn.execute(
                "SELECT message_state FROM scheduled_messages WHERE dialog_id=? AND message_id=?",
                (200, 55),
            ).fetchone(),
        )
        assert result["outcome"] == "acknowledged"
        assert result["current_is_operation_confirmation"] is False
        assert current["message_state"] == "scheduled"
    finally:
        harness.close()
