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
from typing import Protocol, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_telegram.daemon_api import DaemonAPIServer, DaemonClientLike
from mcp_telegram.entity_profile.contracts import GroupProfileObservation
from tests.daemon_api_policy import make_daemon_api_policy
from tests.reaction_helpers import make_reaction_freshener

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
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE synced_dialogs (
            dialog_id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'not_synced',
            last_synced_at INTEGER, last_event_at INTEGER,
            sync_progress INTEGER DEFAULT 0, total_messages INTEGER,
            access_lost_at INTEGER, read_inbox_max_id INTEGER, read_outbox_max_id INTEGER
        );
        CREATE TABLE full_history_enrollment (
            dialog_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
            source TEXT NOT NULL CHECK(source IN ('explicit', 'automatic', 'migration')),
            updated_at INTEGER NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT,
            username TEXT, name_normalized TEXT, updated_at INTEGER NOT NULL
        );
        CREATE TABLE entity_details (
            entity_id INTEGER PRIMARY KEY, detail_json TEXT NOT NULL, fetched_at INTEGER NOT NULL,
            FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
        ) WITHOUT ROWID;
        CREATE INDEX idx_entity_details_fetched_at ON entity_details(fetched_at);
        """
    )
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
        reaction_freshener=make_reaction_freshener(conn, client),
        group_profile_port=_GroupProfilePort(cast(DaemonClientLike, client)),
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


class _GroupProfilePort:
    def __init__(self, client: DaemonClientLike) -> None:
        self._client = client

    async def fetch_group_profile(self, group_id: int) -> GroupProfileObservation:
        response = cast(_FullChatEnvelope, await self._client(object()))
        full_chat = response.full_chat
        raw_participants = getattr(full_chat, "participants", None)
        if raw_participants is None:
            participant_ids, reason = None, "participants_missing"
        else:
            participants = getattr(cast(object, raw_participants), "participants", None)
            participant_ids, reason = tuple(int(item.user_id) for item in (participants or [])), None
        invite = getattr(full_chat, "exported_invite", None)
        invite_link = getattr(cast(object, invite), "link", None) if invite is not None else None
        return GroupProfileObservation(
            group_id=group_id,
            about=getattr(full_chat, "about", None),
            invite_link=invite_link,
            participant_ids=participant_ids,
            participants_unavailable_reason=reason,
            current_photo=None,
            observation_started_at=100,
            observation_completed_at=100,
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
        with patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
            r = await server._dispatch({"method": "get_entity_info", "entity_id": -100})
    assert r["ok"] is True, r
    assert _dict(r["data"])["type"] == "group"


@pytest.mark.asyncio
async def test_get_entity_info_group_field_surface() -> None:
    """SPEC Req 7: members_count, migrated_to, invite_link, contacts_subscribed all present."""
    chat = _legacy_chat(id_=-101)
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
        with patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
            r = await server._dispatch({"method": "get_entity_info", "entity_id": -101})
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
    for did in (1, 3, 99):
        conn.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')", (did,))
    conn.executemany(
        "INSERT INTO entities (id, type, name, username, updated_at) VALUES (?, 'user', ?, ?, 1000)",
        [(1, "Anna", "anna"), (3, "Bob", "bob")],
    )
    conn.commit()

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
        with patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
            r = await server._dispatch({"method": "get_entity_info", "entity_id": -102})
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
        with patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
            r = await server._dispatch({"method": "get_entity_info", "entity_id": -103})
    d = _dict(r["data"])
    assert d["type"] == "group"
    assert d["migrated_to"] == -1002005000000
    # SPEC Req 12 + RESEARCH: no auto-follow code path. Verify by source-grep:
    import inspect

    from mcp_telegram import daemon_entity_info as di

    src = inspect.getsource(di.DaemonEntityInfoService._fetch_group_detail)
    # No "follow" or "redirect" or recursive call to _get_entity_info inside the helper
    assert "follow_migrated" not in src
    assert "_get_entity_info(" not in src
    assert "redirect" not in src.lower()


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
        with patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
            r = await server._dispatch({"method": "get_entity_info", "entity_id": -104})

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
async def test_get_entity_info_group_profile_failure_keeps_legacy_degraded_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    chat = _legacy_chat(id_=-105, participants_count=42)
    with patch(
        "mcp_telegram.daemon_api.telethon_utils.get_peer_id",
        side_effect=lambda e: -105 if e is chat else int(getattr(e, "id", 0)),
    ):
        client = AsyncMock()
        client.get_entity = AsyncMock(return_value=chat)
        client.side_effect = [RuntimeError("profile unavailable"), _empty_search()]
        server = make_server(client=client)
        with caplog.at_level("WARNING"), patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
            r = await server._dispatch({"method": "get_entity_info", "entity_id": -105})

    assert r["ok"] is True
    detail = _dict(r["data"])
    assert detail["about"] is None
    assert detail["members_count"] == 42
    assert detail["contacts_subscribed"] == []
    assert detail["contacts_reason"] is None
    assert "entity_info group full_profile_failed" in caplog.text
    assert server._entity_info_service is not None
    assert "full_profile" in server._entity_info_service._section_failures


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
        with patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
            r = await server._dispatch({"method": "get_entity_info", "entity_id": -106})

    detail = _dict(r["data"])
    assert detail["about"] is None
    assert detail["members_count"] == 1


@pytest.mark.asyncio
async def test_get_entity_info_group_contact_enrichment_failure_is_degraded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    chat = _legacy_chat(id_=-107, participants_count=1)
    with patch(
        "mcp_telegram.daemon_api.telethon_utils.get_peer_id",
        side_effect=lambda e: -107 if e is chat else int(getattr(e, "id", 0)),
    ):
        client = AsyncMock()
        client.get_entity = AsyncMock(return_value=chat)
        client.side_effect = [_full_chat_result(participant_user_ids=(1,)), _empty_search()]
        server = make_server(client=client)
        service = server._get_entity_info_service()
        with (
            caplog.at_level("WARNING"),
            patch.object(service, "_enrich_contact_ids_with_names", side_effect=sqlite3.OperationalError("db")),
            patch("mcp_telegram.daemon_api.MessagesSearchRequest"),
        ):
            r = await server._dispatch({"method": "get_entity_info", "entity_id": -107})

    detail = _dict(r["data"])
    assert detail["contacts_subscribed"] is None
    assert detail["contacts_reason"] == "enumeration_failed"
    assert "entity_info group contacts_intersect_failed" in caplog.text
    assert "contact_overlap" in service._section_failures


@pytest.mark.asyncio
async def test_get_entity_info_group_unavailable_participants_use_legacy_empty_contacts() -> None:
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
        with patch("mcp_telegram.daemon_api.MessagesSearchRequest"):
            r = await server._dispatch({"method": "get_entity_info", "entity_id": -108})

    detail = _dict(r["data"])
    assert detail["members_count"] == 12
    assert detail["contacts_subscribed"] == []
    assert detail["contacts_reason"] is None
