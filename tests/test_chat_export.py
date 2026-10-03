"""Telegram export fixtures preserve current facts without changing the archive."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TypedDict, cast

import pytest
from telethon.errors import ChatAdminRequiredError, UserNotParticipantError
from telethon.tl import functions, types

from mcp_telegram import chat_export
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.telegram_rpc_consumers import TelegramRpcSource
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcAdmissionDeferred, rpc_scope

DATE = datetime(2026, 10, 1, tzinfo=UTC)
DIALOG = -1000000000123


class Facts(TypedDict):
    """Fields asserted by these fixtures; missing expected keys fail the tests."""

    items: list[Facts]
    raw: Facts
    entities: list[Facts]
    author: Facts
    action: Facts
    related_users: list[Facts]
    new_participant: Facts
    banned_rights: Facts
    new_message: Facts
    participant: Facts
    group: Facts
    peer: Facts
    reaction: Facts
    reactions: Facts
    id: int
    dialog_id: int
    next_before_id: int
    upper_id: int
    migrated_from_dialog_id: int | None
    message: str
    _: str
    identity_source: str
    rank: str | None
    is_admin: bool | None
    role: str
    status: str
    date: str
    kind: str
    emoticon: str
    next_offset: str | None
    done: bool
    view_messages: bool
    big: bool | None
    unread: bool | None
    my: bool | None


class Result(TypedDict):
    ok: bool
    data: Facts
    error: str
    reason: str
    retry_after: int


class Client:
    def __init__(self, *responses: object, peer: types.TypeInputPeer | None = None) -> None:
        self.responses = list(responses)
        self.requests: list[object] = []
        self.peer = peer or types.InputPeerChannel(123, 456)

    async def get_input_entity(self, dialog_id: int | str) -> types.TypeInputPeer:
        return types.InputPeerUser(dialog_id, 0) if isinstance(dialog_id, int) and dialog_id > 0 else self.peer

    async def __call__(self, request: object) -> object:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.fixture
def archive() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE entities (id INTEGER, name TEXT, username TEXT, updated_at TEXT)")
    conn.execute('INSERT INTO entities VALUES (7, "Cached author", "cached", "old")')
    changes = conn.total_changes
    conn.set_authorizer(
        lambda action, *args: (
            sqlite3.SQLITE_OK
            if action
            in {
                sqlite3.SQLITE_SELECT,
                sqlite3.SQLITE_READ,
                sqlite3.SQLITE_FUNCTION,
            }
            else sqlite3.SQLITE_DENY
        )
    )
    yield conn
    assert conn.total_changes == changes
    conn.close()


async def call(archive: sqlite3.Connection, client: Client, operation: str = "history", **kwargs: object) -> Result:
    with rpc_scope(TelegramRpcSource.CHAT_EXPORT):
        result = await chat_export.export_operation(
            client,
            {
                "dialog_id": DIALOG,
                "operation": operation,
                **kwargs,
            },
            archive,
        )
    # The operation has a tagged, operation-dependent JSON payload.
    return cast(Result, result)


def response(**kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(users=[], chats=[], **kwargs)


def message(
    message_id: int, text: str = "full text", *, entities: list[types.TypeMessageEntity] | None = None
) -> types.Message:
    return types.Message(
        id=message_id,
        peer_id=types.PeerChannel(123),
        date=DATE,
        message=text,
        from_id=types.PeerUser(7),
        entities=entities,
    )


async def test_history_full_facts_service_actions_and_frozen_id_cursor(archive: sqlite3.Connection) -> None:
    services = [
        types.MessageService(id=i, peer_id=types.PeerChannel(123), date=DATE, from_id=types.PeerUser(7), action=action)
        for i, action in [
            (90, types.MessageActionChatAddUser([8])),
            (80, types.MessageActionChatDeleteUser(8)),
            (70, types.MessageActionPinMessage()),
        ]
    ]
    text = "bold\n" + "long " * 500
    client = Client(
        response(messages=[message(100, text, entities=[types.MessageEntityBold(0, 4)]), *services]),
        response(messages=[message(20)]),
        response(messages=[]),
    )
    first = await call(archive, client, upper_id=100)
    assert first["ok"]
    items = first["data"]["items"]
    assert items[0]["raw"]["message"] == text
    assert items[0]["raw"]["entities"][0]["_"] == "MessageEntityBold"
    assert items[0]["author"]["identity_source"] == "local_cache"
    assert [item["raw"]["action"]["_"] for item in items[1:]] == [
        "MessageActionChatAddUser",
        "MessageActionChatDeleteUser",
        "MessageActionPinMessage",
    ]
    assert [user["id"] for user in items[1]["related_users"]] == [8]
    assert [user["id"] for user in items[2]["related_users"]] == [8]
    assert first["data"]["next_before_id"] == 70
    assert not first["data"]["done"]
    second = await call(archive, client, upper_id=100, before_id=70)
    assert second["data"]["next_before_id"] == 20
    history_request = client.requests[1]
    assert isinstance(history_request, functions.messages.GetHistoryRequest)
    assert history_request.offset_id == 70
    assert history_request.max_id == 101
    assert (await call(archive, client, upper_id=100, before_id=20))["data"]["done"]


async def test_history_lower_bound_is_exclusive_and_passed_to_telegram(archive: sqlite3.Connection) -> None:
    client = Client(response(messages=[message(i) for i in [100, 20, 7, 3]]))
    result = await call(archive, client, upper_id=100, min_id=7)
    assert [item["id"] for item in result["data"]["items"]] == [100, 20]
    request = client.requests[0]
    assert isinstance(request, functions.messages.GetHistoryRequest)
    assert request.min_id == 7

    empty_client = Client(response(messages=[message(10)]))
    empty = await call(archive, empty_client, upper_id=10, min_id=10)
    assert empty["data"] == {"items": [], "next_before_id": 0, "done": True}
    assert empty_client.requests == []


@pytest.mark.parametrize("lower_id", [True, -1, "7", 2**31])
async def test_history_rejects_invalid_minimum_id(archive: sqlite3.Connection, lower_id: object) -> None:
    result = await call(archive, Client(response(messages=[])), upper_id=100, min_id=lower_id)
    assert result["ok"] is False


async def test_history_defaults_lower_bound_to_zero(archive: sqlite3.Connection) -> None:
    client = Client(response(messages=[message(10)]))
    assert (await call(archive, client, upper_id=10))["data"]["items"][0]["id"] == 10
    request = client.requests[0]
    assert isinstance(request, functions.messages.GetHistoryRequest)
    assert request.min_id == 0


@pytest.mark.parametrize("ids,before", [([101], 0), ([70], 70)])
async def test_history_rejects_nonadvancing_or_outside_frozen_boundary(
    archive: sqlite3.Connection, ids: list[int], before: int
) -> None:
    result = await call(archive, Client(response(messages=[message(i) for i in ids])), upper_id=100, before_id=before)
    assert result["ok"] is False


@pytest.mark.parametrize("ids", [[100, 90, 95], [100, 90, 90]])
async def test_history_rejects_malformed_page_order_before_returning_items(
    archive: sqlite3.Connection, ids: list[int]
) -> None:
    result = await call(archive, Client(response(messages=[message(i) for i in ids])), upper_id=100)
    assert result["ok"] is False
    assert result["error"] == "export_failed"
    assert "data" not in result


async def test_byte_split_resumes_at_last_delivered_id_and_oversize_is_explicit(
    archive: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    single = Client(response(messages=[message(10)]))
    item = (await call(archive, single, upper_id=10))["data"]["items"][0]
    monkeypatch.setattr(chat_export, "EXPORT_RESPONSE_BYTES", len(json.dumps(item, ensure_ascii=False).encode()) + 10)
    result = await call(archive, Client(response(messages=[message(10), message(5)])), upper_id=10)
    assert [item["id"] for item in result["data"]["items"]] == [10]
    assert result["data"]["next_before_id"] == 10
    oversized = await call(archive, Client(response(messages=[message(10, "x" * 10000)])), upper_id=10)
    assert oversized["ok"] is False


async def test_open_identifies_migrated_predecessor_and_legacy_group(archive: sqlite3.Connection) -> None:
    group = types.Channel(id=123, title="Current group", photo=types.ChatPhotoEmpty(), date=DATE, megagroup=True)
    full = SimpleNamespace(full_chat=SimpleNamespace(migrated_from_chat_id=55), users=[], chats=[group])
    client = Client(response(messages=[message(100)], count=10), full)
    result = await call(archive, client, "open")
    assert result["data"]["upper_id"] == 100
    assert result["data"]["migrated_from_dialog_id"] == -55
    legacy = Client(response(messages=[]), response(full_chat=SimpleNamespace()), peer=types.InputPeerChat(55))
    result = await call(archive, legacy, "open", dialog_id=-55)
    assert result["data"]["group"]["kind"] == "group"
    assert isinstance(legacy.requests[1], functions.messages.GetFullChatRequest)


async def test_admin_audit_keeps_ban_rank_but_never_deleted_or_edited_bodies(archive: sqlite3.Connection) -> None:
    member = types.ChannelParticipant(7, DATE)
    admin = types.ChannelParticipantAdmin(7, 8, DATE, types.ChatAdminRights(ban_users=True), rank="Moderator")
    banned = types.ChannelParticipantBanned(
        types.PeerUser(7), 8, DATE, types.ChatBannedRights(DATE, view_messages=True)
    )
    actions = [
        types.ChannelAdminLogEventActionParticipantToggleAdmin(member, admin),
        types.ChannelAdminLogEventActionParticipantToggleBan(member, banned),
        types.ChannelAdminLogEventActionEditMessage(message(4, "REMOVED"), message(4, "REPLACEMENT")),
        types.ChannelAdminLogEventActionDeleteMessage(message(3, "DELETED")),
    ]
    events = [types.ChannelAdminLogEvent(100 - i, DATE, 7, action) for i, action in enumerate(actions)]
    result = await call(archive, Client(response(events=events)), "admin_log")
    items = result["data"]["items"]
    assert items[0]["action"]["new_participant"]["rank"] == "Moderator"
    assert [user["id"] for user in items[0]["related_users"]] == [7, 8]
    assert [user["id"] for user in items[1]["related_users"]] == [7, 8]
    assert items[1]["action"]["new_participant"]["banned_rights"]["view_messages"]
    serialized = json.dumps(items)
    assert all(text not in serialized for text in ["REMOVED", "REPLACEMENT", "DELETED"])
    assert items[2]["action"]["new_message"]["id"] == 4
    assert result["data"]["next_before_id"] == 97


async def test_admin_log_minimum_event_id_is_exclusive(archive: sqlite3.Connection) -> None:
    events = [
        types.ChannelAdminLogEvent(event_id, DATE, 7, types.ChannelAdminLogEventActionParticipantJoin())
        for event_id in [100, 20, 7, 3]
    ]
    client = Client(response(events=events))
    result = await call(archive, client, "admin_log", min_id=7)
    assert [item["id"] for item in result["data"]["items"]] == [100, 20]
    request = client.requests[0]
    assert isinstance(request, functions.channels.GetAdminLogRequest)
    assert request.min_id == 7


@pytest.mark.parametrize("ids", [[100, 90, 95], [100, 90, 90]])
async def test_admin_log_rejects_malformed_page_order_before_returning_items(
    archive: sqlite3.Connection, ids: list[int]
) -> None:
    events = [
        types.ChannelAdminLogEvent(event_id, DATE, 7, types.ChannelAdminLogEventActionParticipantJoin())
        for event_id in ids
    ]
    result = await call(archive, Client(response(events=events)), "admin_log")
    assert result["ok"] is False
    assert result["error"] == "export_failed"
    assert "data" not in result


@pytest.mark.parametrize("lower_id", [True, -1, "7", 2**63])
async def test_admin_log_rejects_invalid_minimum_event_id(archive: sqlite3.Connection, lower_id: object) -> None:
    client = Client(response(events=[]))
    result = await call(archive, client, "admin_log", min_id=lower_id)
    assert result["ok"] is True
    assert result["data"]["status"] == "unavailable"
    assert client.requests == []


async def test_participant_current_admin_unknown_and_former(archive: sqlite3.Connection) -> None:
    admin = types.ChannelParticipantAdmin(7, 8, DATE, types.ChatAdminRights(ban_users=True), rank="Moderator")
    result = await call(archive, Client(response(participant=admin)), "participant", user_id=7)
    assert result["data"]["participant"]["is_admin"] is True
    assert result["data"]["participant"]["rank"] == "Moderator"
    unknown = await call(archive, Client(response(participant=None)), "participant", user_id=7)
    assert unknown["data"]["participant"]["is_admin"] is None
    assert unknown["data"]["status"] == "unavailable"
    assert unknown["data"]["participant"]["role"] == "unknown"
    assert unknown["data"]["participant"]["rank"] is None
    former = await call(archive, Client(UserNotParticipantError(None)), "participant", user_id=7)
    assert former["data"]["participant"]["role"] == "former_member"
    assert former["data"]["participant"]["is_admin"] is False


async def test_reaction_opaque_cursor_identity_and_date(archive: sqlite3.Connection) -> None:
    reaction = types.MessagePeerReaction(types.PeerUser(7), DATE, types.ReactionEmoji("👍"))
    client = Client(response(reactions=[reaction], count=2, next_offset="opaque+/=cursor"))
    result = await call(archive, client, "reactions", message_id=10, offset="previous+/=")
    reaction_request = client.requests[0]
    assert isinstance(reaction_request, functions.messages.GetMessageReactionsListRequest)
    assert reaction_request.offset == "previous+/="
    assert result["data"]["next_offset"] == "opaque+/=cursor"
    item = result["data"]["items"][0]
    assert item["peer"]["id"] == 7
    assert item["date"] == DATE.isoformat()
    assert item["reaction"]["emoticon"] == "👍"


async def test_history_complete_recent_reactors_match_paged_wire_facts(archive: sqlite3.Connection) -> None:
    emoji = types.ReactionEmoji("👍")
    custom = types.ReactionCustomEmoji(123456)
    records = [
        types.MessagePeerReaction(types.PeerUser(7), DATE, emoji, big=True, unread=True, my=True),
        types.MessagePeerReaction(types.PeerUser(7), DATE, custom),
        types.MessagePeerReaction(types.PeerUser(8), DATE, emoji),
    ]
    users = [types.User(7, first_name="Current", username="current"), types.User(8, first_name="Other")]
    item = message(10)
    item.reactions = types.MessageReactions(
        [types.ReactionCount(emoji, 2), types.ReactionCount(custom, 1)],
        can_see_list=True,
        recent_reactions=records,
    )
    client = Client(SimpleNamespace(messages=[item], users=users, chats=[]))
    history = await call(archive, client, upper_id=10)
    envelope = history["data"]["items"][0]["reactions"]
    assert envelope["status"] == "complete"
    assert len(client.requests) == 1
    assert isinstance(client.requests[0], functions.messages.GetHistoryRequest)
    paged = await call(
        archive,
        Client(SimpleNamespace(reactions=records, users=users, chats=[], count=3)),
        "reactions",
        message_id=10,
    )
    assert envelope["items"] == paged["data"]["items"]
    assert [record["peer"]["id"] for record in envelope["items"]] == [7, 7, 8]
    assert envelope["items"][0]["peer"]["identity_source"] == "telegram_response"
    assert envelope["items"][0]["date"] == DATE.isoformat()
    assert envelope["items"][0]["raw"]["big"] is True
    assert envelope["items"][0]["raw"]["unread"] is True
    assert envelope["items"][0]["raw"]["my"] is True


@pytest.mark.parametrize(
    "reaction_changes,users",
    [
        ({"min": True}, [types.User(7, first_name="Current")]),
        ({"can_see_list": False}, [types.User(7, first_name="Current")]),
        ({"top_reactors": [types.MessageReactor(1, peer_id=types.PeerUser(7))]}, [types.User(7, first_name="Current")]),
        ({"results": [types.ReactionCount(types.ReactionPaid(), 1)]}, [types.User(7, first_name="Current")]),
        ({"results": [types.ReactionCount(types.ReactionEmoji("👍"), 2)]}, [types.User(7, first_name="Current")]),
        ({"results": [types.ReactionCount(types.ReactionEmoji("❤"), 1)]}, [types.User(7, first_name="Current")]),
        (
            {"results": [types.ReactionCount(types.ReactionEmoji("👍"), 1)] * 2},
            [types.User(7, first_name="Current")],
        ),
        (
            {"recent_reactions": [types.MessagePeerReaction(types.PeerUser(7), DATE, types.ReactionEmoji("👍"))] * 2},
            [types.User(7, first_name="Current")],
        ),
        (
            {"recent_reactions": [types.MessagePeerReaction(types.PeerUser(7), None, types.ReactionEmoji("👍"))]},
            [types.User(7, first_name="Current")],
        ),
        (
            {"recent_reactions": [types.MessagePeerReaction(types.PeerUser(7), DATE, types.ReactionPaid())]},
            [types.User(7, first_name="Current")],
        ),
        ({"recent_reactions": []}, [types.User(7, first_name="Current")]),
        ({}, []),
        ({}, [types.UserEmpty(7)]),
        ({}, [types.User(7, first_name="Partial", min=True)]),
        (
            {"recent_reactions": [types.MessagePeerReaction(types.PeerChannel(123), DATE, types.ReactionEmoji("👍"))]},
            [types.Channel(123, "Partial channel", types.ChatPhotoEmpty(), DATE, min=True)],
        ),
    ],
    ids=[
        "min",
        "hidden",
        "paid-top-reactors",
        "paid-count",
        "count-mismatch",
        "reaction-mismatch",
        "duplicate-count",
        "duplicate-peer",
        "missing-date",
        "paid-record",
        "missing-records",
        "cached-identity",
        "empty-identity",
        "min-user-identity",
        "min-channel-identity",
    ],
)
async def test_history_ambiguous_recent_reactors_keep_paged_fallback(
    archive: sqlite3.Connection, reaction_changes: dict[str, object], users: list[object]
) -> None:
    reaction = types.MessagePeerReaction(types.PeerUser(7), DATE, types.ReactionEmoji("👍"))
    reactions = types.MessageReactions(
        [types.ReactionCount(types.ReactionEmoji("👍"), 1)], can_see_list=True, recent_reactions=[reaction]
    )
    for key, value in reaction_changes.items():
        setattr(reactions, key, value)
    item = message(10)
    item.reactions = reactions
    client = Client(
        SimpleNamespace(
            messages=[item],
            users=[entity for entity in users if isinstance(entity, (types.User, types.UserEmpty))],
            chats=[entity for entity in users if isinstance(entity, types.Channel)],
        ),
        response(reactions=[reaction], count=1),
    )
    history = await call(archive, client, upper_id=10)
    envelope = history["data"]["items"][0]["reactions"]
    assert envelope["status"] == "pending"
    assert "items" not in envelope
    paged = await call(archive, client, "reactions", message_id=10)
    assert paged["data"]["items"][0]["peer"]["id"] == 7
    assert isinstance(client.requests[1], functions.messages.GetMessageReactionsListRequest)


@pytest.mark.parametrize(
    "error,reason,seconds",
    [
        (TelegramRpcAdmissionDeferred(3), "admission", 3),
        (TelegramRpcThrottled(4), "flood_wait", 4),
    ],
)
async def test_deferrals_return_without_retry_or_sleep(
    archive: sqlite3.Connection, error: TelegramRpcThrottled, reason: str, seconds: int
) -> None:
    client = Client(error)
    result = await asyncio.wait_for(call(archive, client, upper_id=10), 1)
    assert result == {"ok": False, "error": "export_deferred", "reason": reason, "retry_after": seconds}
    assert len(client.requests) == 1


@pytest.mark.parametrize("operation", ["history", "reactions", "participant", "topic", "admin_log"])
async def test_operation_timeout_is_deferred_for_every_operation(
    archive: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    async def timeout(*args: object) -> dict[str, object]:
        raise TimeoutError

    monkeypatch.setattr(chat_export, "_perform", timeout)
    result = await call(archive, Client(), operation, upper_id=10)
    assert result == {
        "ok": False,
        "error": "export_deferred",
        "reason": "operation_timeout",
        "retry_after": 5,
    }


async def test_admin_permission_error_remains_unavailable(archive: sqlite3.Connection) -> None:
    result = await call(archive, Client(ChatAdminRequiredError(request=None)), "admin_log")
    assert result["ok"] is True
    assert result["data"]["status"] == "unavailable"


async def test_account_protection_is_terminal_and_cancellation_propagates(archive: sqlite3.Connection) -> None:
    result = await call(archive, Client(TelegramRpcThrottled(latched=True)), upper_id=10)
    assert result["error"] == "flood_wait_kill_switch_open"
    assert "retry_after" not in result
    with pytest.raises(asyncio.CancelledError):
        await call(archive, Client(asyncio.CancelledError()), upper_id=10)


async def test_export_error_log_has_safe_frames_without_exception_text(
    archive: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def fail(*args: object) -> dict[str, object]:
        raise ValueError("PRIVATE CONTENT")

    monkeypatch.setattr(chat_export, "_perform", fail)
    result = await call(archive, Client(), upper_id=10)

    assert result["ok"] is False
    assert "PRIVATE CONTENT" not in caplog.text
    assert "error_class=ValueError" in caplog.text
    assert "function'" in caplog.text and "fail" in caplog.text
    assert "test_chat_export.py" in caplog.text
    assert "line'" in caplog.text


async def test_large_participant_id_is_resolved_to_input_peer(archive: sqlite3.Connection) -> None:
    user_id = 2**40 + 7
    client = Client(response(participant=types.ChannelParticipant(user_id, DATE)))
    result = await call(archive, client, "participant", user_id=user_id)
    assert result["ok"]
    assert result["data"]["participant"]["role"] == "member"
    participant_request = client.requests[0]
    assert isinstance(participant_request, functions.channels.GetParticipantRequest)
    assert isinstance(participant_request.participant, types.InputPeerUser)
    assert participant_request.participant.user_id == user_id


async def test_empty_message_tombstones_advance_without_ending_history(archive: sqlite3.Connection) -> None:
    client = Client(
        response(
            messages=[
                types.MessageEmpty(id=90, peer_id=types.PeerChannel(123)),
                types.MessageEmpty(id=70, peer_id=types.PeerChannel(123)),
            ]
        ),
        response(messages=[message(50)]),
    )
    page = await call(archive, client, upper_id=100)
    assert page["data"]["items"] == []
    assert page["data"]["next_before_id"] == 70
    assert page["data"]["done"] is False
    next_page = await call(archive, client, upper_id=100, before_id=70)
    assert next_page["data"]["items"][0]["id"] == 50


async def test_unicode_page_budget_uses_actual_ipc_json_encoding(
    archive: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "😀漢字" * 100
    reference = await call(archive, Client(response(messages=[message(10, text)])), upper_id=10)
    item = reference["data"]["items"][0]
    actual_size = len(json.dumps(item).encode("utf-8")) + 2
    monkeypatch.setattr(chat_export, "EXPORT_RESPONSE_BYTES", actual_size + 10)
    page = await call(archive, Client(response(messages=[message(10, text), message(5, text)])), upper_id=10)
    assert [item["id"] for item in page["data"]["items"]] == [10]
    assert page["data"]["next_before_id"] == 10
    monkeypatch.setattr(chat_export, "EXPORT_RESPONSE_BYTES", actual_size - 1)
    oversized = await call(archive, Client(response(messages=[message(10, text)])), upper_id=10)
    assert oversized["ok"] is False


async def test_open_public_url_returns_canonical_group_id(archive: sqlite3.Connection) -> None:
    group = types.Channel(id=123, title="Public group", photo=types.ChatPhotoEmpty(), date=DATE, megagroup=True)
    full = response(full_chat=SimpleNamespace(migrated_from_chat_id=None))
    full.chats = [group]
    client = Client(response(messages=[message(100)]), full)
    result = await call(archive, client, "open", dialog_id="https://t.me/ai_engineers_guild")
    assert result["ok"]
    assert result["data"]["group"]["dialog_id"] == DIALOG
