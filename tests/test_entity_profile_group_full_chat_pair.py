"""Acceptance checks for the legacy group FullChat projection pair."""

# pyright: reportAny=false

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import cast

import pytest
from telethon.tl import types

from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
from mcp_telegram.entity_profile.repository import EntitySectionCommit
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.telegram_demand import RpcAttemptBudget, RpcAttemptBudgetExhaustedError
from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope
from tests.test_entity_profile_full_user_pair import _fenced_schema, _pair_service


def _full_chat(
    *,
    chat_id: int = 123,
    participants: object | None = None,
) -> types.messages.ChatFull:
    if participants is None:
        participants = types.ChatParticipants(chat_id=chat_id, participants=[], version=1)
    full = types.ChatFull(
        id=chat_id,
        about="about",
        participants=cast(types.TypeChatParticipants, participants),
        notify_settings=types.PeerNotifySettings(),
        exported_invite=types.ChatInviteExported(link="https://t.me/+group", admin_id=1, date=None),
    )
    return types.messages.ChatFull(full_chat=full, chats=[], users=[])


class _GroupClient:
    def __init__(self, response: object, *, failure: BaseException | None = None) -> None:
        self.response = response
        self.failure = failure
        self.calls: list[str] = []
        self.search_response: object = SimpleNamespace(count=0, messages=[])
        self.after_call: Callable[[], None] | None = None

    async def __call__(self, request: object) -> object:
        scope = current_rpc_scope()
        assert scope.attempt_budget is not None
        scope.attempt_budget.debit()
        name = cast(tuple[str, object], request)[0]
        self.calls.append(name)
        if self.failure is not None:
            raise self.failure
        if name == "search":
            return self.search_response
        response = self.response
        if self.after_call is not None:
            self.after_call()
        return response

    async def get_entity(self, _entity_id: int) -> object:
        raise AssertionError("stored core must avoid resolution")

    async def get_messages(self, *_args: object) -> object:
        raise AssertionError("group pair does not fetch messages")

    def iter_participants(self, *_args: object):
        raise AssertionError("group pair does not enumerate participants")

    def iter_dialogs(self):
        raise AssertionError("group pair does not traverse dialogs")


def _service() -> tuple[sqlite3.Connection, DaemonEntityInfoService, _GroupClient]:
    conn = sqlite3.connect(":memory:")
    _fenced_schema(conn)
    conn.execute("INSERT INTO entities VALUES (-123, 'group', 'Group', NULL, NULL, 1)")
    conn.commit()
    client = _GroupClient(_full_chat())
    service = cast(DaemonEntityInfoService, _pair_service(conn, client, enabled=False))  # type: ignore[arg-type]
    service._deps = replace(  # type: ignore[attr-defined]
        service._deps,
        client=client,
        dm_peer_ids=lambda: {7},
        get_full_chat_request=lambda **kwargs: ("full_chat", kwargs),
        get_messages_search_request=lambda **kwargs: ("search", kwargs),
        get_dialog_placement=lambda _entity_id: {},
    )
    service._profiles.save_core({"id": -123, "type": "group", "name": "Group"}, now=100)  # type: ignore[attr-defined]
    service._profiles.mark_pending(-123, now=100)  # type: ignore[attr-defined]
    return conn, service, client


@pytest.mark.asyncio
async def test_group_pair_uses_one_rpc_and_commits_independent_projections() -> None:
    conn, service, client = _service()
    participants = types.ChatParticipants(
        chat_id=123,
        participants=[types.ChatParticipant(user_id=7, inviter_id=1, date=None)],
        version=1,
    )
    client.response = _full_chat(participants=participants)
    await EntityProfileDemandAdapter(service.refresh_coordinator).run_slice(RpcAttemptBudget(limit=1))  # type: ignore[attr-defined]

    assert client.calls == ["full_chat"]
    assert conn.execute(
        "SELECT section, status, reason FROM entity_detail_sections "
        "WHERE entity_id=-123 AND section IN ('full_profile','common_chats','contact_overlap') ORDER BY section"
    ).fetchall() == [
        ("common_chats", "not_applicable", "not_applicable"),
        ("contact_overlap", "fresh", None),
        ("full_profile", "fresh", None),
    ]
    detail = service._profiles.read(-123, now=100)  # type: ignore[attr-defined]
    assert detail is not None
    assert detail.detail["members_count"] == 1
    assert detail.detail["contacts_subscribed"] == [{"id": 7, "name": None, "username": None}]
    assert conn.execute("SELECT next_section FROM entity_profile_refresh_state WHERE entity_id=-123").fetchone() == (
        "avatar_history",
    )
    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()


@pytest.mark.asyncio
async def test_group_pair_real_empty_participants_are_complete_empty_overlap() -> None:
    conn, service, client = _service()
    client.response = _full_chat(participants=types.ChatParticipants(chat_id=123, participants=[], version=1))
    await EntityProfileDemandAdapter(service.refresh_coordinator).run_slice(RpcAttemptBudget(limit=1))  # type: ignore[attr-defined]

    detail = service._profiles.read(-123, now=100)  # type: ignore[attr-defined]
    assert detail is not None
    assert detail.detail["members_count"] == 0
    assert detail.detail["contacts_subscribed"] == []
    assert conn.execute(
        "SELECT status FROM entity_detail_sections WHERE entity_id=-123 AND section='contact_overlap'"
    ).fetchone() == ("fresh",)
    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_chat_id", (True, 123.0, "123", 0, -123, None))
async def test_group_pair_requires_positive_non_bool_participant_chat_id(bad_chat_id: object) -> None:
    conn, service, client = _service()
    participants = types.ChatParticipants(chat_id=123, participants=[], version=1)
    participants.chat_id = cast(int, bad_chat_id)
    client.response = _full_chat(participants=participants)
    await EntityProfileDemandAdapter(service.refresh_coordinator).run_slice(RpcAttemptBudget(limit=1))  # type: ignore[attr-defined]
    assert conn.execute(
        "SELECT status, reason FROM entity_detail_sections WHERE entity_id=-123 AND section='contact_overlap'"
    ).fetchone() == ("unavailable", "participants_target_mismatch")
    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_target", (True, 123.0, "123", 0, -123, None))
async def test_group_pair_rejects_non_positive_or_non_int_target(bad_target: object) -> None:
    conn, service, client = _service()
    response = _full_chat()
    response.full_chat.id = cast(int, bad_target)
    client.response = response
    await EntityProfileDemandAdapter(service.refresh_coordinator).run_slice(RpcAttemptBudget(limit=1))  # type: ignore[attr-defined]
    assert conn.execute(
        "SELECT status, reason, next_section FROM entity_profile_refresh_state WHERE entity_id=-123"
    ).fetchone() == ("failed", "valueerror", "full_profile")
    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()


@pytest.mark.asyncio
async def test_group_pair_sections_share_the_observation_boundary() -> None:
    conn, service, _client = _service()
    await EntityProfileDemandAdapter(service.refresh_coordinator).run_slice(RpcAttemptBudget(limit=1))  # type: ignore[attr-defined]
    assert conn.execute(
        "SELECT observation_started_at, observation_completed_at FROM entity_detail_sections "
        "WHERE entity_id=-123 AND section IN ('full_profile','contact_overlap') ORDER BY section"
    ).fetchall() == [(100, 100), (100, 100)]
    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()


@pytest.mark.asyncio
async def test_old_contact_cursor_keeps_independent_fallback() -> None:
    conn, service, client = _service()
    conn.execute(
        "UPDATE entity_profile_refresh_state SET next_section='contact_overlap', acquisition_cursor=0 WHERE entity_id=-123"
    )
    conn.commit()
    await EntityProfileDemandAdapter(service.refresh_coordinator).run_slice(RpcAttemptBudget(limit=1))  # type: ignore[attr-defined]
    assert client.calls == ["full_chat"]
    assert conn.execute("SELECT next_section FROM entity_profile_refresh_state WHERE entity_id=-123").fetchone() == (
        "avatar_history",
    )
    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()


def _profile_snapshot(conn: sqlite3.Connection) -> tuple[object, object, object]:
    return (
        conn.execute("SELECT detail_json, profile_revision FROM entity_details WHERE entity_id=-123").fetchone(),
        conn.execute(
            "SELECT section, status, reason, payload_json FROM entity_detail_sections "
            "WHERE entity_id=-123 ORDER BY section"
        ).fetchall(),
        conn.execute(
            "SELECT status, retry_at, reason, next_section, acquisition_cursor, generation, profile_revision, "
            "follow_up_required FROM entity_profile_refresh_state WHERE entity_id=-123"
        ).fetchone(),
    )


def _assert_stale_group_cursor_is_unchanged(conn: sqlite3.Connection, service: object, column: str) -> None:
    repo = service._profiles  # type: ignore[attr-defined]
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    conn.execute(f"UPDATE entity_profile_refresh_state SET {column}={column}+1 WHERE entity_id=-123")
    conn.commit()
    expected = _profile_snapshot(conn)
    assert not repo.commit_group_full_chat_pair(
        cursor,
        EntitySectionCommit({"about": "stale"}),
        EntitySectionCommit({"contacts_subscribed": []}),
        now=101,
    )
    assert _profile_snapshot(conn) == expected


def test_group_pair_generation_only_stale_cursor_does_not_mutate_anything() -> None:
    conn, service, _client = _service()
    _assert_stale_group_cursor_is_unchanged(conn, service, "generation")
    conn.close()


def test_group_pair_detail_revision_only_stale_cursor_does_not_mutate_anything() -> None:
    conn, service, _client = _service()
    repo = service._profiles
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    conn.execute(
        "INSERT INTO entity_details(entity_id, detail_json, fetched_at, profile_revision) VALUES (?, ?, ?, ?)",
        (-123, '{"schema":1}', 100, cursor.profile_revision + 1),
    )
    conn.commit()
    expected = _profile_snapshot(conn)
    assert not repo.commit_group_full_chat_pair(
        cursor,
        EntitySectionCommit({"about": "stale"}),
        EntitySectionCommit({"contacts_subscribed": []}),
        now=101,
    )
    assert _profile_snapshot(conn) == expected
    conn.close()


@pytest.mark.asyncio
async def test_ordinary_completion_follow_up_starts_one_fenced_generation() -> None:
    conn, service, client = _service()
    adapter = EntityProfileDemandAdapter(service.refresh_coordinator)  # type: ignore[attr-defined]
    await adapter.run_slice(RpcAttemptBudget(limit=1))
    await adapter.run_slice(RpcAttemptBudget(limit=1))
    await adapter.run_slice(RpcAttemptBudget(limit=1))
    assert conn.execute("SELECT status FROM entity_profile_refresh_state WHERE entity_id=-123").fetchone() == (
        "complete",
    )
    assert service._profiles.request_follow_up(-123, now=100)  # type: ignore[attr-defined]
    await adapter.run_slice(RpcAttemptBudget(limit=1))
    detail_revision = conn.execute("SELECT profile_revision FROM entity_details WHERE entity_id=-123").fetchone()[0]
    assert conn.execute(
        "SELECT generation, profile_revision, next_section, acquisition_cursor, follow_up_required "
        "FROM entity_profile_refresh_state WHERE entity_id=-123"
    ).fetchone() == (2, detail_revision, "avatar_history", 0, 0)
    assert client.calls == ["full_chat", "search", "full_chat"]
    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("forbidden", "missing", "nonsequence", "malformed"))
async def test_group_pair_keeps_full_profile_when_participants_are_unavailable(kind: str) -> None:
    conn, service, client = _service()
    participants: object
    if kind == "forbidden":
        participants = types.ChatParticipantsForbidden(chat_id=123)
    elif kind == "missing":
        participants = None
    elif kind == "nonsequence":
        participants = types.ChatParticipants(chat_id=123, participants=[], version=1)
        participants.participants = cast(list[types.TypeChatParticipant], object())
    else:
        participants = types.ChatParticipants(
            chat_id=123,
            participants=[cast(types.TypeChatParticipant, object())],
            version=1,
        )
    client.response = _full_chat(participants=participants)
    if kind == "missing":
        cast(types.ChatFull, client.response.full_chat).participants = None  # type: ignore[union-attr]
    await EntityProfileDemandAdapter(service.refresh_coordinator).run_slice(RpcAttemptBudget(limit=1))  # type: ignore[attr-defined]

    assert conn.execute(
        "SELECT status, reason FROM entity_detail_sections WHERE entity_id=-123 AND section='full_profile'"
    ).fetchone() == ("fresh", None)
    assert conn.execute(
        "SELECT status, reason FROM entity_detail_sections WHERE entity_id=-123 AND section='contact_overlap'"
    ).fetchone() == ("unavailable", f"participants_{'not_sequence' if kind == 'nonsequence' else kind}")
    assert service._profiles.read(-123, now=100).detail["contacts_subscribed"] is None  # type: ignore[union-attr]
    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", (True, 0, -1, None))
async def test_group_pair_invalid_participant_ids_do_not_create_empty_overlap(bad_id: object) -> None:
    conn, service, client = _service()
    participant = types.ChatParticipant(user_id=1, inviter_id=1, date=None)
    participant.user_id = cast(int, bad_id)
    client.response = _full_chat(
        participants=types.ChatParticipants(chat_id=123, participants=[participant], version=1)
    )
    await EntityProfileDemandAdapter(service.refresh_coordinator).run_slice(RpcAttemptBudget(limit=1))  # type: ignore[attr-defined]
    assert conn.execute(
        "SELECT status, reason FROM entity_detail_sections WHERE entity_id=-123 AND section='contact_overlap'"
    ).fetchone() == ("unavailable", "participants_malformed")
    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    (
        object(),
        types.messages.ChatFull(
            full_chat=types.ChannelFull(
                id=123,
                about="about",
                read_inbox_max_id=0,
                read_outbox_max_id=0,
                unread_count=0,
                chat_photo=types.PhotoEmpty(id=1),
                notify_settings=types.PeerNotifySettings(),
                bot_info=[],
                pts=1,
            ),
            chats=[],
            users=[],
        ),
    ),
)
async def test_group_pair_rejects_wrong_envelope_and_nested_channel_full(response: object) -> None:
    conn, service, client = _service()
    client.response = response
    await EntityProfileDemandAdapter(service.refresh_coordinator).run_slice(RpcAttemptBudget(limit=1))  # type: ignore[attr-defined]
    assert conn.execute(
        "SELECT status, reason, next_section FROM entity_profile_refresh_state WHERE entity_id=-123"
    ).fetchone() == ("failed", "valueerror", "full_profile")
    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()


@pytest.mark.asyncio
async def test_group_pair_budget_and_flood_wait_leave_projections_unwritten() -> None:
    for failure in (RpcAttemptBudgetExhaustedError("budget"), TelegramRpcThrottled(retry_after_seconds=7)):
        conn, service, client = _service()
        client.failure = failure
        await EntityProfileDemandAdapter(service.refresh_coordinator).run_slice(RpcAttemptBudget(limit=1))  # type: ignore[attr-defined]
        assert conn.execute(
            "SELECT COUNT(*) FROM entity_detail_sections WHERE entity_id=-123 AND status='fresh'"
        ).fetchone() == (0,)
        await service.shutdown()  # type: ignore[attr-defined]
        conn.close()


def test_group_pair_repository_rolls_back_when_cursor_advance_is_rejected() -> None:
    conn, service, _client = _service()
    repo = service._profiles  # type: ignore[attr-defined]
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    before_detail = conn.execute(
        "SELECT detail_json, profile_revision FROM entity_details WHERE entity_id=-123"
    ).fetchone()
    before_sections = conn.execute(
        "SELECT section, status, reason, payload_json FROM entity_detail_sections WHERE entity_id=-123 ORDER BY section"
    ).fetchall()
    original = repo._advance_group_full_chat_cursor
    repo._advance_group_full_chat_cursor = lambda *_args, **_kwargs: False  # type: ignore[method-assign]
    with pytest.raises(sqlite3.OperationalError, match="cursor advance"):
        repo.commit_group_full_chat_pair(
            cursor,
            EntitySectionCommit({"about": "new"}),
            EntitySectionCommit({"contacts_subscribed": []}),
            now=101,
        )
    repo._advance_group_full_chat_cursor = original  # type: ignore[method-assign]
    assert (
        conn.execute("SELECT detail_json, profile_revision FROM entity_details WHERE entity_id=-123").fetchone()
        == before_detail
    )
    assert (
        conn.execute(
            "SELECT section, status, reason, payload_json FROM entity_detail_sections WHERE entity_id=-123 ORDER BY section"
        ).fetchall()
        == before_sections
    )
    assert conn.execute("SELECT next_section FROM entity_profile_refresh_state WHERE entity_id=-123").fetchone() == (
        "full_profile",
    )
    conn.close()


@pytest.mark.asyncio
async def test_group_pair_follow_up_carries_detail_revision_into_next_generation() -> None:
    conn, service, client = _service()
    adapter = EntityProfileDemandAdapter(service.refresh_coordinator)  # type: ignore[attr-defined]
    await adapter.run_slice(RpcAttemptBudget(limit=1))
    assert service._profiles.request_follow_up(-123, now=100)  # type: ignore[attr-defined]
    await adapter.run_slice(RpcAttemptBudget(limit=1))
    await adapter.run_slice(RpcAttemptBudget(limit=1))

    detail_revision = conn.execute("SELECT profile_revision FROM entity_details WHERE entity_id=-123").fetchone()[0]
    assert conn.execute(
        "SELECT status, generation, profile_revision, next_section, acquisition_cursor, follow_up_required "
        "FROM entity_profile_refresh_state WHERE entity_id=-123"
    ).fetchone() == ("pending", 2, detail_revision, "full_profile", 0, 0)
    await adapter.run_slice(RpcAttemptBudget(limit=1))
    assert client.calls == ["full_chat", "search", "full_chat"]
    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()


@pytest.mark.asyncio
async def test_group_pair_auth_scope_change_does_not_commit_evidence() -> None:
    conn, service, client = _service()
    from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope

    scopes = [TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99)]
    service._deps = replace(service._deps, full_user_auth_scope=lambda: scopes[0])  # type: ignore[attr-defined]

    def change_scope() -> None:
        scopes[0] = TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 3, 99)

    client.after_call = change_scope
    await EntityProfileDemandAdapter(service.refresh_coordinator).run_slice(RpcAttemptBudget(limit=1))  # type: ignore[attr-defined]
    assert conn.execute(
        "SELECT status, reason, next_section FROM entity_profile_refresh_state WHERE entity_id=-123"
    ).fetchone() == ("failed", "auth_scope_changed", "full_profile")
    assert conn.execute(
        "SELECT COUNT(*) FROM entity_detail_sections WHERE entity_id=-123 AND status='fresh'"
    ).fetchone() == (0,)
    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()


@pytest.mark.parametrize("failure_index", (0, 1, 2))
def test_group_pair_rolls_back_when_any_section_write_raises(failure_index: int) -> None:
    conn, service, _client = _service()
    repo = service._profiles  # type: ignore[attr-defined]
    cursor = repo.next_due_refresh(now=100)
    assert cursor is not None
    before_detail = conn.execute(
        "SELECT detail_json, profile_revision FROM entity_details WHERE entity_id=-123"
    ).fetchone()
    before_sections = conn.execute(
        "SELECT section, status, reason, payload_json FROM entity_detail_sections WHERE entity_id=-123 ORDER BY section"
    ).fetchall()
    original = repo._write_section
    calls = 0

    def failing_write(*args: object, **kwargs: object) -> bool:
        nonlocal calls
        if calls == failure_index:
            raise sqlite3.IntegrityError("test section write")
        calls += 1
        return original(*args, **kwargs)  # type: ignore[arg-type]

    repo._write_section = failing_write  # type: ignore[method-assign]
    with pytest.raises(sqlite3.IntegrityError, match="test section write"):
        repo.commit_group_full_chat_pair(
            cursor,
            EntitySectionCommit({"about": "new"}),
            EntitySectionCommit({"contacts_subscribed": []}),
            now=101,
        )
    repo._write_section = original  # type: ignore[method-assign]
    assert (
        conn.execute("SELECT detail_json, profile_revision FROM entity_details WHERE entity_id=-123").fetchone()
        == before_detail
    )
    assert (
        conn.execute(
            "SELECT section, status, reason, payload_json FROM entity_detail_sections WHERE entity_id=-123 ORDER BY section"
        ).fetchall()
        == before_sections
    )
    conn.close()
