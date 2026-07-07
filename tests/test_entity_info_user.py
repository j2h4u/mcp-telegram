"""Tests for GetEntityInfo / _get_entity_info — User and Bot kinds.

SPEC Reqs covered: 1 (registration smoke via dispatch), 2 (type discriminator
for user/bot), 3 (common envelope), 4 (User/Bot field surface fully preserved),
10 (no file_id / file_reference / download_*).
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# HIGH-1 from 47-REVIEWS.md cycle 3 (codex 2026-04-25): User/Bot fixtures
# MUST construct mocks with spec=User so `isinstance(entity, User)` inside
# `_classify_dialog_type()` returns True. Without spec=User, plain
# MagicMock() never satisfies the isinstance check and the User branch is
# silently not exercised. Module-level import keeps the spec= reference
# cheap (no per-test imports).
from telethon.tl.types import Channel, User  # type: ignore[import-untyped]

from mcp_telegram.daemon_api import DaemonAPIServer, _DaemonClientLike

_TEST_DBS: list[sqlite3.Connection] = []


def _dict(value: object) -> dict[str, object]:
    return cast(dict[str, object], value)


def _dict_at(value: object, *keys: str) -> dict[str, object]:
    current = _dict(value)
    for key in keys:
        current = _dict(current[key])
    return current


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
        side_effect=lambda entity: int(getattr(entity, "id", 0)),
    ):
        yield


def _make_db() -> sqlite3.Connection:
    """Return an in-memory SQLite at v16 (entity_details present)."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE synced_dialogs (
            dialog_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'not_synced',
            last_synced_at INTEGER, last_event_at INTEGER,
            sync_progress INTEGER DEFAULT 0, total_messages INTEGER,
            access_lost_at INTEGER, read_inbox_max_id INTEGER, read_outbox_max_id INTEGER
        );
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


def make_server(conn: sqlite3.Connection | None = None, client: _DaemonClientLike | None = None) -> DaemonAPIServer:
    if conn is None:
        conn = _make_db()
    if client is None:
        client = MagicMock()
    shutdown_event = asyncio.Event()
    server = DaemonAPIServer(conn, cast(_DaemonClientLike, client), shutdown_event)
    server._ready = True
    return server


def _make_user_mock(**kwargs: object) -> MagicMock:
    # HIGH-1 from 47-REVIEWS.md cycle 3 (codex 2026-04-25): spec=User makes
    # `isinstance(u, User)` return True so `_classify_dialog_type()` actually
    # routes through the User branch. Plain MagicMock() (the cycle-3 bug)
    # silently bypasses the User branch — the same root cause as the
    # cycle-2 HIGH that was fixed for Channel. Note: Telegram bots are also
    # represented by `telethon.tl.types.User` instances with `bot=True` —
    # there is no separate `Bot` class, so `spec=User` is correct for both
    # user and bot fixtures.
    u = MagicMock(spec=User)
    u.id = kwargs.get("id", 12345)
    u.first_name = kwargs.get("first_name", "Alice")
    u.last_name = kwargs.get("last_name", "Smith")
    u.username = kwargs.get("username", "alice")
    u.bot = kwargs.get("bot", False)
    u.contact = kwargs.get("contact", False)
    u.mutual_contact = kwargs.get("mutual_contact", False)
    u.close_friend = kwargs.get("close_friend", False)
    u.verified = u.premium = u.scam = u.fake = u.restricted = False
    u.phone = kwargs.get("phone")
    u.lang_code = kwargs.get("lang_code")
    u.usernames = []
    u.emoji_status = None
    u.restriction_reason = []
    u.send_paid_messages_stars = None
    u.status = None
    return u


def _make_channel_mock(**kwargs: object) -> MagicMock:
    channel = MagicMock(spec=Channel)
    channel.id = kwargs.get("id", 777)
    channel.title = kwargs.get("title", "Personal Lab")
    channel.username = kwargs.get("username", "personal_lab")
    channel.megagroup = False
    channel.broadcast = True
    channel.forum = False
    return channel


@pytest.mark.asyncio
async def test_get_entity_info_user_type() -> None:
    """SPEC Req 2: User entity (bot=False) returns type='user'."""
    user = _make_user_mock(id=1, bot=False)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=user)
    common, full, photos = MagicMock(), MagicMock(), MagicMock()
    common.chats = []
    full.full_user = MagicMock(
        about=None,
        personal_channel_id=None,
        birthday=None,
        blocked=False,
        ttl_period=None,
        private_forward_name=None,
        bot_info=None,
        business_location=None,
        business_intro=None,
        business_work_hours=None,
        note=None,
        folder_id=None,
    )
    photos.count = 0
    photos.photos = []
    client.side_effect = [common, full, photos]
    server = make_server(client=client)
    with (
        patch("mcp_telegram.daemon_api.GetCommonChatsRequest"),
        patch("mcp_telegram.daemon_api.GetFullUserRequest"),
        patch("mcp_telegram.daemon_api.GetUserPhotosRequest"),
    ):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": 1})
    assert r["ok"] is True, f"got {r}"
    assert _dict(r["data"])["type"] == "user"


@pytest.mark.asyncio
async def test_get_entity_info_user_avatar_history_handles_photos_without_count() -> None:
    """User avatar collection should tolerate Photos-like results without count."""
    user = _make_user_mock(id=11, bot=False)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=user)
    common, full, photos = MagicMock(), MagicMock(), MagicMock()
    common.chats = []
    full.full_user = MagicMock(
        about=None,
        personal_channel_id=None,
        birthday=None,
        blocked=False,
        ttl_period=None,
        private_forward_name=None,
        bot_info=None,
        business_location=None,
        business_intro=None,
        business_work_hours=None,
        note=None,
        folder_id=None,
    )
    photo = MagicMock(id=77, date=datetime(2026, 6, 23, 12, 0, tzinfo=UTC))
    del photos.count
    photos.photos = [photo]
    client.side_effect = [common, full, photos]
    server = make_server(client=client)
    with (
        patch("mcp_telegram.daemon_api.GetCommonChatsRequest"),
        patch("mcp_telegram.daemon_api.GetFullUserRequest"),
        patch("mcp_telegram.daemon_api.GetUserPhotosRequest"),
    ):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": 11})
    assert r["ok"] is True, f"got {r}"
    d = _dict(r["data"])
    assert d["avatar_count"] == 1
    assert d["avatar_history"] == [{"photo_id": 77, "date": "2026-06-23T12:00:00+00:00"}]


@pytest.mark.asyncio
async def test_get_entity_info_bot_type() -> None:
    """SPEC Req 2 + CONTEXT D-08: User entity with bot=True returns type='bot'."""
    bot = _make_user_mock(id=2, bot=True, first_name="MyBot")
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=bot)
    common, full, photos = MagicMock(), MagicMock(), MagicMock()
    common.chats = []
    full.full_user = MagicMock(
        about=None,
        personal_channel_id=None,
        birthday=None,
        blocked=False,
        ttl_period=None,
        private_forward_name=None,
        bot_info=None,
        business_location=None,
        business_intro=None,
        business_work_hours=None,
        note=None,
        folder_id=None,
    )
    photos.count = 0
    photos.photos = []
    client.side_effect = [common, full, photos]
    server = make_server(client=client)
    with (
        patch("mcp_telegram.daemon_api.GetCommonChatsRequest"),
        patch("mcp_telegram.daemon_api.GetFullUserRequest"),
        patch("mcp_telegram.daemon_api.GetUserPhotosRequest"),
    ):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": 2})
    assert r["ok"] is True
    d = _dict(r["data"])
    assert d["type"] == "bot"
    assert d["bot"] is True


@pytest.mark.asyncio
async def test_get_entity_info_common_envelope_user() -> None:
    """SPEC Req 3: User response carries the common envelope keys."""
    user = _make_user_mock(id=3)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=user)
    common, full, photos = MagicMock(), MagicMock(), MagicMock()
    common.chats = []
    full.full_user = MagicMock(
        about="bio text",
        personal_channel_id=None,
        birthday=None,
        blocked=False,
        ttl_period=None,
        private_forward_name=None,
        bot_info=None,
        business_location=None,
        business_intro=None,
        business_work_hours=None,
        note=None,
        folder_id=None,
    )
    photos.count = 0
    photos.photos = []
    client.side_effect = [common, full, photos]
    server = make_server(client=client)
    with (
        patch("mcp_telegram.daemon_api.GetCommonChatsRequest"),
        patch("mcp_telegram.daemon_api.GetFullUserRequest"),
        patch("mcp_telegram.daemon_api.GetUserPhotosRequest"),
    ):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": 3})
    assert r["ok"] is True
    d = _dict(r["data"])
    for key in ("id", "type", "name", "username", "about", "my_membership", "avatar_history", "avatar_count"):
        assert key in d, f"missing common envelope key: {key}"
    assert d["about"] == "bio text"
    assert isinstance(d["my_membership"], dict)


@pytest.mark.asyncio
async def test_get_entity_info_user_field_surface_preserved() -> None:
    """SPEC Req 4: User payload preserves every field the prior user-info tool carried."""
    user = _make_user_mock(id=4, phone="+12025551234", lang_code="en")
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=user)
    common, full, photos = MagicMock(), MagicMock(), MagicMock()
    common.chats = []
    full.full_user = MagicMock(
        about=None,
        personal_channel_id=999,
        birthday=None,
        blocked=False,
        ttl_period=86400,
        private_forward_name=None,
        bot_info=None,
        business_location=None,
        business_intro=None,
        business_work_hours=None,
        note=None,
        folder_id=None,
    )
    photos.count = 0
    photos.photos = []
    client.side_effect = [common, full, photos]
    server = make_server(client=client)
    with (
        patch("mcp_telegram.daemon_api.GetCommonChatsRequest"),
        patch("mcp_telegram.daemon_api.GetFullUserRequest"),
        patch("mcp_telegram.daemon_api.GetUserPhotosRequest"),
    ):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": 4})
    d = _dict(r["data"])
    # Every field the prior user-info data dict carried — see daemon_api.py history.
    for key in (
        "first_name",
        "last_name",
        "extra_usernames",
        "emoji_status_id",
        "status",
        "phone",
        "lang_code",
        "contact",
        "mutual_contact",
        "close_friend",
        "send_paid_messages_stars",
        "personal_channel_id",
        "birthday",
        "verified",
        "premium",
        "bot",
        "scam",
        "fake",
        "restricted",
        "restriction_reason",
        "blocked",
        "ttl_period",
        "private_forward_name",
        "bot_info",
        "business_location",
        "business_intro",
        "business_work_hours",
        "note",
        "folder_id",
        "folder_name",
        "common_chats",
    ):
        assert key in d, f"User field surface regression: missing {key}"
    assert d["phone"] == "+12025551234"
    assert d["lang_code"] == "en"
    assert d["personal_channel_id"] == 999
    assert d["ttl_period"] == 86400


@pytest.mark.asyncio
async def test_get_entity_info_user_personal_channel_card_from_full_user_chats() -> None:
    user = _make_user_mock(id=44)
    channel = _make_channel_mock(id=777, title="Deep Reality Notes", username="deep_reality")
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=user)
    long_text = "0123456789" * 12
    attached_message = MagicMock()
    attached_message.message = long_text
    attached_message.date = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)
    get_messages = AsyncMock(return_value=[attached_message])
    client.get_messages = get_messages
    common, full, photos = MagicMock(), MagicMock(), MagicMock()
    common.chats = []
    full.full_user = MagicMock(
        about=None,
        personal_channel_id=777,
        personal_channel_message=55,
        birthday=None,
        blocked=False,
        ttl_period=None,
        private_forward_name=None,
        bot_info=None,
        business_location=None,
        business_intro=None,
        business_work_hours=None,
        note=None,
        folder_id=None,
    )
    full.chats = [channel]
    photos.count = 0
    photos.photos = []
    client.side_effect = [common, full, photos]
    server = make_server(client=client)

    with (
        patch("mcp_telegram.daemon_api.GetCommonChatsRequest"),
        patch("mcp_telegram.daemon_api.GetFullUserRequest"),
        patch("mcp_telegram.daemon_api.GetUserPhotosRequest"),
    ):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": 44})

    d = _dict(r["data"])
    personal_channel = _dict(d["personal_channel"])
    assert personal_channel == {
        "channel_id": 777,
        "dialog_id": -1000000000777,
        "title": "Deep Reality Notes",
        "username": "deep_reality",
        "url": "https://t.me/deep_reality",
        "metadata_source": "user_full_chats",
        "attached_message_id": 55,
        "latest_or_attached_post": {
            "source": "personal_channel_message",
            "message_id": 55,
            "sent_at": 1782216000,
            "text_preview": long_text[:100],
            "char_count": len(long_text),
            "is_truncated": True,
        },
    }
    get_messages.assert_awaited_once_with(channel, ids=[55])


@pytest.mark.asyncio
async def test_get_entity_info_user_personal_channel_falls_back_to_local_latest_post() -> None:
    conn = _make_db()
    conn.execute(
        """CREATE TABLE messages (
            dialog_id INTEGER,
            message_id INTEGER,
            sent_at INTEGER,
            text TEXT,
            is_deleted INTEGER DEFAULT 0,
            is_service INTEGER DEFAULT 0
        )"""
    )
    conn.execute(
        "INSERT INTO entities (id, type, name, username, updated_at) VALUES (?, 'channel', ?, ?, 1700000000)",
        (-1000000000777, "Local Channel", "local_channel"),
    )
    conn.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at, text, is_deleted, is_service) VALUES (?, 10, 100, ?, 0, 0)",
        (-1000000000777, "older local post"),
    )
    conn.execute(
        "INSERT INTO messages (dialog_id, message_id, sent_at, text, is_deleted, is_service) VALUES (?, 11, 200, ?, 0, 0)",
        (-1000000000777, "latest local post"),
    )

    user = _make_user_mock(id=45)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=user)
    get_messages = AsyncMock()
    client.get_messages = get_messages
    common, full, photos = MagicMock(), MagicMock(), MagicMock()
    common.chats = []
    full.full_user = MagicMock(
        about=None,
        personal_channel_id=777,
        personal_channel_message=None,
        birthday=None,
        blocked=False,
        ttl_period=None,
        private_forward_name=None,
        bot_info=None,
        business_location=None,
        business_intro=None,
        business_work_hours=None,
        note=None,
        folder_id=None,
    )
    full.chats = []
    photos.count = 0
    photos.photos = []
    client.side_effect = [common, full, photos]
    server = make_server(conn=conn, client=client)

    with (
        patch("mcp_telegram.daemon_api.GetCommonChatsRequest"),
        patch("mcp_telegram.daemon_api.GetFullUserRequest"),
        patch("mcp_telegram.daemon_api.GetUserPhotosRequest"),
    ):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": 45})

    d = _dict(r["data"])
    personal_channel = _dict(d["personal_channel"])
    assert personal_channel["title"] == "Local Channel"
    assert personal_channel["username"] == "local_channel"
    assert personal_channel["metadata_source"] == "local_entities"
    assert personal_channel["latest_or_attached_post"] == {
        "source": "local_latest_message",
        "message_id": 11,
        "sent_at": 200,
        "text_preview": "latest local post",
        "char_count": 17,
        "is_truncated": False,
    }
    assert get_messages.await_count == 0


@pytest.mark.asyncio
async def test_get_entity_info_no_download_keys_user() -> None:
    """SPEC Req 10: response must contain no file_id / file_reference / download_* keys."""
    user = _make_user_mock(id=5)
    client = AsyncMock()
    client.get_entity = AsyncMock(return_value=user)
    common, full, photos = MagicMock(), MagicMock(), MagicMock()
    common.chats = []
    full.full_user = MagicMock(
        about=None,
        personal_channel_id=None,
        birthday=None,
        blocked=False,
        ttl_period=None,
        private_forward_name=None,
        bot_info=None,
        business_location=None,
        business_intro=None,
        business_work_hours=None,
        note=None,
        folder_id=None,
    )
    # Photo mock with id+date AND additional bytes-like attrs that must NOT leak.
    photo = MagicMock()
    photo.id = 10001
    photo.date = MagicMock(isoformat=lambda: "2024-01-01T00:00:00")
    photos.count = 1
    photos.photos = [photo]
    client.side_effect = [common, full, photos]
    server = make_server(client=client)
    with (
        patch("mcp_telegram.daemon_api.GetCommonChatsRequest"),
        patch("mcp_telegram.daemon_api.GetFullUserRequest"),
        patch("mcp_telegram.daemon_api.GetUserPhotosRequest"),
    ):
        r = await server._dispatch({"method": "get_entity_info", "entity_id": 5})

    def _walk_keys(o: object):
        if isinstance(o, dict):
            for k in o:
                yield k
                yield from _walk_keys(o[k])
        elif isinstance(o, list):
            for it in o:
                yield from _walk_keys(it)

    forbidden = re.compile(r"^(file_id|file_reference|download_)")
    bad = [k for k in _walk_keys(_dict(r["data"])) if forbidden.match(str(k))]
    assert not bad, f"forbidden download-related keys present: {bad}"
    d = _dict(r["data"])
    assert d["avatar_history"] == [{"photo_id": 10001, "date": "2024-01-01T00:00:00"}]
    assert d["avatar_count"] == 1


@pytest.mark.asyncio
async def test_get_entity_info_entity_not_found() -> None:
    """CONTEXT D-10: ValueError from get_entity → ok=false, error='entity_not_found'."""
    client = AsyncMock()
    client.get_entity = AsyncMock(side_effect=ValueError("No entity 99999"))
    server = make_server(client=client)
    r = await server._dispatch({"method": "get_entity_info", "entity_id": 99999})
    assert r["ok"] is False
    assert r["error"] == "entity_not_found"
    assert r["data"] is None


@pytest.mark.asyncio
async def test_get_entity_info_dispatcher_route() -> None:
    """SPEC Req 1 (smoke): _dispatch routes 'get_entity_info' to _get_entity_info."""
    client = AsyncMock()
    client.get_entity = AsyncMock(side_effect=ValueError("smoke"))
    server = make_server(client=client)
    # The unknown_method fallback would return error='unknown_method', not entity_not_found.
    r = await server._dispatch({"method": "get_entity_info", "entity_id": 1})
    assert r["error"] == "entity_not_found", "must route, not fall to unknown_method"


@pytest.mark.asyncio
async def test_old_entity_dispatch_route_removed() -> None:
    """CONTEXT D-11: atomic removal — legacy dispatch route must be gone, unknown_method returned."""
    server = make_server()
    old_route = "get_" + "user_info"  # literal avoided; this string must not appear in source
    r = await server._dispatch({"method": old_route, "user_id": 1})
    assert r["ok"] is False
    assert r["error"] == "unknown_method"


def test_dm_peer_ids_excludes_access_lost() -> None:
    """LOW-1 from 47-REVIEWS.md: _dm_peer_ids skips access-lost DM peers.

    Operator no longer 'knows' someone whose chat was deleted or who blocked
    them. The synced_dialogs.status='access_lost' rows must NOT appear in
    the contacts_subscribed denominator.

    HIGH-B fix from 47-REVIEWS.md cycle 2 (2026-04-25): the INSERT
    statements MUST NOT reference a `dialog_type` column — the production
    `synced_dialogs` schema does not have one (verify in
    src/mcp_telegram/sync_db.py), and the `_dm_peer_ids` query does not
    filter on it. Including `dialog_type` in the INSERT raises
    sqlite3.OperationalError: table synced_dialogs has no column named
    dialog_type. The dialog_id sign + status filter alone are
    authoritative: positive ids are DM/User peers (Telethon convention),
    negative ids are groups/channels.
    """
    server = make_server()
    # Insert two DM peers: one synced, one access_lost.
    server._conn.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')", (111,))
    server._conn.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'access_lost')", (222,))
    # Negative dialog_id (group/channel) must also stay excluded by the
    # dialog_id > 0 clause regardless of status — sanity check.
    server._conn.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')", (-1001,))
    peers = server._dm_peer_ids()
    assert peers == {111}, f"expected only the synced DM peer, got {peers}"
