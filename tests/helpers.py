"""Shared test helpers for sync/event/delta tests."""

# Telethon-shaped MagicMock fixtures intentionally expose dynamic attributes.
# pyright: reportAny=false

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, TypedDict, Unpack, cast

from mcp_telegram import telegram_rpc
from mcp_telegram.entity_profile.contracts import (
    PROFILE_SECTIONS,
    ChannelContactOverlapObservation,
    ChannelProfileObservation,
    ChannelReference,
    ChatAvatarHistoryObservation,
    ChatAvatarReference,
    ChatCurrentPhoto,
    CommonChatsObservation,
    CommonChatSummary,
    GroupProfileObservation,
    GroupReference,
    ObservationBoundary,
    ProfileCollectionItem,
    ProfileCollectionPage,
    ProfileFieldObservation,
    ProjectionOutcome,
    ProjectionStatus,
    TargetKind,
    UserAvatarHistoryObservation,
    UserProfileObservation,
    UserReference,
)
from mcp_telegram.entity_profile.repository import EntityProfileRepository, EntitySectionCommit
from mcp_telegram.observation_order import allocate_observation_order
from mcp_telegram.sync_db import _apply_migrations
from mcp_telegram.sync_transactions import enable_runtime_writes, write_savepoint
from tests.demand_adapter_helpers import run_ready_slice

if TYPE_CHECKING:
    from mcp_telegram.daemon_entity_info import DaemonEntityInfoService


def in_memory_sync_db() -> sqlite3.Connection:
    """Return a current-schema in-memory database with runtime writes guarded."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    _apply_migrations(conn)
    enable_runtime_writes(conn)
    return conn


def seed_entity_profile(
    conn: sqlite3.Connection,
    entity_id: int,
    detail: dict[str, object],
    *,
    owner_account_id: int = 1,
    observed_at: int = 100,
) -> None:
    """Seed a collected canonical profile for local projection tests."""
    entity_type = str(detail["type"])
    with write_savepoint(conn):
        conn.execute(
            "INSERT INTO entities(id,type,name,username,updated_at) VALUES (?,?,?,?,?)",
            (entity_id, entity_type, detail.get("name"), detail.get("username"), observed_at),
        )
        conn.execute(
            "INSERT INTO entity_details(entity_id,detail_json,fetched_at,profile_owner_account_id) VALUES (?,?,?,?)",
            (entity_id, json.dumps({"schema": 1, **detail}), observed_at, owner_account_id),
        )
        conn.executemany(
            "INSERT INTO entity_detail_sections(entity_id,section,status,observed_at,payload_json) "
            "VALUES (?,?,'fresh',?,'{}')",
            ((entity_id, section, observed_at) for section in PROFILE_SECTIONS),
        )


async def acquire_entity_profile(service: DaemonEntityInfoService, entity_id: int, detail: dict[str, object]) -> None:
    """Run the service's durable adapter for one fixture entity to completion."""
    from unittest.mock import MagicMock

    from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter

    bind_profile_test_account(service)
    service._deps = replace(service._deps, now_provider=lambda: 100.0, enable_full_user_pair=False)
    service.bind_demand_sink(MagicMock())
    await service._write_profiles(lambda profiles: profiles.save_core(detail, now=100))
    await service._write_profiles(
        lambda profiles: profiles.mark_pending(
            entity_id, now=100, pair_eligible_override=False, pair_mode_override="disabled"
        )
    )
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    adapter = EntityProfileDemandAdapter(coordinator)
    for _ in range(len(PROFILE_SECTIONS) + 1):
        await run_ready_slice(adapter, now=100.0)
        if service._profiles.next_due_refresh(now=100) is None:
            break


def finish_empty_profile_collections(repo: EntityProfileRepository, entity_id: int, *, now: int) -> None:
    """Commit the four additional empty collection pages in a repository fixture."""
    for section in PROFILE_SECTIONS[5:]:
        cursor = repo.next_due_refresh(now=now)
        assert cursor is not None and cursor.entity_id == entity_id and cursor.next_section == section
        page = ProfileCollectionPage(entity_id, section, (), ObservationBoundary(now, now), 0, terminal=True)
        observation = ProfileFieldObservation(7, allocate_observation_order(repo._conn), now, "rpc")
        assert repo.commit_section(
            cursor, EntitySectionCommit({}, collection_page=page, field_observation=observation), now=now
        )


def bind_profile_test_account(service: DaemonEntityInfoService) -> None:
    """Bind fixture-owned private facts to a stable fake Telegram account."""
    from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope

    scope = TelegramAuthScope(AUTH_SCOPE_VERSION, 1, 2, 99)
    service._deps = replace(
        service._deps,
        self_id=1,
        full_user_auth_scope=lambda: scope,
    )


class _BuildMockMessageKwargs(TypedDict, total=False):
    text: str | None
    sender_id: int | None
    sender_first_name: str | None
    media: object | None
    reply_to_msg_id: int | None
    forum_topic: bool
    reply_to_top_id: int | None
    message_thread_id: int | None
    is_topic_message: bool
    reply_count: int
    reactions: object | None
    edit_date: datetime | None


def build_mock_message(
    id: int,
    **kwargs: Unpack[_BuildMockMessageKwargs],
) -> SimpleNamespace:
    """Build a minimal Telethon-like message object."""
    text = kwargs.get("text", "hello")
    sender_id = kwargs.get("sender_id", 42)
    sender_first_name = kwargs.get("sender_first_name", "Alice")
    media = kwargs.get("media")
    reply_to_msg_id = kwargs.get("reply_to_msg_id")
    forum_topic = kwargs.get("forum_topic", False)
    reply_to_top_id = kwargs.get("reply_to_top_id")
    message_thread_id = kwargs.get("message_thread_id")
    is_topic_message = kwargs.get("is_topic_message", False)
    reply_count = kwargs.get("reply_count", 0)
    reactions = kwargs.get("reactions")
    edit_date = kwargs.get("edit_date")
    sender = SimpleNamespace(first_name=sender_first_name) if sender_first_name is not None else None

    reply_to_obj: SimpleNamespace | None = None
    if reply_to_msg_id is not None or forum_topic:
        reply_to_obj = SimpleNamespace(
            reply_to_msg_id=reply_to_msg_id,
            forum_topic=forum_topic,
            reply_to_top_id=reply_to_top_id,
        )

    return SimpleNamespace(
        id=id,
        date=datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
        message=text,
        sender_id=sender_id,
        sender=sender,
        media=media,
        reply_to=reply_to_obj,
        replies=SimpleNamespace(replies=reply_count) if reply_count else None,
        reactions=reactions,
        edit_date=edit_date,
        message_thread_id=message_thread_id,
        is_topic_message=is_topic_message,
    )


def build_mock_reactions(counts: dict[str, int]) -> SimpleNamespace:
    """Build a mock MessageReactions object."""
    results = [
        SimpleNamespace(reaction=SimpleNamespace(emoticon=emoji), count=count) for emoji, count in counts.items()
    ]
    return SimpleNamespace(results=results)


class MockTotalList(list):
    """List subclass with .total attribute, mimicking Telethon TotalList.

    Use in tests that switch from iter_messages to get_messages:
        mock_client.get_messages = AsyncMock(
            return_value=MockTotalList([msg1, msg2], total=500)
        )
    """

    def __init__(self, items: list, total: int | None = None) -> None:
        super().__init__(items)
        self.total = total if total is not None else len(items)


class LoudGroupProfilePort:
    """Test-only port that fails if an unrelated composition invokes it."""

    async def fetch_group_profile(self, group_id: int) -> GroupProfileObservation:
        raise AssertionError(f"unexpected group profile request for {group_id}")


class LoudCommonChatsPort:
    async def fetch_common_chats(self, reference: UserReference) -> CommonChatsObservation:
        raise AssertionError(f"unexpected common-chat request for {reference.user_id}")


class LoudUserAvatarHistoryPort:
    async def fetch_user_avatar_history(self, reference: UserReference) -> UserAvatarHistoryObservation:
        raise AssertionError(f"unexpected user-avatar request for {reference.user_id}")


class LoudChatAvatarHistoryPort:
    def get_chat_avatar_reference(self, entity_id: int) -> None:
        raise AssertionError(f"unexpected chat-avatar reference request for {entity_id}")

    async def fetch_chat_avatar_history(self, reference: ChatAvatarReference) -> ChatAvatarHistoryObservation:
        raise AssertionError(f"unexpected chat-avatar request for {reference}")


class ClientCommonChatsPort:
    """Neutral common-chat port backed by a test client's response queue."""

    def __init__(self, client: object) -> None:
        self.client = client

    async def fetch_common_chats(self, reference: UserReference) -> CommonChatsObservation:
        result = await self.client(("common_chats", {"user_id": reference.user_id, "limit": 100}))  # type: ignore[operator]
        chats: list[CommonChatSummary] = []
        for chat in getattr(result, "chats", ()) or ():
            chat_id = getattr(chat, "id", None)
            if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id == 0:
                raise ValueError("test common-chat id is invalid")
            kind = "supergroup" if bool(getattr(chat, "megagroup", False)) else "channel"
            if type(chat).__name__ == "Chat":
                kind = "group"
            chats.append(CommonChatSummary(chat_id, getattr(chat, "title", None), kind))
        return CommonChatsObservation(
            reference.user_id, tuple(chats), len(chats), ProjectionStatus.USABLE, None, 100.0, 100.0
        )


class ClientUserAvatarHistoryPort:
    """Neutral user-avatar port backed by a test client's response queue."""

    def __init__(self, client: object) -> None:
        self.client = client

    async def fetch_user_avatar_history(self, reference: UserReference) -> UserAvatarHistoryObservation:
        result = await self.client(("user_photos", {"user_id": reference.user_id, "limit": 100}))  # type: ignore[operator]
        photos: list[ChatCurrentPhoto] = []
        for photo in getattr(result, "photos", ()) or ():
            photo_id = getattr(photo, "id", None)
            if not isinstance(photo_id, int) or isinstance(photo_id, bool) or photo_id <= 0:
                raise ValueError("test user-photo id is invalid")
            date = getattr(photo, "date", None)
            photos.append(ChatCurrentPhoto(photo_id, date.isoformat() if date is not None else None))
        count = getattr(result, "count", len(photos))
        if not isinstance(count, int):
            count = len(photos)
        return UserAvatarHistoryObservation(
            reference.user_id, tuple(photos), max(count, len(photos)), ProjectionStatus.USABLE, None, 100.0, 100.0
        )


async def client_profile_collection_page(
    client: object, reference: UserReference, section: str
) -> ProfileCollectionPage:
    if section == "common_chats":
        observation = await ClientCommonChatsPort(client).fetch_common_chats(reference)
        items = tuple(
            ProfileCollectionItem(str(chat.chat_id), {"id": chat.chat_id, "name": chat.name}, {})
            for chat in observation.chats
        )
        count = observation.reported_count
    elif section == "avatar_history":
        observation = await ClientUserAvatarHistoryPort(client).fetch_user_avatar_history(reference)
        items = tuple(
            ProfileCollectionItem(str(photo.photo_id), {"id": photo.photo_id, "date": photo.date}, {})
            for photo in observation.photos
        )
        count = observation.reported_count
    else:
        from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope

        budget = current_rpc_scope().attempt_budget
        assert budget is not None
        budget.debit()
        items, count = (), 0
    return ProfileCollectionPage(reference.user_id, section, items, ObservationBoundary(100, 100), count, terminal=True)


class ClientChatAvatarHistoryPort:
    """Neutral chat-avatar port backed by a test client's response queue."""

    def __init__(self, client: object) -> None:
        self.client = client

    def get_chat_avatar_reference(self, entity_id: int) -> ChatAvatarReference | None:
        return GroupReference(entity_id) if isinstance(entity_id, int) and entity_id < 0 else None

    async def fetch_chat_avatar_history(self, reference: ChatAvatarReference) -> ChatAvatarHistoryObservation:
        result = await self.client(("search", {"limit": 100}))  # type: ignore[operator]
        photos: list[ChatCurrentPhoto] = []
        for message in getattr(result, "messages", ()) or ():
            action = getattr(message, "action", None)
            photo = getattr(action, "photo", None)
            photo_id = getattr(photo, "id", None)
            if not isinstance(photo_id, int) or isinstance(photo_id, bool) or photo_id <= 0:
                continue
            date = getattr(message, "date", None)
            photos.append(ChatCurrentPhoto(photo_id, date.isoformat() if date is not None else None))
        count = getattr(result, "count", len(photos))
        if not isinstance(count, int):
            count = len(photos)
        return ChatAvatarHistoryObservation(
            reference, tuple(photos), max(count, len(photos)), ProjectionStatus.USABLE, None, 100.0, 100.0
        )


class LoudChannelProfilePort:
    """Test-only port that fails if a channel composition unexpectedly calls it."""

    def get_channel_reference(self, channel_id: int) -> ChannelReference | None:
        raise AssertionError(f"unexpected channel reference request for {channel_id}")

    async def fetch_channel_profile(self, reference: ChannelReference) -> ChannelProfileObservation:
        raise AssertionError(f"unexpected channel profile request for {reference.channel_id}")

    async def fetch_channel_contact_overlap(self, reference: ChannelReference) -> ChannelContactOverlapObservation:
        raise AssertionError(f"unexpected channel contact overlap request for {reference.channel_id}")


class FakeChannelProfilePort:
    """Deterministic channel profile port double with independent call accounting."""

    def __init__(
        self,
        profile: ChannelProfileObservation,
        overlap: ChannelContactOverlapObservation,
        *,
        access_hash: int = 0,
    ) -> None:
        self.profile = profile
        self.overlap = overlap
        self.profile_calls: list[int] = []
        self.overlap_calls: list[int] = []
        self.references: list[ChannelReference] = []
        self.access_hash = access_hash

    def get_channel_reference(self, channel_id: int) -> ChannelReference | None:
        canonical_id = channel_id if channel_id <= -1_000_000_000_001 else -1_000_000_000_000 - abs(channel_id)
        return ChannelReference(channel_id=canonical_id, access_hash=self.access_hash)

    async def fetch_channel_profile(self, reference: ChannelReference) -> ChannelProfileObservation:
        self.references.append(reference)
        self.profile_calls.append(reference.channel_id)
        return replace(self.profile, channel_id=reference.channel_id)

    async def fetch_channel_contact_overlap(self, reference: ChannelReference) -> ChannelContactOverlapObservation:
        self.overlap_calls.append(reference.channel_id)
        return replace(self.overlap, channel_id=reference.channel_id)


class LoudUserProfilePort:
    """Test-only port that fails if a user profile request is unexpected."""

    def get_user_reference(self, user_id: int, *, is_self: bool = False) -> None:
        raise AssertionError(f"unexpected user reference request for {user_id}/{is_self}")

    async def fetch_user_profile(
        self, user_id: int, target_kind: TargetKind, *, owner_account_id: int | None = None
    ) -> UserProfileObservation:
        raise AssertionError(f"unexpected user profile request for {user_id} ({target_kind})")


class FakeUserProfilePort:
    """Deterministic user profile port double with explicit call accounting."""

    def __init__(
        self,
        observation: UserProfileObservation | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.observation = observation
        self.error = error
        self.calls: list[tuple[int, TargetKind]] = []

    def get_user_reference(self, user_id: int, *, is_self: bool = False) -> UserReference:
        return UserReference(user_id, 0, is_self=is_self)

    async def fetch_user_profile(
        self, user_id: int, target_kind: TargetKind, *, owner_account_id: int | None = None
    ) -> UserProfileObservation:
        self.calls.append((user_id, target_kind))
        if self.error is not None:
            raise self.error
        if self.observation is None:
            unavailable = ProjectionOutcome(ProjectionStatus.UNAVAILABLE, None, "unset", None)
            return UserProfileObservation(user_id, target_kind, unavailable, unavailable)
        return self.observation


def remove_v80_schema_for_historical_fixture(conn: sqlite3.Connection) -> None:
    """Remove v80 and later physical additions before rewinding the migration ledger."""
    remove_v87_schema_for_historical_fixture(conn)
    conn.execute("DROP TRIGGER IF EXISTS topic_catalog_identity_changed")
    conn.execute("DROP TRIGGER IF EXISTS message_fts_key_insert")
    conn.execute("DROP INDEX IF EXISTS idx_messages_nonchannel_message_id")
    for table in (
        "message_fts_keys",
        "message_observations",
        "scheduled_publication_evidence",
        "nonchannel_message_deletions",
        "entity_profile_field_observations",
        "topic_catalog_state",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    for table, column in (
        ("synced_dialogs", "delta_refresh_generation"),
        ("synced_dialogs", "full_sync_retry_at"),
        ("synced_dialogs", "delta_retry_at"),
        ("synced_dialogs", "delta_message_id"),
        ("topic_metadata", "observation_order"),
        ("topic_metadata", "catalog_present"),
        ("topic_metadata", "seen_generation"),
    ):
        rows = cast(
            list[tuple[int, str, str, int, str | None, int]], conn.execute(f"PRAGMA table_info({table})").fetchall()
        )
        if column in {row[1] for row in rows}:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    conn.execute("DELETE FROM schema_version WHERE version>=80")


def remove_v87_schema_for_historical_fixture(conn: sqlite3.Connection) -> None:
    """Historical fixtures must remove later physical tables as well as ledger rows."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='full_history_enrollment'").fetchone():
        conn.execute("DROP TRIGGER IF EXISTS topic_catalog_identity_changed")
    for table in (
        "entity_profile_change_events",
        "entity_profile_collection_membership",
        "entity_profile_collection_facts",
        "entity_profile_semantic_snapshots",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    if "page_cursor_json" in {row[1] for row in conn.execute("PRAGMA table_info(entity_profile_refresh_state)")}:
        conn.execute("ALTER TABLE entity_profile_refresh_state DROP COLUMN page_cursor_json")


def enable_profile_page_fixture(conn: sqlite3.Connection) -> None:
    """Upgrade the small old executor fixture before enabling writer authorization."""
    if "page_cursor_json" in {row[1] for row in conn.execute("PRAGMA table_info(entity_profile_refresh_state)")}:
        return
    from mcp_telegram.sync_db import _apply_migration_87, _apply_migration_88, _apply_migration_89

    columns = {row[1] for row in conn.execute("PRAGMA table_info(entity_details)")}
    if "profile_owner_account_id" not in columns:
        conn.execute("ALTER TABLE entity_details ADD COLUMN profile_owner_account_id INTEGER")
    conn.execute("CREATE TABLE IF NOT EXISTS daemon_state(key TEXT PRIMARY KEY,value TEXT)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS entity_profile_field_observations(entity_id INTEGER,field TEXT,"
        "owner_account_id INTEGER,observation_order INTEGER,observed_at INTEGER,source TEXT,PRIMARY KEY(entity_id,field))"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY,applied_at INTEGER)")
    _apply_migration_87(conn, 86)
    _apply_migration_88(conn, 87)
    _apply_migration_89(conn, 88)


def reset_rpc_process_policy() -> None:
    """Isolate test processes without a production cooldown-reset capability."""
    telegram_rpc._COOLDOWN_DEADLINE = 0.0
    telegram_rpc._OBSERVED_FLOOD_IDS.clear()
