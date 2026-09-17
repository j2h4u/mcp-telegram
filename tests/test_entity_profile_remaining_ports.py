"""Focused contracts for the remaining entity-profile acquisition ports."""

# Telethon's generated unions are broader than the concrete envelopes asserted here.
# pyright: reportAny=false, reportAttributeAccessIssue=false

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from telethon.errors import ChatAdminRequiredError
from telethon.tl import types
from telethon.tl.functions.messages import GetCommonChatsRequest, SearchRequest
from telethon.tl.functions.photos import GetUserPhotosRequest

from mcp_telegram.entity_profile.contracts import (
    ChannelReference,
    ChatCurrentPhoto,
    GroupReference,
    ProjectionStatus,
    UserReference,
    reconcile_chat_avatar_history,
)
from mcp_telegram.entity_profile.repository import EntityProfileRepository
from mcp_telegram.telegram_gateway import (
    TelethonChatAvatarHistoryGateway,
    TelethonCommonChatsGateway,
    TelethonUserAvatarHistoryGateway,
)


class _Client:
    def __init__(self, response: object, *, session: object | None = None) -> None:
        self.response = response
        self.requests: list[object] = []
        self.session = session

    async def __call__(self, request: object) -> object:
        self.requests.append(request)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class _SyncSession:
    def __init__(self, value: object) -> None:
        self.value = value
        self.calls: list[int] = []

    def get_input_entity(self, entity_id: int) -> object:
        self.calls.append(entity_id)
        return self.value


def _user(user_id: int = 7) -> types.User:
    return types.User(user_id, access_hash=8)


def _photo(photo_id: int, date: datetime | None = None) -> types.Photo:
    return types.Photo(photo_id, 1, b"", date, [], 1)


def _chat(chat_id: int = 3) -> types.Chat:
    return types.Chat(chat_id, "Group", types.ChatPhotoEmpty(), 0, None, 1)


def _channel(channel_id: int = 4) -> types.Channel:
    return types.Channel(channel_id, "Channel", types.ChatPhotoEmpty(), None, access_hash=5)


def test_references_are_strict_and_reconciliation_is_deterministic() -> None:
    assert UserReference(7, -8, is_self=False).access_hash == -8
    assert GroupReference(-3).group_id == -3
    with pytest.raises(ValueError):
        UserReference(0, 8, is_self=False)
    with pytest.raises(ValueError):
        GroupReference(0)

    current = ChatCurrentPhoto(2, "current")
    history = (ChatCurrentPhoto(1, "old"), ChatCurrentPhoto(2, "history"), ChatCurrentPhoto(1, "duplicate"))
    merged, count = reconcile_chat_avatar_history(history, current, 1)
    assert merged == (current, ChatCurrentPhoto(1, "old"))
    assert count == 2

    merged, _ = reconcile_chat_avatar_history((ChatCurrentPhoto(2, "history"),), ChatCurrentPhoto(2), 1)
    assert merged == (ChatCurrentPhoto(2, "history"),)


@pytest.mark.asyncio
async def test_common_chats_uses_one_exact_bounded_request_and_normalizes_rows() -> None:
    client = _Client(
        types.messages.Chats(
            [
                _chat(),
                _channel(),
                types.ChatForbidden(id=5, title="Forbidden group"),
                types.ChannelForbidden(id=6, access_hash=9, title="Forbidden channel"),
            ]
        )
    )
    observation = await TelethonCommonChatsGateway(client, now_provider=lambda: 10.0).fetch_common_chats(
        UserReference(7, 8, is_self=False)
    )

    request = client.requests[0]
    assert isinstance(request, GetCommonChatsRequest)
    assert request.user_id.user_id == 7
    assert request.user_id.access_hash == 8
    assert (request.max_id, request.limit) == (0, 100)
    assert observation.status is ProjectionStatus.USABLE
    assert [item.kind for item in observation.chats] == ["group", "channel", "group", "channel"]
    assert [item.chat_id for item in observation.chats] == [-3, -1000000000004, -5, -1000000000006]


@pytest.mark.asyncio
async def test_common_chat_missing_name_falls_back_to_raw_id_and_user_empty_matches_target() -> None:
    common = _Client(
        types.messages.Chats(
            [types.ChatEmpty(id=8), types.Chat(9, cast(str, None), types.ChatPhotoEmpty(), 0, None, 1)]
        )
    )
    observation = await TelethonCommonChatsGateway(common).fetch_common_chats(UserReference(7, 8))
    assert observation.chats[0].name == "9"

    photos = _Client(types.photos.Photos([_photo(2)], [types.UserEmpty(id=7)]))
    result = await TelethonUserAvatarHistoryGateway(photos).fetch_user_avatar_history(UserReference(7, 8))
    assert result.photos == (ChatCurrentPhoto(2),)


@pytest.mark.asyncio
async def test_avatar_rows_skip_empty_transport_placeholders() -> None:
    user_photos = _Client(types.photos.Photos([types.PhotoEmpty(id=0), _photo(3)], [_user()]))
    result = await TelethonUserAvatarHistoryGateway(user_photos).fetch_user_avatar_history(UserReference(7, 8))
    assert result.photos == (ChatCurrentPhoto(3),)

    reference = ChannelReference(-1000000000004, 5)
    messages = types.messages.Messages(
        [
            types.MessageEmpty(id=1, peer_id=types.PeerChannel(4)),
            types.MessageService(
                2,
                types.PeerChannel(4),
                datetime(2026, 1, 1, tzinfo=UTC),
                action=types.MessageActionChatEditPhoto(types.PhotoEmpty(id=0)),
            ),
        ],
        [],
        [],
        [],
    )
    result = await TelethonChatAvatarHistoryGateway(_Client(messages)).fetch_chat_avatar_history(reference)
    assert result.photos == ()


@pytest.mark.asyncio
async def test_user_photos_slice_is_partial_and_mismatch_is_rejected() -> None:
    client = _Client(types.photos.PhotosSlice(9, [_photo(2)], [_user()]))
    observation = await TelethonUserAvatarHistoryGateway(client, now_provider=lambda: 10.0).fetch_user_avatar_history(
        UserReference(7, 8, is_self=False)
    )

    request = client.requests[0]
    assert isinstance(request, GetUserPhotosRequest)
    assert request.user_id.user_id == 7
    assert (request.offset, request.max_id, request.limit) == (0, 0, 100)
    assert observation.status is ProjectionStatus.PARTIAL
    assert observation.reason == "bounded_page"
    assert observation.reported_count == 9

    mismatch = _Client(types.photos.Photos([_photo(2)], [_user(9)]))
    with pytest.raises(ValueError, match="target"):
        await TelethonUserAvatarHistoryGateway(mismatch).fetch_user_avatar_history(UserReference(7, 8, False))


@pytest.mark.asyncio
async def test_chat_photos_uses_exact_filter_and_rejects_wrong_envelopes() -> None:
    date = datetime(2026, 1, 1, tzinfo=UTC)
    message = types.MessageService(
        1,
        types.PeerChannel(4),
        date,
        action=types.MessageActionChatEditPhoto(_photo(12, date)),
    )
    client = _Client(types.messages.Messages([message], [], [], []))
    reference = ChannelReference(-1000000000004, 5)
    observation = await TelethonChatAvatarHistoryGateway(client).fetch_chat_avatar_history(reference)

    request = client.requests[0]
    assert isinstance(request, SearchRequest)
    assert request.peer.channel_id == 4
    assert request.q == ""
    assert isinstance(request.filter, types.InputMessagesFilterChatPhotos)
    assert (request.offset_id, request.add_offset, request.limit) == (0, 0, 100)
    assert observation.status is ProjectionStatus.USABLE
    assert observation.photos == (ChatCurrentPhoto(12, date.isoformat()),)

    with pytest.raises(ValueError, match="envelope"):
        await TelethonChatAvatarHistoryGateway(_Client(object())).fetch_chat_avatar_history(reference)


@pytest.mark.asyncio
async def test_stable_denial_is_unavailable_and_transient_errors_propagate() -> None:
    reference = UserReference(7, 8, False)
    denied = await TelethonCommonChatsGateway(_Client(ChatAdminRequiredError(request=None))).fetch_common_chats(
        reference
    )
    assert denied.status is ProjectionStatus.UNAVAILABLE
    assert denied.reason == "not_an_admin"

    with pytest.raises(RuntimeError, match="temporary"):
        await TelethonCommonChatsGateway(_Client(RuntimeError("temporary"))).fetch_common_chats(reference)


def test_reference_lookup_is_sync_session_only() -> None:
    from mcp_telegram.telegram_gateway import TelethonChatAvatarHistoryGateway, TelethonUserProfileGateway

    user_client = SimpleNamespace(session=_SyncSession(types.InputPeerUser(7, 8)))
    user_gateway = TelethonUserProfileGateway(user_client)
    assert user_gateway.get_user_reference(7, is_self=False) == UserReference(7, 8, False)
    user_client.session.value = types.InputPeerUser(7, 9)
    assert user_gateway.get_user_reference(7, is_self=False) == UserReference(7, 9, False)
    assert user_gateway.get_user_reference(8, is_self=False) is None

    chat_client = SimpleNamespace(session=_SyncSession(types.InputPeerChat(3)))
    gateway = TelethonChatAvatarHistoryGateway(chat_client)
    assert gateway.get_chat_avatar_reference(-3) == GroupReference(-3)
    assert gateway.get_chat_avatar_reference(-4) is None


def test_private_section_payload_read_is_narrow_and_legacy_payload_has_no_context(tmp_path: Path) -> None:
    import json
    import sqlite3

    conn = sqlite3.connect(tmp_path / "profile.sqlite")
    conn.execute("CREATE TABLE entity_detail_sections (entity_id INTEGER, section TEXT, payload_json TEXT)")
    conn.execute(
        "INSERT INTO entity_detail_sections VALUES (?, ?, ?)",
        (7, "full_profile", json.dumps({"about": "old"})),
    )
    conn.commit()
    repo = EntityProfileRepository(conn, section_ttl_seconds=60)
    assert repo.read_section_payload(7, "full_profile") == {"about": "old"}
    assert repo.read_section_payload(8, "full_profile") is None
    conn.close()


def test_request_factories_and_chat_photo_types_have_one_production_owner() -> None:
    root = Path(__file__).parents[1] / "src" / "mcp_telegram"
    daemon_api = (root / "daemon_api.py").read_text()
    daemon_entity_info = (root / "daemon_entity_info.py").read_text()
    for source in (daemon_api, daemon_entity_info):
        assert "GetCommonChatsRequest" not in source
        assert "GetUserPhotosRequest" not in source
        assert "InputMessagesFilterChatPhotos" not in source
        assert "MessageActionChatEditPhoto" not in source
    owners = (root / "telegram_gateway.py").read_text()
    assert owners.count("GetCommonChatsRequest") == 2
    assert owners.count("GetUserPhotosRequest") == 2
    assert owners.count("InputMessagesFilterChatPhotos") == 1
    assert owners.count("MessageActionChatEditPhoto") == 1
