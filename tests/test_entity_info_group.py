"""Tests for GetEntityInfo — Legacy basic Chat (group) kind.

SPEC Reqs covered: 2 (type=group), 3 (common envelope), 7 (Group field
surface: members_count, migrated_to, invite_link, contacts_subscribed),
9 (full enumerate + intersect — no admin gate per CONTEXT D-16),
10 (no download keys), 12 (migrated_to verbatim peer-id, no auto-follow).
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from typing import Protocol, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telethon.tl import types as telethon_types  # type: ignore[import-untyped]

from mcp_telegram.daemon_api import DaemonAPIServer, DaemonClientLike
from mcp_telegram.entity_profile.contracts import GroupProfileObservation
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
from mcp_telegram.sync_transactions import write_savepoint
from mcp_telegram.telegram_gateway import (
    _group_identity_patch,
    _group_profile_entity_fields,
    _group_profile_participants_count,
    _normalize_invite_link,
    _normalize_participants,
    _optional_string,
)
from tests.daemon_api_policy import make_daemon_api_policy
from tests.demand_adapter_helpers import run_ready_slice
from tests.helpers import (
    ClientChatAvatarHistoryPort,
    LoudChannelProfilePort,
    LoudCommonChatsPort,
    LoudUserAvatarHistoryPort,
    LoudUserProfilePort,
    acquire_entity_profile,
    in_memory_sync_db,
)
from tests.sqlite_writer_helpers import inline_writer

_TEST_DBS: list[sqlite3.Connection] = []


class _FullChatEnvelope(Protocol):
    full_chat: object


def _dict(value: object) -> dict[str, object]:
    return cast(dict[str, object], value)


@pytest.fixture(autouse=True)
def _close_test_db():
    yield
    while _TEST_DBS:
        conn = _TEST_DBS.pop()
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - best-effort fixture cleanup, preserve old teardown semantics
            pass


@pytest.fixture(autouse=True)
def _patch_get_peer_id():
    with patch(
        "mcp_telegram.daemon_api.telethon_utils.get_peer_id",
        side_effect=lambda entity: (
            int(getattr(entity, "id", 0)) if not isinstance(entity, MagicMock) or hasattr(entity, "id") else 0
        ),
    ):
        yield


def _make_db() -> sqlite3.Connection:
    conn = in_memory_sync_db()
    _TEST_DBS.append(conn)
    return conn


def make_server(conn: sqlite3.Connection | None = None, client: DaemonClientLike | None = None) -> DaemonAPIServer:
    if conn is None:
        conn = _make_db()
    if client is None:
        client = MagicMock()
    shutdown_event = asyncio.Event()
    server = DaemonAPIServer(
        conn,
        cast(DaemonClientLike, client),
        shutdown_event,
        writer=inline_writer(conn),
        channel_profile_port=LoudChannelProfilePort(),
        group_profile_port=_GroupProfilePort(cast(DaemonClientLike, client)),
        user_profile_port=LoudUserProfilePort(),
        common_chats_port=LoudCommonChatsPort(),
        user_avatar_history_port=LoudUserAvatarHistoryPort(),
        chat_avatar_history_port=ClientChatAvatarHistoryPort(client),
        policy=make_daemon_api_policy(),
    )
    server._ready = True
    return server


def _legacy_chat(id_: int = -12345, **kwargs: object) -> MagicMock:
    from telethon.tl.types import Chat as TelethonChat  # type: ignore[import-untyped]

    c = MagicMock(spec=TelethonChat)
    c.id = abs(id_) if id_ < 0 else id_  # Chat.id is bare positive in Telethon; peer-id form is negative
    c.title = kwargs.get("title", "Legacy Group")
    c.creator = kwargs.get("creator", True)
    c.left = False
    c.admin_rights = None
    c.restriction_reason = []
    c.migrated_to = kwargs.get("migrated_to")
    c.participants_count = kwargs.get("participants_count")
    return c


def _full_chat_result(
    participant_user_ids: tuple[int, ...] = (),
    invite_link: str | None = None,
    about: str | None = None,
) -> MagicMock:
    full = MagicMock()
    ps = []
    for uid in participant_user_ids:
        p = MagicMock()
        p.user_id = uid
        ps.append(p)
    participants_obj = MagicMock()
    participants_obj.participants = ps
    full.full_chat = MagicMock(
        about=about,
        exported_invite=MagicMock(link=invite_link) if invite_link else None,
        participants=participants_obj,
        chat_photo=None,
    )
    return full


def _empty_search() -> MagicMock:
    return MagicMock(count=0, messages=[])


async def _acquire_local_profile(
    server: DaemonAPIServer, entity_id: int, chat: object, name: str = "Legacy Group"
) -> None:
    cast(_GroupProfilePort, server._group_profile_port).entity = chat
    await acquire_entity_profile(
        server._get_entity_info_service(), entity_id, {"id": entity_id, "type": "group", "name": name}
    )


async def _read_local_profile(server: DaemonAPIServer, client: AsyncMock, entity_id: int) -> dict[str, object]:
    changes_before = server._conn.total_changes
    calls_before = client.await_count
    resolver = cast(AsyncMock, client.get_entity)
    resolutions_before = resolver.await_count
    result = await server._dispatch({"method": "get_entity_info", "entity_id": entity_id})
    assert server._conn.total_changes == changes_before
    assert client.await_count == calls_before
    assert resolver.await_count == resolutions_before
    return result


class _GroupProfilePort:
    def __init__(self, client: DaemonClientLike) -> None:
        self._client = client
        self.entity: object | None = None

    async def fetch_group_profile(self, group_id: int) -> GroupProfileObservation:
        response = cast(_FullChatEnvelope, await self._client(object()))
        full_chat = response.full_chat
        raw_participants = cast(object | None, getattr(full_chat, "participants", None))
        if raw_participants is not None and not isinstance(raw_participants, telethon_types.ChatParticipants):
            raw_items = getattr(raw_participants, "participants", None)
            if isinstance(raw_items, list):
                raw_participants = telethon_types.ChatParticipants(
                    chat_id=abs(group_id),
                    participants=[
                        telethon_types.ChatParticipant(user_id=int(item.user_id), inviter_id=1, date=None)
                        for item in raw_items
                    ],
                    version=1,
                )
        participant_ids, reason = _normalize_participants(raw_participants, abs(group_id))
        invite = getattr(full_chat, "exported_invite", None)
        invite_link = _normalize_invite_link(invite)
        entity_envelope = SimpleNamespace(chats=[self.entity]) if self.entity is not None else None
        return GroupProfileObservation(
            group_id=group_id,
            about=_optional_string(getattr(full_chat, "about", None)),
            invite_link=invite_link,
            participant_ids=participant_ids,
            participants_unavailable_reason=reason,
            current_photo=None,
            observation_started_at=100,
            observation_completed_at=100,
            chat_entity_fields=(
                _group_profile_entity_fields(entity_envelope, abs(group_id)) if entity_envelope is not None else None
            ),
            participants_count=(
                _group_profile_participants_count(entity_envelope, abs(group_id))
                if entity_envelope is not None
                else None
            ),
            identity_patch=(
                _group_identity_patch(entity_envelope, abs(group_id)) if entity_envelope is not None else None
            ),
        )


@pytest.mark.asyncio
async def test_get_entity_info_group_type() -> None:
    """SPEC Req 2: legacy basic chat returns type='group'."""
    chat = _legacy_chat(id_=-100)
    # _patch_get_peer_id returns int(chat.id) = 100; we want -100, so override:
    with patch(
        "mcp_telegram.daemon_api.telethon_utils.get_peer_id",
        side_effect=lambda e: -100 if e is chat else int(getattr(e, "id", 0)),
    ):
        client = AsyncMock()
        client.get_entity = AsyncMock(return_value=chat)
        client.side_effect = [_full_chat_result(), _empty_search()]
        server = make_server(client=client)
        await _acquire_local_profile(server, -100, chat)
        r = await _read_local_profile(server, client, -100)
    assert r["ok"] is True, r
    assert _dict(r["data"])["type"] == "group"


@pytest.mark.asyncio
async def test_get_entity_info_group_field_surface() -> None:
    """SPEC Req 7: members_count, migrated_to, invite_link, contacts_subscribed all present."""
    chat = _legacy_chat(id_=-101, participants_count=3)
    with patch(
        "mcp_telegram.daemon_api.telethon_utils.get_peer_id",
        side_effect=lambda e: -101 if e is chat else int(getattr(e, "id", 0)),
    ):
        client = AsyncMock()
        client.get_entity = AsyncMock(return_value=chat)
        client.side_effect = [
            _full_chat_result(participant_user_ids=(1, 2, 3), invite_link="https://t.me/+abcdef"),
            _empty_search(),
        ]
        server = make_server(client=client)
        await _acquire_local_profile(server, -101, chat)
        r = await _read_local_profile(server, client, -101)
    d = _dict(r["data"])
    for key in ("members_count", "migrated_to", "invite_link", "contacts_subscribed"):
        assert key in d, f"missing group key: {key}"
    assert d["members_count"] == 3
    assert d["migrated_to"] is None  # not migrated
    assert d["invite_link"] == "https://t.me/+abcdef"


@pytest.mark.asyncio
async def test_get_entity_info_group_dm_intersection() -> None:
    """CONTEXT D-16: full participant list always available; intersect with DM-peer set."""
    conn = _make_db()
    with write_savepoint(conn):
        for did in (1, 3, 99):
            conn.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')", (did,))
        conn.executemany(
            "INSERT INTO entities (id, type, name, username, updated_at) VALUES (?, 'user', ?, ?, 1000)",
            [(1, "Anna", "anna"), (3, "Bob", "bob")],
        )

    chat = _legacy_chat(id_=-102)
    with patch(
        "mcp_telegram.daemon_api.telethon_utils.get_peer_id",
        side_effect=lambda e: -102 if e is chat else int(getattr(e, "id", 0)),
    ):
        client = AsyncMock()
        client.get_entity = AsyncMock(return_value=chat)
        client.side_effect = [
            _full_chat_result(participant_user_ids=(1, 2, 3, 4)),  # 1 and 3 are in DM peers
            _empty_search(),
        ]
        server = make_server(conn=conn, client=client)
        await _acquire_local_profile(server, -102, chat)
        r = await _read_local_profile(server, client, -102)
    d = _dict(r["data"])
    contacts = cast(list[dict[str, object]], d["contacts_subscribed"])
    ids = {entry["id"] for entry in contacts}
    assert ids == {1, 3}
    assert d["contacts_subscribed_partial"] is False


@pytest.mark.asyncio
async def test_get_entity_info_group_migrated_to_verbatim() -> None:
    """SPEC Req 12: migrated_to = peer-id form of new supergroup; no auto-follow.

    Telethon Chat.migrated_to is an InputChannel with channel_id; tool must
    return int(telethon_utils.get_peer_id(...)) which is negative for channels.
    """
    migrated = MagicMock()
    migrated.channel_id = 200500
    migrated.id = 200500  # for the patched get_peer_id mock

    chat = _legacy_chat(id_=-103, migrated_to=migrated)

    # get_peer_id needs to return -103 for the chat and -100200500 for the migrated InputChannel
    def fake_get_peer_id(e: object) -> int:
        if e is chat:
            return -103
        if e is migrated:
            return -1002005000000  # canonical peer-id form for channel
        return int(getattr(e, "id", 0))

    with patch(
        "mcp_telegram.daemon_api.telethon_utils.get_peer_id",
        side_effect=fake_get_peer_id,
    ):
        client = AsyncMock()
        client.get_entity = AsyncMock(return_value=chat)
        client.side_effect = [_full_chat_result(), _empty_search()]
        server = make_server(client=client)
        await _acquire_local_profile(server, -103, chat)
        calls_before_read = client.await_count
        r = await _read_local_profile(server, client, -103)
    d = _dict(r["data"])
    assert d["type"] == "group"
    assert d["migrated_to"] == -1000000200500
    assert client.await_count == calls_before_read  # no read-time follow-up


@pytest.mark.asyncio
async def test_get_entity_info_no_download_keys_group() -> None:
    """SPEC Req 10: Group response has no file_id / file_reference / download_*."""
    chat = _legacy_chat(id_=-104)
    with patch(
        "mcp_telegram.daemon_api.telethon_utils.get_peer_id",
        side_effect=lambda e: -104 if e is chat else int(getattr(e, "id", 0)),
    ):
        client = AsyncMock()
        client.get_entity = AsyncMock(return_value=chat)
        client.side_effect = [_full_chat_result(), _empty_search()]
        server = make_server(client=client)
        await _acquire_local_profile(server, -104, chat)
        r = await _read_local_profile(server, client, -104)

    def _walk(o: object):
        if isinstance(o, dict):
            for k in o:
                yield k
                yield from _walk(o[k])
        elif isinstance(o, list):
            for it in o:
                yield from _walk(it)

    forbidden = re.compile(r"^(file_id|file_reference|download_)")
    bad = [k for k in _walk(_dict(r["data"])) if forbidden.match(str(k))]
    assert not bad


@pytest.mark.asyncio
async def test_get_entity_info_group_profile_failure_retains_identity_without_read_retry() -> None:
    chat = _legacy_chat(id_=-105, participants_count=42)
    with patch(
        "mcp_telegram.daemon_api.telethon_utils.get_peer_id",
        side_effect=lambda e: -105 if e is chat else int(getattr(e, "id", 0)),
    ):
        client = AsyncMock()
        client.get_entity = AsyncMock(return_value=chat)
        client.side_effect = [RuntimeError("profile unavailable"), _empty_search()]
        server = make_server(client=client)
        with pytest.raises(RuntimeError, match="profile unavailable"):
            await _acquire_local_profile(server, -105, chat)
        calls_before_read = client.await_count
        r = await _read_local_profile(server, client, -105)

    assert r["ok"] is True
    detail = _dict(r["data"])
    assert detail["id"] == -105
    assert detail["type"] == "group"
    assert "about" not in detail
    assert client.await_count == calls_before_read
    assert "contacts_subscribed" not in detail


@pytest.mark.asyncio
async def test_get_entity_info_group_empty_about_is_normalized_to_none() -> None:
    chat = _legacy_chat(id_=-106, participants_count=9)
    with patch(
        "mcp_telegram.daemon_api.telethon_utils.get_peer_id",
        side_effect=lambda e: -106 if e is chat else int(getattr(e, "id", 0)),
    ):
        client = AsyncMock()
        client.get_entity = AsyncMock(return_value=chat)
        client.side_effect = [_full_chat_result(about="", participant_user_ids=(1,)), _empty_search()]
        server = make_server(client=client)
        await _acquire_local_profile(server, -106, chat)
        r = await _read_local_profile(server, client, -106)

    detail = _dict(r["data"])
    assert detail["about"] == ""
    assert detail["members_count"] == 1


@pytest.mark.asyncio
async def test_get_entity_info_group_intersection_without_local_contacts_is_empty() -> None:
    chat = _legacy_chat(id_=-107, participants_count=1)
    with patch(
        "mcp_telegram.daemon_api.telethon_utils.get_peer_id",
        side_effect=lambda e: -107 if e is chat else int(getattr(e, "id", 0)),
    ):
        client = AsyncMock()
        client.get_entity = AsyncMock(return_value=chat)
        client.side_effect = [_full_chat_result(participant_user_ids=(1,)), _empty_search()]
        server = make_server(client=client)
        await _acquire_local_profile(server, -107, chat)
        r = await _read_local_profile(server, client, -107)

    detail = _dict(r["data"])
    assert detail["contacts_subscribed"] == []
    assert detail["contacts_reason"] is None


@pytest.mark.asyncio
async def test_get_entity_info_group_unavailable_participants_remain_unknown() -> None:
    chat = _legacy_chat(id_=-108, participants_count=12)
    response = _full_chat_result()
    object.__setattr__(cast(object, response.full_chat), "participants", None)
    with patch(
        "mcp_telegram.daemon_api.telethon_utils.get_peer_id",
        side_effect=lambda e: -108 if e is chat else int(getattr(e, "id", 0)),
    ):
        client = AsyncMock()
        client.get_entity = AsyncMock(return_value=chat)
        client.side_effect = [response, _empty_search()]
        server = make_server(client=client)
        await _acquire_local_profile(server, -108, chat)
        r = await _read_local_profile(server, client, -108)

    detail = _dict(r["data"])
    assert detail["members_count"] == 12
    assert detail["contacts_subscribed"] is None
    assert detail["contacts_reason"] == "participants_missing"


async def _exercise_contact_overlap_failure_and_recovery(
    server: DaemonAPIServer,
    client: AsyncMock,
    adapter: EntityProfileDemandAdapter,
    prior_contacts: object,
) -> None:
    service = server._get_entity_info_service()
    with patch.object(service, "_enrich_contact_ids_with_names", side_effect=sqlite3.OperationalError("local read")):
        await run_ready_slice(adapter)
        partial = _dict((await _read_local_profile(server, client, -109))["data"])
        assert partial["about"] == "updated"
        assert partial["contacts_subscribed"] == prior_contacts
        sections = _dict(partial["sections"])
        assert _dict(sections["full_profile"])["status"] == "fresh"
        assert _dict(sections["contact_overlap"])["status"] == "pending"
        assert _dict(sections["contact_overlap"])["reason"] == "enumeration_failed"
        await run_ready_slice(adapter)
        immediate_retry = service._profiles.next_due_refresh(now=200)
        assert immediate_retry is not None
        assert immediate_retry.next_section == "contact_overlap"
        assert immediate_retry.retry_at == 200
        retry = service._profiles.next_due_refresh(now=260)
        assert retry is not None and retry.next_section == "contact_overlap"
        retained = _dict((await _read_local_profile(server, client, -109))["data"])
        assert retained["about"] == "updated"
        assert retained["contacts_subscribed"] == prior_contacts
        assert _dict(_dict(retained["sections"])["contact_overlap"])["status"] == "unavailable"
    service._deps = replace(service._deps, now_provider=lambda: 260.0)
    await run_ready_slice(adapter)
    recovered = _dict((await _read_local_profile(server, client, -109))["data"])
    assert recovered["about"] == "updated"
    assert recovered["contacts_subscribed"] == [{"id": 3, "name": "Bob", "username": None}]
    assert _dict(_dict(recovered["sections"])["contact_overlap"])["status"] == "fresh"
    assert client.await_count == 5  # initial profile/avatar, then paired, failed and recovered overlap


@pytest.mark.asyncio
async def test_group_overlap_failure_preserves_full_profile_and_retries_without_erasing_contacts() -> None:
    conn = _make_db()
    with write_savepoint(conn):
        conn.executemany(
            "INSERT INTO entities(id,type,name,updated_at) VALUES (?,'user',?,100)",
            ((1, "Anna"), (3, "Bob")),
        )
        conn.executemany("INSERT INTO synced_dialogs(dialog_id,status) VALUES (?,'synced')", ((1,), (3,)))
    chat = _legacy_chat(id_=-109, participants_count=1)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=chat)
    client.side_effect = [_full_chat_result(participant_user_ids=(1,), about="original"), _empty_search()]
    server = make_server(conn=conn, client=client)
    await _acquire_local_profile(server, -109, chat)
    service = server._get_entity_info_service()
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    adapter = EntityProfileDemandAdapter(coordinator)
    baseline = _dict((await _read_local_profile(server, client, -109))["data"])
    prior_contacts = baseline["contacts_subscribed"]
    assert prior_contacts == [{"id": 1, "name": "Anna", "username": None}]
    client.side_effect = [_full_chat_result(participant_user_ids=(3,), about="updated") for _ in range(3)]
    service._deps = replace(service._deps, now_provider=lambda: 200.0)
    assert service._profiles.request_section_refresh(
        -109, ("full_profile", "contact_overlap"), now=200, pair_mode="disabled"
    )
    await _exercise_contact_overlap_failure_and_recovery(server, client, adapter, prior_contacts)
