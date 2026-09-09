"""Entity info extraction service extracted from daemon_api.

This module owns the full ``get_entity_info`` orchestration plus type-specific
helpers for user/bot/channel/supergroup/group entity details.
"""

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol, cast, runtime_checkable

from telethon.errors import ChatAdminRequiredError, RPCError  # type: ignore[import-untyped]
from telethon.tl.types import PeerChannel  # type: ignore[import-untyped]

from .entity_profile.contracts import PROFILE_SECTIONS, completeness
from .entity_profile.refresh import EntityRefreshCoordinator, RefreshEnqueueResult, RefreshLimits
from .entity_profile.repository import EntityProfileRepository, EntityRefreshCursor, EntitySectionCommit
from .entity_profile.telegram_gateway import BoundedTelegramGateway
from .entity_store import EntitySnapshot, ensure_entity_stub
from .flood import TelegramRpcThrottled
from .folders.read_model import dialog_placement
from .models import DialogType
from .telegram_access import ACCESS_LOST_ERRORS
from .telegram_demand import AcquisitionKind, DemandStatus, RpcAttemptBudget, RpcAttemptBudgetExhaustedError
from .telegram_rpc import raise_if_flood_wait_error
from .telegram_rpc_scheduler import TelegramRpcSource, rpc_scope
from .telethon_dialog import classify_dialog_type

_ENTITY_DETAIL_SCHEMA_VERSION = 1
_MEMBERSHIP_THRESHOLD_LARGE = 1000
_CHANNEL_DIALOG_ID_OFFSET = 1_000_000_000_000
_PERSONAL_CHANNEL_PREVIEW_CHARS = 100

_ADMIN_RIGHT_FIELDS = (
    "change_info",
    "post_messages",
    "edit_messages",
    "delete_messages",
    "ban_users",
    "invite_users",
    "pin_messages",
    "add_admins",
    "anonymous",
    "manage_call",
    "other",
    "manage_topics",
    "post_stories",
    "edit_stories",
    "delete_stories",
)


class _EntityInfoClient(Protocol):
    def get_entity(self, entity_id: int) -> Awaitable[object]: ...

    def get_messages(self, entity: object, ids: list[int]) -> Awaitable[object]: ...

    def __call__(self, request: object) -> Awaitable[object]: ...

    def iter_participants(self, peer: object, limit: int = 0) -> AsyncIterator[object]: ...

    def iter_dialogs(self) -> AsyncIterator[object]: ...


class _CommonChatsResult(Protocol):
    chats: Sequence[object]


class _FullUserResult(Protocol):
    full_user: object
    chats: Sequence[object]


class _UserPhotosResult(Protocol):
    count: int
    photos: Sequence[object]


class _FullChannelResult(Protocol):
    full_chat: object


class _FullChatResult(Protocol):
    full_chat: object


class _MessagesSearchResult(Protocol):
    count: int
    messages: Sequence[object]


@runtime_checkable
class _SupportsIsoformat(Protocol):
    def isoformat(self) -> str: ...


def _attr(obj: object, name: str, default: object | None = None) -> object | None:
    try:
        return cast(object | None, object.__getattribute__(obj, name))
    except AttributeError:
        return default


def _opt_int_attr(obj: object, name: str) -> int | None:
    value = _attr(obj, name)
    return value if isinstance(value, int) else None


def _opt_str_attr(obj: object, name: str) -> str | None:
    value = _attr(obj, name)
    return value if isinstance(value, str) else None


def _bool_attr(obj: object, name: str) -> bool:
    return bool(_attr(obj, name, False))


def _value_or_none[T](value: object, expected_type: type[T]) -> T | None:
    return value if isinstance(value, expected_type) else None


def _isoformat_or_none(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, _SupportsIsoformat):
        return cast(_SupportsIsoformat, value).isoformat()
    return None


def _text_or_none(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    text = _attr(value, "text", None)
    return text if isinstance(text, str) else None


def _sequence_attr(obj: object, name: str) -> Sequence[object]:
    value = _attr(obj, name, None)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return value
    return ()


def _positive_int_attr(obj: object, name: str) -> int | None:
    value = _opt_int_attr(obj, name)
    if value is None or value <= 0:
        return None
    return value


def _compact_dict(items: Mapping[str, object | None]) -> dict[str, object]:
    return {key: value for key, value in items.items() if value is not None}


def _timestamp_or_none(value: object | None) -> int | None:
    if isinstance(value, datetime):
        return int(value.timestamp())
    return None


def _row_sequence(row: object) -> Sequence[object]:
    return cast(Sequence[object], row)


def _row_mapping(row: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], row)


def _row_int_or_none(row: Mapping[str, object], key: str) -> int | None:
    value = row.get(key)
    return value if isinstance(value, int) else None


def _row_str_or_none(row: Mapping[str, object], key: str) -> str | None:
    value = row.get(key)
    return value if isinstance(value, str) else None


@dataclass(frozen=True)
class EntityInfoDeps:
    """Dependency container for entity-info orchestration."""

    conn: sqlite3.Connection
    client: _EntityInfoClient
    dm_peer_ids: Callable[[], set[int]]
    self_id: int | None
    self_profile: Mapping[str, object] | None
    get_peer_id: Callable[[object], int]
    rid: Callable[[], str]
    logger: logging.Logger
    now_provider: Callable[[], float]
    detail_ttl_seconds: int
    slow_stage_seconds: float
    get_common_chats_request: Callable[..., object]
    get_full_user_request: Callable[..., object]
    get_user_photos_request: Callable[..., object]
    get_messages_search_request: Callable[..., object]
    get_full_channel_request: Callable[..., object]
    get_participants_request: Callable[..., object]
    channel_participants_contacts_request: Callable[..., object]
    get_full_chat_request: Callable[..., object]
    input_messages_filter_chat_photos: type[object]
    message_action_chat_edit_photo: type[object]
    chat_reactions_all: type[object]
    chat_reactions_some: type[object]
    chat_reactions_none: type[object]
    channel_type: type[object]
    chat_type: type[object]
    get_dialog_placement: Callable[[int], dict[str, object]] | None = None
    refresh_limits: RefreshLimits = field(default_factory=RefreshLimits)


class DaemonEntityInfoService:
    """Entity-info extraction service used by ``DaemonAPIServer._get_entity_info``."""

    def __init__(self, deps: EntityInfoDeps, *, enable_refresh_coordinator: bool = True) -> None:
        self._deps = deps
        self._profiles = EntityProfileRepository(deps.conn, section_ttl_seconds=deps.detail_ttl_seconds)
        self._section_failures: dict[str, str] = {}
        self._refresh = (
            EntityRefreshCoordinator(
                self._refresh_entity,
                limits=deps.refresh_limits,
                on_failure=self._refresh_failed,
            )
            if enable_refresh_coordinator
            else None
        )
        if self._refresh is not None:
            self._refresh.bind_durable_executor(self._durable_refresh_status, self._run_durable_refresh_slice)

    @property
    def refresh_coordinator(self) -> EntityRefreshCoordinator | None:
        """Return the service-owned coordinator used by demand composition."""
        return self._refresh

    async def get_entity_info(self, req: Mapping[str, object]) -> dict[str, object]:
        """Run one foreground entity-info use case under its RPC source."""
        with rpc_scope(
            TelegramRpcSource.ENTITY_INFO_FOREGROUND,
            acquisition_kind=AcquisitionKind.ENTITY_LOOKUP,
        ):
            return await self._get_entity_info(req)

    async def _get_entity_info(self, req: Mapping[str, object]) -> dict[str, object]:
        """Type-tagged entity inspector covering 5 Telegram entity kinds."""
        started_at = self._deps.now_provider()
        entity_id = self._extract_entity_id(req)
        if entity_id is None:
            return self._error("telegram_api_error", "entity_id missing or not an integer")

        now = int(self._deps.now_provider())
        if self._progressive_enabled():
            cached_result = self._progressive_cached_result(entity_id, now=now)
            if cached_result is not None:
                return await self._await_progressive_refresh(entity_id, cached_result)
        else:
            cached = self._load_cached_detail(entity_id, now)
            if cached is not None:
                return cached

        if self._deps.self_id == entity_id and self._deps.self_profile is not None:
            return self._self_snapshot_result(entity_id, now=now, started_at=started_at)

        if self._progressive_enabled():
            result = await self._progressive_miss(entity_id, now=now, started_at=started_at)
            return await self._await_progressive_refresh(entity_id, result)

        return await self._legacy_entity_info(entity_id, now=now, started_at=started_at)

    async def _await_progressive_refresh(
        self,
        entity_id: int,
        fallback: dict[str, object],
    ) -> dict[str, object]:
        """Return the refreshed projection when foreground wait headroom permits it."""
        assert self._refresh is not None
        completed = await self._refresh.wait_for_completion(
            entity_id,
            self._deps.refresh_limits.foreground_refresh_wait_seconds,
        )
        if not completed:
            return fallback
        refreshed = self._progressive_cached_result(entity_id, now=int(self._deps.now_provider()))
        return fallback if refreshed is None else refreshed

    def _progressive_cached_result(self, entity_id: int, *, now: int) -> dict[str, object] | None:
        cached = self._profiles.read(entity_id, now=now)
        if cached is not None:
            return self._progressive_result(entity_id, cached.detail, cached.sections, now=now)
        refresh_state = self._profiles.refresh_state(entity_id, now=now)
        if refresh_state is None:
            return None
        reason = str(refresh_state.get("reason", "entity refresh is temporarily unavailable"))
        return self._pending_error(entity_id, reason)

    def _self_snapshot_result(self, entity_id: int, *, now: int, started_at: float) -> dict[str, object]:
        self_detail = self._build_self_detail()
        if not self._progressive_enabled():
            self._log_stage(entity_id, "self_snapshot", started_at, detail_type=self_detail.get("type"))
            return {"ok": True, "data": self_detail}
        self._profiles.save_core(self_detail, now=now)
        assert self._refresh is not None
        enqueue_result = self._refresh.enqueue(entity_id)
        self._persist_refresh_admission(entity_id, enqueue_result, now=now)
        sections: dict[str, dict[str, object]] = {
            section: {
                "status": "pending",
                "observed_at": None,
                "reason": self._refresh_reason(enqueue_result),
            }
            for section in PROFILE_SECTIONS
        }
        return self._progressive_result(entity_id, self_detail, sections, now=now)

    async def _legacy_entity_info(
        self,
        entity_id: int,
        *,
        now: int,
        started_at: float,
    ) -> dict[str, object]:
        self._log_stage(entity_id, "cache_miss", started_at)
        stage_started_at = self._deps.now_provider()
        entity, resolve_error = await self._resolve_entity(entity_id)
        self._log_stage(entity_id, "resolve_entity", stage_started_at)
        if resolve_error is not None:
            return resolve_error

        stage_started_at = self._deps.now_provider()
        detail, detail_error = await self._build_detail_by_type(entity)
        detail_type = detail.get("type") if detail is not None else None
        self._log_stage(entity_id, "build_detail", stage_started_at, detail_type=detail_type)
        if detail_error is not None or detail is None:
            return detail_error or self._error("telegram_api_error", "per-type helper returned no detail")

        stage_started_at = self._deps.now_provider()
        self._writeback(entity_id, detail, now)
        self._log_stage(entity_id, "writeback", stage_started_at, detail_type=detail.get("type"))
        self._log_stage(entity_id, "complete", started_at, detail_type=detail.get("type"))
        return {"ok": True, "data": detail}

    def _progressive_enabled(self) -> bool:
        """Enable the new contract only after its additive table exists."""
        row = cast(
            object,
            self._deps.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entity_detail_sections'"
            ).fetchone(),
        )
        return row is not None

    def _record_section_failure(self, section: str, reason: str) -> None:
        self._section_failures.setdefault(section, reason)

    async def _progressive_miss(self, entity_id: int, *, now: int, started_at: float) -> dict[str, object]:
        try:
            entity, resolve_error = await asyncio.wait_for(
                self._resolve_entity(entity_id),
                timeout=self._deps.refresh_limits.foreground_resolve_seconds,
            )
        except TimeoutError:
            self._log_stage(entity_id, "resolve_timeout", started_at)
            assert self._refresh is not None
            enqueue_result = self._refresh.enqueue(entity_id)
            if enqueue_result is RefreshEnqueueResult.REJECTED:
                self._profiles.mark_refresh_rejected(entity_id, now=now)
                return self._pending_error(entity_id, "entity profile refresh queue is full")
            budget = self._deps.refresh_limits.foreground_resolve_seconds
            return self._pending_error(entity_id, f"entity resolution exceeded the {budget:g} second foreground budget")
        if resolve_error is not None or entity is None:
            message = str((resolve_error or {}).get("message", "entity resolution is temporarily unavailable"))
            return self._pending_error(entity_id, message)

        core = self._core_from_entity(entity)
        self._profiles.save_core(core, now=now)
        assert self._refresh is not None
        enqueue_result = self._refresh.enqueue(entity_id)
        self._persist_refresh_admission(entity_id, enqueue_result, now=now)
        sections: dict[str, dict[str, object]] = {
            section: {
                "status": "pending",
                "observed_at": None,
                "reason": self._refresh_reason(enqueue_result),
            }
            for section in PROFILE_SECTIONS
        }
        result = self._progressive_result(entity_id, core, sections, now=now)
        self._log_stage(entity_id, "core_complete", started_at, detail_type=core.get("type"))
        return result

    async def _refresh_entity(self, entity_id: int) -> None:
        """Run one refresh under the coordinator-owned scope and deadline."""
        await self._refresh_entity_impl(entity_id)

    async def _refresh_entity_impl(self, entity_id: int) -> None:
        started_at = self._deps.now_provider()
        bounded_client = BoundedTelegramGateway(
            self._deps.client,
            timeout_seconds=self._deps.refresh_limits.per_rpc_seconds,
        )
        worker = DaemonEntityInfoService(
            replace(self._deps, client=cast(_EntityInfoClient, bounded_client)),
            enable_refresh_coordinator=False,
        )
        assert self._refresh is not None
        entity = await self._refresh_resolved_entity(worker, entity_id)
        core = self._core_from_entity(entity)
        self._profiles.save_core(core, now=int(self._deps.now_provider()))
        detail = await self._refresh_detail(worker, entity)
        now = int(self._deps.now_provider())
        self._profiles.save_detail(entity_id, detail, now=now, section_outcomes=worker._section_failures)
        self._deps.logger.info(
            "entity_profile.refresh outcome=success entity_id=%r duration_s=%.3f refreshed=%d timed_out=%d queue_depth=%d%s",
            entity_id,
            self._deps.now_provider() - started_at,
            len(PROFILE_SECTIONS),
            0,
            self._refresh.queue_depth,
            self._deps.rid(),
        )

    def _durable_refresh_status(self, now: float) -> DemandStatus | None:
        del now
        release_at = self._profiles.next_refresh_release_at()
        return DemandStatus(release_at=release_at) if release_at is not None else None

    async def _run_durable_refresh_slice(self, budget: RpcAttemptBudget) -> None:
        """Execute and commit at most one profile acquisition for one entity."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        now = int(self._deps.now_provider())
        cursor = self._profiles.next_due_refresh(now=now)
        if cursor is None:
            return
        stored = self._profiles.read(cursor.entity_id, now=now)
        raw_type = stored.detail.get("type") if stored is not None else None
        entity_type = DialogType.parse(raw_type if isinstance(raw_type, str) else None)
        if stored is None or entity_type is DialogType.UNKNOWN:
            await self._acquire_durable_refresh_core(cursor, now=now)
            return
        if not self._section_applies(entity_type, cursor.next_section):
            self._profiles.commit_section(
                cursor,
                EntitySectionCommit(
                    self._not_applicable_section_patch(cursor.next_section),
                    status="not_applicable",
                ),
                now=now,
            )
            return
        try:
            result = await self._acquire_profile_section(cursor, entity_type)
        except RpcAttemptBudgetExhaustedError:
            raise
        except TelegramRpcThrottled as exc:
            retry_seconds = max(1, int(exc.retry_after_seconds or 1))
            self._profiles.mark_section_failure(
                cursor,
                now=now,
                reason="flood_wait",
                retry_at=now + retry_seconds,
            )
            return
        except (TimeoutError, RPCError, RuntimeError, TypeError, AttributeError, ValueError) as exc:
            raise_if_flood_wait_error(exc)
            self._profiles.mark_section_failure(
                cursor,
                now=now,
                reason=type(exc).__name__.lower(),
                retry_at=now + 60,
            )
            return
        self._profiles.commit_section(
            cursor,
            result,
            now=now,
        )

    async def _acquire_durable_refresh_core(self, cursor: EntityRefreshCursor, *, now: int) -> None:
        """Persist one resolved core so later raw section requests can resume by id."""
        try:
            entity, error = await self._resolve_entity(cursor.entity_id)
        except RpcAttemptBudgetExhaustedError:
            raise
        except TelegramRpcThrottled as exc:
            retry_seconds = max(1, int(exc.retry_after_seconds or 1))
            self._profiles.mark_refresh_failure(
                cursor.entity_id,
                now=now,
                reason="flood_wait",
                retry_at=now + retry_seconds,
            )
            return
        if entity is None or error is not None:
            retry_after = (error or {}).get("_retry_after_seconds") if error is not None else None
            self._profiles.mark_refresh_failure(
                cursor.entity_id,
                now=now,
                reason="entity_unavailable",
                retry_at=now + retry_after if isinstance(retry_after, int) and retry_after > 0 else now + 60,
            )
            return
        self._profiles.save_core(self._core_from_entity(entity), now=now)
        self._profiles.advance_acquisition_cursor(
            cursor,
            next_acquisition_cursor=cursor.acquisition_cursor + 1,
            now=now,
        )

    async def _acquire_profile_section(
        self,
        cursor: EntityRefreshCursor,
        entity_type: DialogType,
    ) -> EntitySectionCommit:
        section = cursor.next_section
        if section == "full_profile":
            return await self._acquire_full_profile(cursor.entity_id, entity_type)
        if section == "common_chats":
            return await self._acquire_common_chats(cursor.entity_id)
        if section == "contact_overlap":
            return await self._acquire_contact_overlap(cursor.entity_id, entity_type)
        if section == "avatar_history":
            return await self._acquire_avatar_history(cursor.entity_id, entity_type)
        if section == "personal_channel":
            return await self._acquire_personal_channel(cursor.entity_id)
        raise ValueError(f"unsupported entity profile section: {section}")

    @staticmethod
    def _section_applies(entity_type: DialogType, section: str) -> bool:
        if section in {"full_profile", "avatar_history"}:
            return entity_type is not DialogType.UNKNOWN
        if section == "common_chats":
            return entity_type in {DialogType.USER, DialogType.BOT, DialogType.SERVICE}
        if section == "personal_channel":
            return entity_type in {DialogType.USER, DialogType.BOT}
        if section == "contact_overlap":
            return entity_type in {
                DialogType.CHANNEL,
                DialogType.SUPERGROUP,
                DialogType.FORUM,
                DialogType.GROUP,
            }
        return False

    @staticmethod
    def _not_applicable_section_patch(section: str) -> Mapping[str, object]:
        if section == "common_chats":
            return {"common_chats": []}
        if section == "contact_overlap":
            return {
                "contacts_subscribed": None,
                "contacts_subscribed_partial": False,
                "contacts_reason": "not_applicable",
            }
        if section == "avatar_history":
            return {"avatar_history": [], "avatar_count": 0}
        if section == "personal_channel":
            return {
                "personal_channel_id": None,
                "personal_channel": None,
                "personal_channel_unavailable_reason": None,
            }
        return {}

    async def _acquire_full_profile(
        self,
        entity_id: int,
        entity_type: DialogType,
    ) -> EntitySectionCommit:
        if entity_type in {DialogType.USER, DialogType.BOT, DialogType.SERVICE}:
            result = await self._deps.client(self._deps.get_full_user_request(id=entity_id))
            full_user = _attr(result, "full_user", None)
            if full_user is None:
                raise ValueError("full user payload is missing")
            folder_id = _opt_int_attr(full_user, "folder_id")
            blocked = _bool_attr(full_user, "blocked")
            patch: dict[str, object] = {
                "about": _opt_str_attr(full_user, "about"),
                "blocked": blocked,
                "ttl_period": _opt_int_attr(full_user, "ttl_period"),
                "private_forward_name": _opt_str_attr(full_user, "private_forward_name"),
                "folder_id": folder_id,
                "folder_name": await self._resolve_folder_name(folder_id),
                "birthday": self._extract_user_birthday(full_user),
                "bot_info": self._extract_user_bot_info(full_user),
                "business_location": self._extract_user_business_location(full_user),
                "business_intro": self._extract_user_business_intro(full_user),
                "business_work_hours": self._extract_user_business_work_hours(full_user),
                "note": self._extract_user_note(full_user),
            }
            user = self._find_result_entity(result, entity_id)
            if user is not None:
                first_name = _opt_str_attr(user, "first_name")
                last_name = _opt_str_attr(user, "last_name")
                patch.update(
                    name=" ".join(part for part in (first_name, last_name) if part) or None,
                    username=_opt_str_attr(user, "username"),
                    first_name=first_name,
                    last_name=last_name,
                    extra_usernames=self._collect_extra_usernames(user),
                    emoji_status_id=self._collect_emoji_status_id(user),
                    status=self._format_user_status(_attr(user, "status", None)),
                    phone=_opt_str_attr(user, "phone"),
                    lang_code=_opt_str_attr(user, "lang_code"),
                    contact=_bool_attr(user, "contact"),
                    mutual_contact=_bool_attr(user, "mutual_contact"),
                    close_friend=_bool_attr(user, "close_friend"),
                    send_paid_messages_stars=_opt_int_attr(user, "send_paid_messages_stars"),
                    verified=_bool_attr(user, "verified"),
                    premium=_bool_attr(user, "premium"),
                    bot=_bool_attr(user, "bot"),
                    scam=_bool_attr(user, "scam"),
                    fake=_bool_attr(user, "fake"),
                    restricted=_bool_attr(user, "restricted"),
                    restriction_reason=self._collect_restrictions(user),
                    my_membership=self._build_user_membership(user, blocked),
                )
            return EntitySectionCommit(patch)

        if entity_type in {DialogType.CHANNEL, DialogType.SUPERGROUP, DialogType.FORUM}:
            result = await self._deps.client(self._deps.get_full_channel_request(channel=entity_id))
            full_chat = _attr(result, "full_chat", None)
            if full_chat is None:
                raise ValueError("full channel payload is missing")
            patch = {
                "about": _opt_str_attr(full_chat, "about"),
                "linked_chat_id": self._normalize_linked_chat_id(_opt_int_attr(full_chat, "linked_chat_id")),
                "pinned_msg_id": _opt_int_attr(full_chat, "pinned_msg_id"),
                "slow_mode_seconds": _opt_int_attr(full_chat, "slowmode_seconds"),
            }
            member_count = _opt_int_attr(full_chat, "participants_count")
            if entity_type is DialogType.CHANNEL:
                patch.update(
                    subscribers_count=member_count,
                    available_reactions=self._collect_reactions(_attr(full_chat, "available_reactions", None)),
                )
            else:
                patch.update(members_count=member_count, linked_broadcast_id=patch.pop("linked_chat_id"))
            return EntitySectionCommit(patch)

        if entity_type is DialogType.GROUP:
            result = await self._deps.client(
                self._deps.get_full_chat_request(chat_id=self._legacy_chat_raw_id(entity_id))
            )
            full_chat = _attr(result, "full_chat", None)
            if full_chat is None:
                raise ValueError("full chat payload is missing")
            participants = _sequence_attr(_attr(full_chat, "participants", None), "participants")
            exported_invite = _attr(full_chat, "exported_invite", None)
            return EntitySectionCommit(
                {
                    "about": _opt_str_attr(full_chat, "about"),
                    "invite_link": _opt_str_attr(exported_invite, "link") if exported_invite is not None else None,
                    "members_count": len(participants) if participants else None,
                }
            )
        return EntitySectionCommit({}, status="unavailable", reason="unsupported_entity_type")

    async def _acquire_common_chats(self, entity_id: int) -> EntitySectionCommit:
        result = await self._deps.client(self._deps.get_common_chats_request(user_id=entity_id, max_id=0, limit=100))
        chats = [
            {
                "id": int(self._deps.get_peer_id(chat)),
                "name": _opt_str_attr(chat, "title") or str(_attr(chat, "id", "")),
                "type": self._classify_chat_type(chat),
            }
            for chat in _sequence_attr(result, "chats")
        ]
        return EntitySectionCommit({"common_chats": chats}, payload=chats)

    async def _acquire_contact_overlap(
        self,
        entity_id: int,
        entity_type: DialogType,
    ) -> EntitySectionCommit:
        if entity_type is DialogType.GROUP:
            result = await self._deps.client(
                self._deps.get_full_chat_request(chat_id=self._legacy_chat_raw_id(entity_id))
            )
            full_chat = _attr(result, "full_chat", None)
            if full_chat is None:
                raise ValueError("full chat payload is missing")
            raw_participants = _attr(full_chat, "participants", None)
            participant_ids = self._extract_group_participants(
                _sequence_attr(raw_participants, "participants") if raw_participants is not None else ()
            )
            contacts = self._enrich_contact_ids_with_names(participant_ids & self._deps.dm_peer_ids())
            return EntitySectionCommit(
                {
                    "contacts_subscribed": contacts,
                    "contacts_subscribed_partial": False,
                    "contacts_reason": None,
                },
                payload=contacts,
            )

        result = await self._deps.client(
            self._deps.get_participants_request(
                channel=entity_id,
                filter=self._deps.channel_participants_contacts_request(q=""),
                offset=0,
                limit=200,
                hash=0,
            )
        )
        contact_ids = {
            user_id for user in _sequence_attr(result, "users") if (user_id := _opt_int_attr(user, "id")) is not None
        }
        contacts = self._enrich_contact_ids_with_names(contact_ids & self._deps.dm_peer_ids())
        return EntitySectionCommit(
            {
                "contacts_subscribed": contacts,
                "contacts_subscribed_partial": True,
                "contacts_reason": "bounded_contacts_page",
            },
            payload=contacts,
        )

    async def _acquire_avatar_history(
        self,
        entity_id: int,
        entity_type: DialogType,
    ) -> EntitySectionCommit:
        if entity_type in {DialogType.USER, DialogType.BOT, DialogType.SERVICE}:
            result = await self._deps.client(
                self._deps.get_user_photos_request(user_id=entity_id, offset=0, max_id=0, limit=100)
            )
            photos = _sequence_attr(result, "photos")
            user_history = [
                {"photo_id": photo_id, "date": _isoformat_or_none(_attr(photo, "date", None))}
                for photo in photos
                if (photo_id := _opt_int_attr(photo, "id")) is not None
            ]
            count = _opt_int_attr(result, "count")
            return EntitySectionCommit(
                {
                    "avatar_history": user_history,
                    "avatar_count": count if count is not None else len(user_history),
                },
                payload=user_history,
            )

        result = await self._deps.client(
            self._deps.get_messages_search_request(
                peer=entity_id,
                q="",
                filter=self._deps.input_messages_filter_chat_photos(),
                min_date=None,
                max_date=None,
                offset_id=0,
                add_offset=0,
                limit=100,
                max_id=0,
                min_id=0,
                hash=0,
                from_id=None,
            )
        )
        chat_history: list[dict[str, object]] = []
        for message in _sequence_attr(result, "messages"):
            action = _attr(message, "action", None)
            if not isinstance(action, self._deps.message_action_chat_edit_photo):
                continue
            photo = _attr(action, "photo", None)
            photo_id = _opt_int_attr(photo, "id") if photo is not None else None
            if photo_id is not None:
                chat_history.append({"photo_id": photo_id, "date": _isoformat_or_none(_attr(message, "date", None))})
        count = _opt_int_attr(result, "count")
        return EntitySectionCommit(
            {
                "avatar_history": chat_history,
                "avatar_count": count if count is not None else len(chat_history),
            },
            payload=chat_history,
        )

    async def _acquire_personal_channel(self, entity_id: int) -> EntitySectionCommit:
        result = await self._deps.client(self._deps.get_full_user_request(id=entity_id))
        full_user = _attr(result, "full_user", None)
        if full_user is None:
            raise ValueError("full user payload is missing")
        raw_channel_id = _positive_int_attr(full_user, "personal_channel_id")
        if raw_channel_id is None:
            patch: dict[str, object] = {
                "personal_channel_id": None,
                "personal_channel": None,
                "personal_channel_unavailable_reason": None,
            }
            return EntitySectionCommit(patch, payload=None)
        dialog_id = self._normalize_channel_dialog_id(raw_channel_id)
        channel = self._find_personal_channel_chat(
            _sequence_attr(result, "chats"),
            raw_channel_id=raw_channel_id,
            dialog_id=dialog_id,
        )
        metadata = self._personal_channel_metadata(channel, dialog_id=dialog_id)
        if metadata is None:
            patch = {
                "personal_channel_id": raw_channel_id,
                "personal_channel": None,
                "personal_channel_unavailable_reason": "channel_metadata_unavailable",
            }
            return EntitySectionCommit(patch, status="unavailable", reason="channel_metadata_unavailable")
        local_preview, local_reason = self._latest_local_personal_channel_post(dialog_id)
        attached_message_id = _positive_int_attr(full_user, "personal_channel_message")
        card = _compact_dict(
            {
                "channel_id": raw_channel_id,
                "dialog_id": dialog_id,
                "title": metadata.get("title"),
                "username": metadata.get("username"),
                "url": self._tme_url(cast(str | None, metadata.get("username"))),
                "metadata_source": "user_full_chats" if channel is not None else "local_entities",
                "attached_message_id": attached_message_id,
                "latest_or_attached_post": local_preview,
                "post_preview_unavailable_reason": local_reason if local_preview is None else None,
            }
        )
        patch = {
            "personal_channel_id": raw_channel_id,
            "personal_channel": card,
            "personal_channel_unavailable_reason": None,
        }
        return EntitySectionCommit(patch, payload=card)

    def _find_result_entity(self, result: object, entity_id: int) -> object | None:
        for entity in _sequence_attr(result, "users"):
            try:
                if int(self._deps.get_peer_id(entity)) == entity_id:
                    return entity
            except TypeError, ValueError:
                continue
        return None

    @staticmethod
    def _legacy_chat_raw_id(entity_id: int) -> int:
        return -entity_id if entity_id < 0 else entity_id

    async def _refresh_resolved_entity(self, worker: DaemonEntityInfoService, entity_id: int) -> object:
        assert self._refresh is not None
        entity, error = await self._refresh.run_rpc(lambda: worker._resolve_entity(entity_id))
        if error is None and entity is not None:
            return entity
        retry_after = (error or {}).get("_retry_after_seconds") if error else None
        if isinstance(retry_after, int) and retry_after > 0:
            raise TelegramRpcThrottled(retry_after_seconds=retry_after)
        raise RuntimeError(str(error or "entity unavailable"))

    async def _refresh_detail(self, worker: DaemonEntityInfoService, entity: object) -> dict[str, object]:
        detail, detail_error = await worker._build_detail_by_type(entity)
        if detail_error is not None or detail is None:
            raise RuntimeError(str(detail_error or "detail unavailable"))
        return detail

    def _refresh_failed(self, entity_id: int, error: BaseException) -> None:
        refresh = self._refresh
        assert refresh is not None
        retry_at = getattr(error, "retry_after_seconds", None)
        if not isinstance(retry_at, int):
            retry_at = getattr(error, "seconds", None)
        now = int(self._deps.now_provider())
        reason = "flood_wait" if retry_at is not None else type(error).__name__.lower()
        self._profiles.mark_refresh_failure(
            entity_id, now=now, reason=reason, retry_at=(now + retry_at if isinstance(retry_at, int) else None)
        )
        self._deps.logger.warning(
            "entity_info.refresh outcome=failed entity_id=%r reason=%s queue_depth=%d%s",
            entity_id,
            reason,
            refresh.queue_depth,
            self._deps.rid(),
        )

    def _core_from_entity(self, entity: object) -> dict[str, object]:
        entity_id = int(self._deps.get_peer_id(entity))
        dispatch_kind = classify_dialog_type(entity)
        name = _opt_str_attr(entity, "first_name") or _opt_str_attr(entity, "title")
        last_name = _opt_str_attr(entity, "last_name")
        if last_name:
            name = " ".join(part for part in (name, last_name) if part)
        return {
            "id": entity_id,
            "type": dispatch_kind.value,
            "name": name,
            "username": _opt_str_attr(entity, "username"),
            "dialog_placement": (
                self._deps.get_dialog_placement(entity_id)
                if self._deps.get_dialog_placement
                else dialog_placement(self._deps.conn, entity_id)
            ),
        }

    def _progressive_result(
        self,
        entity_id: int,
        detail: Mapping[str, object],
        sections: dict[str, dict[str, object]],
        *,
        now: int,
    ) -> dict[str, object]:
        self._enqueue_section_refresh(entity_id, sections)
        result = dict(detail)
        result["id"] = entity_id
        result["dialog_placement"] = result.get(
            "dialog_placement",
            self._deps.get_dialog_placement(entity_id)
            if self._deps.get_dialog_placement
            else dialog_placement(self._deps.conn, entity_id),
        )
        result["completeness"] = completeness(sections)
        result["sections"] = self._section_summaries(sections)
        self._deps.logger.info(
            "entity_profile.request outcome=%s entity_id=%r duration_s=0.000 sections=%s%s",
            result["completeness"],
            entity_id,
            self._section_counts(sections),
            self._deps.rid(),
        )
        return {"ok": True, "data": result}

    def _enqueue_section_refresh(self, entity_id: int, sections: dict[str, dict[str, object]]) -> None:
        if not self._has_refreshable_sections(sections):
            return
        assert self._refresh is not None
        enqueue_result = self._refresh.enqueue(entity_id)
        self._record_section_admission(entity_id, sections, enqueue_result)

    @staticmethod
    def _has_refreshable_sections(sections: Mapping[str, Mapping[str, object]]) -> bool:
        return any(
            value.get("status") in {"pending", "stale"}
            or (value.get("status") == "unavailable" and value.get("reason") == "refresh_rejected")
            for value in sections.values()
        )

    def _record_section_admission(
        self,
        entity_id: int,
        sections: dict[str, dict[str, object]],
        result: RefreshEnqueueResult,
    ) -> None:
        if result is RefreshEnqueueResult.REJECTED:
            self._profiles.mark_refresh_rejected(entity_id, now=int(self._deps.now_provider()))
            self._set_section_rejected(sections)
            return
        self._profiles.mark_refresh_queued(entity_id)
        self._set_section_queued(sections)

    @staticmethod
    def _set_section_rejected(sections: dict[str, dict[str, object]]) -> None:
        for section in sections.values():
            if section.get("status") == "not_applicable":
                continue
            section.update(status="unavailable", reason="refresh_rejected")

    @staticmethod
    def _set_section_queued(sections: dict[str, dict[str, object]]) -> None:
        for section in sections.values():
            if section.get("status") not in {"pending", "stale", "unavailable"}:
                continue
            if section.get("status") == "unavailable" and section.get("reason") != "refresh_rejected":
                continue
            section.update(status="pending", reason="refresh_queued")

    def _persist_refresh_admission(self, entity_id: int, result: RefreshEnqueueResult, *, now: int) -> None:
        if result is RefreshEnqueueResult.REJECTED:
            self._profiles.mark_refresh_rejected(entity_id, now=now)
        else:
            self._profiles.mark_pending(entity_id, now=now)

    @staticmethod
    def _refresh_reason(result: RefreshEnqueueResult) -> str:
        return "refresh_rejected" if result is RefreshEnqueueResult.REJECTED else "refresh_queued"

    @staticmethod
    def _section_summaries(sections: Mapping[str, Mapping[str, object]]) -> dict[str, dict[str, object]]:
        return {
            name: {
                key: value[key]
                for key in ("status", "observed_at", "reason")
                if key == "status" or isinstance(value.get(key), int | str)
            }
            for name, value in sections.items()
        }

    @staticmethod
    def _section_counts(sections: Mapping[str, Mapping[str, object]]) -> dict[str, int]:
        statuses = {"fresh", "stale", "pending", "unavailable", "not_applicable"}
        return {status: sum(value.get("status") == status for value in sections.values()) for status in statuses}

    @staticmethod
    def _pending_error(entity_id: int, message: str) -> dict[str, object]:
        return {
            "ok": False,
            "error": "entity_info_pending",
            "message": message,
            "retryable": True,
            "data": {"id": entity_id, "completeness": "partial", "sections": {}},
        }

    async def shutdown(self) -> None:
        if self._refresh is not None:
            await self._refresh.shutdown()

    def _build_self_detail(self) -> dict[str, object]:
        profile = self._deps.self_profile or {}
        first_name = _value_or_none(profile.get("first_name"), str)
        last_name = _value_or_none(profile.get("last_name"), str)
        name = " ".join(part for part in (first_name, last_name) if part) or None
        return {
            "id": self._deps.self_id,
            "type": DialogType.USER.value,
            "name": name,
            "username": _value_or_none(profile.get("username"), str),
            "about": None,
            "my_membership": {"role": "self"},
            "avatar_history": [],
            "avatar_count": 0,
            "first_name": first_name,
            "last_name": last_name,
            "extra_usernames": [],
            "emoji_status_id": None,
            "status": None,
            "phone": None,
            "lang_code": None,
            "contact": False,
            "mutual_contact": False,
            "close_friend": False,
            "send_paid_messages_stars": None,
            "personal_channel_id": None,
            "personal_channel": None,
            "personal_channel_unavailable_reason": None,
            "birthday": None,
            "verified": False,
            "premium": False,
            "bot": False,
            "scam": False,
            "fake": False,
            "restricted": False,
            "restriction_reason": [],
            "blocked": False,
            "ttl_period": None,
            "private_forward_name": None,
            "bot_info": None,
            "business_location": None,
            "business_intro": None,
            "business_work_hours": None,
            "note": None,
            "folder_id": None,
            "folder_name": None,
            "common_chats": [],
            "_full_fetch_ok": False,
        }

    def _log_stage(
        self,
        entity_id: int,
        stage: str,
        started_at: float,
        *,
        detail_type: object | None = None,
    ) -> None:
        duration_s = self._deps.now_provider() - started_at
        if stage in {"cache_miss", "self_snapshot", "complete"} or duration_s >= self._deps.slow_stage_seconds:
            self._deps.logger.info(
                "entity_info_stage entity_id=%r stage=%s duration_s=%.3f detail_type=%s%s",
                entity_id,
                stage,
                duration_s,
                detail_type,
                self._deps.rid(),
            )

    def _extract_entity_id(self, req: Mapping[str, object]) -> int | None:
        entity_id = req.get("entity_id")
        if not isinstance(entity_id, int):
            return None
        return entity_id

    def _load_cached_detail(self, entity_id: int, now: int) -> dict[str, object] | None:
        try:
            row = cast(
                tuple[str, int] | None,
                self._deps.conn.execute(
                    "SELECT detail_json, fetched_at FROM entity_details WHERE entity_id = ?",
                    (entity_id,),
                ).fetchone(),
            )
        except sqlite3.OperationalError as exc:
            self._deps.logger.warning(
                "entity_info db_read_failed entity_id=%r error=%s%s",
                entity_id,
                exc,
                self._deps.rid(),
            )
            return self._error("db_unavailable", str(exc))

        if row is not None:
            detail_json, fetched_at = row
            if now - fetched_at < self._deps.detail_ttl_seconds:
                try:
                    detail = cast(dict[str, object], json.loads(detail_json))
                except json.JSONDecodeError:
                    self._deps.logger.warning(
                        "entity_info detail_json_corrupt entity_id=%r%s — treating as cache miss",
                        entity_id,
                        self._deps.rid(),
                    )
                    return None
                if detail.get("schema") == _ENTITY_DETAIL_SCHEMA_VERSION:
                    return {"ok": True, "data": self._strip_envelope_schema(detail)}
        return None

    async def _resolve_entity(self, entity_id: int) -> tuple[object | None, dict[str, object] | None]:
        try:
            entity = await self._deps.client.get_entity(entity_id)
            return entity, None
        except (ValueError, KeyError) as exc:
            self._deps.logger.warning(
                "entity_info entity_not_found entity_id=%r error=%s%s",
                entity_id,
                exc,
                self._deps.rid(),
            )
            return None, self._error("entity_not_found", str(exc))
        except TelegramRpcThrottled:
            raise
        except RpcAttemptBudgetExhaustedError:
            raise
        except (TimeoutError, RPCError, RuntimeError, TypeError, AttributeError) as exc:
            raise_if_flood_wait_error(exc)
            raise_if_flood_wait_error(exc)
            raise_if_flood_wait_error(exc)
            raise_if_flood_wait_error(exc)
            raise_if_flood_wait_error(exc)
            raise_if_flood_wait_error(exc)
            if isinstance(exc, ACCESS_LOST_ERRORS):
                return None, self._error("telegram_api_error", str(exc))
            self._deps.logger.warning(
                "entity_info get_entity_failed entity_id=%r error=%s%s",
                entity_id,
                exc,
                self._deps.rid(),
                exc_info=True,
            )
            error = self._error("telegram_api_error", str(exc))
            retry_after = getattr(exc, "retry_after_seconds", None)
            if not isinstance(retry_after, int):
                retry_after = getattr(exc, "seconds", None)
            if isinstance(retry_after, int) and retry_after > 0:
                error["_retry_after_seconds"] = retry_after
            return None, error

    async def _build_detail_by_type(self, entity: object) -> tuple[dict[str, object] | None, dict[str, object] | None]:
        dispatch_kind = classify_dialog_type(entity)
        if dispatch_kind in (DialogType.USER, DialogType.BOT, DialogType.SERVICE):
            return await self._fetch_user_detail(entity), None
        if dispatch_kind == DialogType.CHANNEL:
            return await self._fetch_channel_detail(entity), None
        if dispatch_kind in (DialogType.SUPERGROUP, DialogType.FORUM):
            return await self._fetch_supergroup_detail(entity), None
        if dispatch_kind == DialogType.GROUP:
            return await self._fetch_group_detail(entity), None
        return None, self._error("unsupported_entity_type", f"unknown entity kind: {dispatch_kind}")

    def _writeback(self, entity_id: int, detail: dict[str, object], now: int) -> None:
        full_fetch_ok = detail.pop("_full_fetch_ok", True)
        try:
            ensure_entity_stub(
                self._deps.conn,
                EntitySnapshot(
                    entity_id=entity_id,
                    entity_type=cast(str, detail.get("type", "unknown")),
                    name=cast(str | None, detail.get("name")),
                    username=cast(str | None, detail.get("username")),
                    name_normalized=None,
                    updated_at=now,
                ),
            )
            if full_fetch_ok:
                payload_with_schema = {"schema": _ENTITY_DETAIL_SCHEMA_VERSION, **detail}
                self._deps.conn.execute(
                    "INSERT OR REPLACE INTO entity_details (entity_id, detail_json, fetched_at) VALUES (?, ?, ?)",
                    (entity_id, json.dumps(payload_with_schema), now),
                )
            self._deps.conn.commit()
        except sqlite3.OperationalError as exc:
            self._deps.logger.warning(
                "entity_info db_writeback_failed entity_id=%r error=%s%s",
                entity_id,
                exc,
                self._deps.rid(),
                exc_info=True,
            )

    @staticmethod
    def _error(message_key: str, message: str) -> dict[str, object]:
        return {
            "ok": False,
            "error": message_key,
            "message": message,
            "data": None,
        }

    @staticmethod
    def _strip_envelope_schema(detail: Mapping[str, object]) -> dict[str, object]:
        return {k: v for k, v in detail.items() if k != "schema"}

    @staticmethod
    def _format_user_status(status: object) -> dict[str, object] | None:
        if status is None:
            return None
        status_type = type(status).__name__
        online_like = {
            "UserStatusOnline": ("online", "expires"),
            "UserStatusOffline": ("offline", "was_online"),
        }
        if status_type in online_like:
            key, ts_key = online_like[status_type]
            value = _attr(status, "expires" if ts_key == "expires" else "was_online", None)
            return {"type": key, ts_key: _isoformat_or_none(value)}
        if status_type == "UserStatusRecently":
            return {"type": "recently"}
        if status_type == "UserStatusLastWeek":
            return {"type": "last_week"}
        if status_type == "UserStatusLastMonth":
            return {"type": "last_month"}
        return None

    async def _fetch_user_detail(self, user: object) -> dict[str, object]:
        user_id = _opt_int_attr(user, "id")
        if user_id is None:
            raise ValueError("user id missing")

        common_chats = await self._collect_common_chats(user_id)
        profile = await self._collect_user_profile(user_id)
        profile["folder_name"] = await self._resolve_folder_name(cast(int | None, profile["folder_id"]))
        extra_usernames = self._collect_extra_usernames(user)
        emoji_status_id = self._collect_emoji_status_id(user)
        avatar_history, avatar_count = await self._collect_user_avatar_history(user)
        my_membership = self._build_user_membership(user, cast(bool, profile["blocked"]))

        first_name = _opt_str_attr(user, "first_name")
        last_name = _opt_str_attr(user, "last_name")
        name = " ".join(part for part in (first_name, last_name) if part)
        return {
            "id": user_id,
            "type": classify_dialog_type(user).value,
            "name": name or None,
            "username": _attr(user, "username", None),
            "about": profile["about"],
            "my_membership": my_membership,
            "avatar_history": avatar_history,
            "avatar_count": avatar_count,
            "first_name": _attr(user, "first_name", None),
            "last_name": _attr(user, "last_name", None),
            "extra_usernames": extra_usernames,
            "emoji_status_id": emoji_status_id,
            "status": self._format_user_status(_attr(user, "status", None)),
            "phone": _attr(user, "phone", None),
            "lang_code": _attr(user, "lang_code", None),
            "contact": bool(_attr(user, "contact", False)),
            "mutual_contact": bool(_attr(user, "mutual_contact", False)),
            "close_friend": bool(_attr(user, "close_friend", False)),
            "send_paid_messages_stars": _attr(user, "send_paid_messages_stars", None),
            "personal_channel_id": profile["personal_channel_id"],
            "personal_channel": profile["personal_channel"],
            "personal_channel_unavailable_reason": profile["personal_channel_unavailable_reason"],
            "birthday": profile["birthday"],
            "verified": bool(_attr(user, "verified", False)),
            "premium": bool(_attr(user, "premium", False)),
            "bot": bool(_attr(user, "bot", False)),
            "scam": bool(_attr(user, "scam", False)),
            "fake": bool(_attr(user, "fake", False)),
            "restricted": bool(_attr(user, "restricted", False)),
            "restriction_reason": self._collect_restrictions(user),
            "blocked": profile["blocked"],
            "ttl_period": profile["ttl_period"],
            "private_forward_name": profile["private_forward_name"],
            "bot_info": profile["bot_info"],
            "business_location": profile["business_location"],
            "business_intro": profile["business_intro"],
            "business_work_hours": profile["business_work_hours"],
            "note": profile["note"],
            "folder_id": profile["folder_id"],
            "folder_name": profile["folder_name"],
            "common_chats": common_chats,
            "_full_fetch_ok": profile["full_user_ok"],
        }

    async def _collect_common_chats(self, user_id: int) -> list[dict[str, object]]:
        chats: list[dict[str, object]] = []
        try:
            common_result = cast(
                _CommonChatsResult,
                await self._deps.client(self._deps.get_common_chats_request(user_id=user_id, max_id=0, limit=100)),
            )
            chats.extend(
                {
                    "id": int(self._deps.get_peer_id(chat)),
                    "name": _opt_str_attr(chat, "title") or str(_attr(chat, "id", "")),
                    "type": self._classify_chat_type(chat),
                }
                for chat in common_result.chats
            )
        except TelegramRpcThrottled:
            raise
        except (TimeoutError, RPCError, RuntimeError, TypeError, AttributeError, ValueError) as exc:
            raise_if_flood_wait_error(exc)
            self._record_section_failure("common_chats", type(exc).__name__.lower())
            self._deps.logger.warning(
                "entity_info user common_chats_failed user_id=%r error=%s%s",
                user_id,
                exc,
                self._deps.rid(),
                exc_info=not isinstance(exc, TimeoutError),
            )
        return chats

    def _classify_chat_type(self, chat: object) -> str:
        if isinstance(chat, self._deps.channel_type):
            return DialogType.SUPERGROUP.value if _attr(chat, "megagroup", False) else DialogType.CHANNEL.value
        if isinstance(chat, self._deps.chat_type):
            return DialogType.GROUP.value
        return DialogType.USER.value

    async def _collect_user_profile(self, user_id: int) -> dict[str, object]:
        profile: dict[str, object] = {
            "about": None,
            "personal_channel_id": None,
            "blocked": False,
            "ttl_period": None,
            "private_forward_name": None,
            "folder_id": None,
            "folder_name": None,
            "birthday": None,
            "personal_channel": None,
            "personal_channel_unavailable_reason": None,
            "bot_info": None,
            "business_location": None,
            "business_intro": None,
            "business_work_hours": None,
            "note": None,
            "full_user_ok": False,
        }
        try:
            full_result = cast(
                _FullUserResult,
                await self._deps.client(self._deps.get_full_user_request(id=user_id)),
            )
            user_full = full_result.full_user
            profile["about"] = _opt_str_attr(user_full, "about")
            profile["personal_channel_id"] = _positive_int_attr(user_full, "personal_channel_id")
            profile["blocked"] = _bool_attr(user_full, "blocked")
            profile["ttl_period"] = _opt_int_attr(user_full, "ttl_period")
            profile["private_forward_name"] = _opt_str_attr(user_full, "private_forward_name")
            profile["folder_id"] = _opt_int_attr(user_full, "folder_id")
            profile["note"] = self._extract_user_note(user_full)
            profile["bot_info"] = self._extract_user_bot_info(user_full)
            profile["business_location"] = self._extract_user_business_location(user_full)
            profile["business_intro"] = self._extract_user_business_intro(user_full)
            profile["business_work_hours"] = self._extract_user_business_work_hours(user_full)
            profile["birthday"] = self._extract_user_birthday(user_full)
            personal_channel, reason = await self._collect_personal_channel(
                user_full,
                _sequence_attr(full_result, "chats"),
            )
            profile["personal_channel"] = personal_channel
            profile["personal_channel_unavailable_reason"] = reason
            profile["full_user_ok"] = True
        except TelegramRpcThrottled:
            raise
        except (TimeoutError, RPCError, RuntimeError, TypeError, AttributeError, ValueError, KeyError) as exc:
            raise_if_flood_wait_error(exc)
            self._record_section_failure("full_profile", type(exc).__name__.lower())
            self._deps.logger.warning(
                "entity_info user full_user_failed user_id=%r error=%s%s",
                user_id,
                exc,
                self._deps.rid(),
                exc_info=not isinstance(exc, TimeoutError),
            )
        return profile

    async def _collect_personal_channel(
        self,
        user_full: object,
        chats: Sequence[object],
    ) -> tuple[dict[str, object] | None, str | None]:
        raw_channel_id = _positive_int_attr(user_full, "personal_channel_id")
        if raw_channel_id is None:
            return None, None

        dialog_id = self._normalize_channel_dialog_id(raw_channel_id)
        channel = self._find_personal_channel_chat(
            chats,
            raw_channel_id=raw_channel_id,
            dialog_id=dialog_id,
        )
        metadata = self._personal_channel_metadata(channel, dialog_id=dialog_id)
        metadata_source = "user_full_chats" if channel is not None else "local_entities"
        if metadata is None:
            return None, "channel_metadata_unavailable"

        attached_message_id = _positive_int_attr(user_full, "personal_channel_message")
        preview, preview_reason = await self._collect_personal_channel_post_preview(
            channel,
            dialog_id=dialog_id,
            attached_message_id=attached_message_id,
        )
        card = _compact_dict(
            {
                "channel_id": raw_channel_id,
                "dialog_id": dialog_id,
                "title": metadata.get("title"),
                "username": metadata.get("username"),
                "url": self._tme_url(cast(str | None, metadata.get("username"))),
                "metadata_source": metadata_source,
                "attached_message_id": attached_message_id,
                "latest_or_attached_post": preview,
                "post_preview_unavailable_reason": preview_reason if preview is None else None,
            }
        )
        return card, None

    def _find_personal_channel_chat(
        self,
        chats: Sequence[object],
        *,
        raw_channel_id: int,
        dialog_id: int,
    ) -> object | None:
        for chat in chats:
            chat_id = _opt_int_attr(chat, "id")
            if chat_id in (raw_channel_id, dialog_id):
                return chat
            try:
                if int(self._deps.get_peer_id(chat)) == dialog_id:
                    return chat
            except TypeError, ValueError:
                continue
        return None

    def _personal_channel_metadata(self, channel: object | None, *, dialog_id: int) -> dict[str, object] | None:
        if channel is not None:
            metadata = _compact_dict(
                {
                    "title": _opt_str_attr(channel, "title"),
                    "username": _opt_str_attr(channel, "username"),
                }
            )
            if metadata:
                return metadata

        row = cast(
            tuple[object | None, object | None] | None,
            self._deps.conn.execute("SELECT name, username FROM entities WHERE id = ?", (dialog_id,)).fetchone(),
        )
        if row is None:
            return None
        metadata = _compact_dict(
            {
                "title": row[0] if isinstance(row[0], str) else None,
                "username": row[1] if isinstance(row[1], str) else None,
            }
        )
        return metadata or None

    async def _collect_personal_channel_post_preview(
        self,
        channel: object | None,
        *,
        dialog_id: int,
        attached_message_id: int | None,
    ) -> tuple[dict[str, object] | None, str | None]:
        attached_failure: str | None = None
        if channel is not None and attached_message_id is not None:
            preview, attached_failure = await self._fetch_attached_personal_channel_post(
                channel,
                message_id=attached_message_id,
            )
            if preview is not None:
                return preview, None

        local_preview, local_failure = self._latest_local_personal_channel_post(dialog_id)
        if local_preview is not None:
            return local_preview, None
        return None, attached_failure or local_failure

    async def _fetch_attached_personal_channel_post(
        self,
        channel: object,
        *,
        message_id: int,
    ) -> tuple[dict[str, object] | None, str]:
        try:
            fetched = await self._deps.client.get_messages(channel, ids=[message_id])
        except TelegramRpcThrottled:
            raise
        except (TimeoutError, RPCError, TypeError, AttributeError, ValueError) as exc:
            raise_if_flood_wait_error(exc)
            self._record_section_failure("personal_channel", type(exc).__name__.lower())
            self._deps.logger.warning(
                "entity_info personal_channel_message_failed channel_id=%r message_id=%r error=%s%s",
                int(self._deps.get_peer_id(channel)),
                message_id,
                exc,
                self._deps.rid(),
            )
            return None, "attached_message_fetch_failed"

        message = self._first_message(fetched)
        if message is None:
            return None, "attached_message_not_found"
        preview = self._post_preview_from_text(
            source="personal_channel_message",
            message_id=message_id,
            sent_at=_timestamp_or_none(_attr(message, "date", None)),
            text=self._message_text(message),
        )
        if preview is None:
            return None, "attached_message_has_no_text"
        return preview, ""

    @staticmethod
    def _first_message(fetched: object) -> object | None:
        if fetched is None:
            return None
        if isinstance(fetched, Sequence) and not isinstance(fetched, str | bytes | bytearray):
            return fetched[0] if fetched else None
        return fetched

    def _latest_local_personal_channel_post(self, dialog_id: int) -> tuple[dict[str, object] | None, str]:
        try:
            row = cast(
                tuple[object, object, object] | None,
                self._deps.conn.execute(
                    """
                    SELECT message_id, sent_at, text
                    FROM messages
                    WHERE dialog_id = ?
                      AND is_deleted = 0
                      AND is_service = 0
                      AND text IS NOT NULL
                      AND TRIM(text) != ''
                    ORDER BY sent_at DESC, message_id DESC
                    LIMIT 1
                    """,
                    (dialog_id,),
                ).fetchone(),
            )
        except sqlite3.OperationalError:
            return None, "local_messages_unavailable"

        if row is None:
            return None, "no_synced_text_posts"
        preview = self._post_preview_from_text(
            source="local_latest_message",
            message_id=int(cast(int | str, row[0])),
            sent_at=int(cast(int | str, row[1])),
            text=row[2] if isinstance(row[2], str) else None,
        )
        return (preview, "") if preview is not None else (None, "local_latest_message_has_no_text")

    @staticmethod
    def _message_text(message: object) -> str | None:
        return _opt_str_attr(message, "message") or _opt_str_attr(message, "text")

    @staticmethod
    def _post_preview_from_text(
        *,
        source: str,
        message_id: int,
        sent_at: int | None,
        text: str | None,
    ) -> dict[str, object] | None:
        if text is None:
            return None
        stripped = text.strip()
        if not stripped:
            return None
        preview = stripped[:_PERSONAL_CHANNEL_PREVIEW_CHARS]
        return _compact_dict(
            {
                "source": source,
                "message_id": message_id,
                "sent_at": sent_at,
                "text_preview": preview,
                "char_count": len(stripped),
                "is_truncated": len(stripped) > _PERSONAL_CHANNEL_PREVIEW_CHARS,
            }
        )

    @staticmethod
    def _normalize_channel_dialog_id(raw_channel_id: int) -> int:
        if raw_channel_id > 0:
            return -_CHANNEL_DIALOG_ID_OFFSET - raw_channel_id
        return raw_channel_id

    @staticmethod
    def _tme_url(username: str | None) -> str | None:
        if not username:
            return None
        return f"https://t.me/{username}"

    def _extract_user_note(self, user_full: object) -> str | None:
        return _text_or_none(_attr(user_full, "note", None))

    def _extract_user_bot_info(self, user_full: object) -> dict[str, object] | None:
        raw_bot_info = _attr(user_full, "bot_info", None)
        if raw_bot_info is None:
            return None
        return {
            "description": _opt_str_attr(raw_bot_info, "description"),
            "commands": [
                {
                    "command": _opt_str_attr(cmd, "command") or "",
                    "description": _opt_str_attr(cmd, "description") or "",
                }
                for cmd in cast(Sequence[object], _attr(raw_bot_info, "commands", None) or [])
            ],
        }

    def _extract_user_business_location(self, user_full: object) -> dict[str, object] | None:
        raw_loc = _attr(user_full, "business_location", None)
        if raw_loc is None:
            return None
        geo = _attr(raw_loc, "geo_point", None)
        return {
            "address": _opt_str_attr(raw_loc, "address"),
            "lat": _attr(geo, "lat", None) if geo is not None else None,
            "long": _attr(geo, "long", None) if geo is not None else None,
        }

    def _extract_user_business_intro(self, user_full: object) -> dict[str, object] | None:
        raw_intro = _attr(user_full, "business_intro", None)
        if raw_intro is None:
            return None
        return {
            "title": _opt_str_attr(raw_intro, "title"),
            "description": _opt_str_attr(raw_intro, "description"),
        }

    def _extract_user_business_work_hours(self, user_full: object) -> dict[str, object] | None:
        raw_hours = _attr(user_full, "business_work_hours", None)
        if raw_hours is None:
            return None
        return {"timezone": _opt_str_attr(raw_hours, "timezone_id")}

    def _extract_user_birthday(self, user_full: object) -> dict[str, object] | None:
        bday = _attr(user_full, "birthday", None)
        if bday is None:
            return None
        return {
            "day": _opt_int_attr(bday, "day"),
            "month": _opt_int_attr(bday, "month"),
            "year": _opt_int_attr(bday, "year"),
        }

    async def _resolve_folder_name(self, folder_id: int | None) -> str | None:
        if folder_id is None:
            return None
        try:
            row = cast(
                tuple[object, ...] | None,
                self._deps.conn.execute(
                    "SELECT title FROM telegram_folders WHERE folder_id = ?", (folder_id,)
                ).fetchone(),
            )
            if row is not None:
                return str(row[0])
        except (sqlite3.OperationalError, TypeError, ValueError) as exc:
            self._deps.logger.warning(
                "entity_info user folder_resolve_failed folder_id=%r error=%s%s",
                folder_id,
                exc,
                self._deps.rid(),
            )
        return None

    def _collect_extra_usernames(self, user: object) -> list[str]:
        extra_usernames: list[str] = []
        for uname in cast(Sequence[object], _attr(user, "usernames", None) or []):
            raw = _opt_str_attr(uname, "username")
            if raw and raw != _opt_str_attr(user, "username"):
                extra_usernames.append(raw)
        return extra_usernames

    def _collect_emoji_status_id(self, user: object) -> int | None:
        emoji_status = _attr(user, "emoji_status", None)
        if emoji_status is None:
            return None
        return _opt_int_attr(emoji_status, "document_id")

    async def _collect_user_avatar_history(self, user: object) -> tuple[list[dict[str, object]], int]:
        avatar_history: list[dict[str, object]] = []
        avatar_count = 0
        try:
            photos_result = cast(
                _UserPhotosResult,
                await self._deps.client(
                    self._deps.get_user_photos_request(user_id=user, offset=0, max_id=0, limit=100)
                ),
            )
            photos = list(photos_result.photos)
            avatar_count = int(getattr(photos_result, "count", len(photos)))
            for photo in photos:
                photo_id = _opt_int_attr(photo, "id")
                photo_date = _attr(photo, "date", None)
                if photo_id is None or photo_date is None:
                    continue
                avatar_history.append({"photo_id": int(photo_id), "date": _isoformat_or_none(photo_date)})
        except TelegramRpcThrottled:
            raise
        except (TimeoutError, RPCError, RuntimeError, TypeError, AttributeError, ValueError) as exc:
            raise_if_flood_wait_error(exc)
            self._record_section_failure("avatar_history", type(exc).__name__.lower())
            self._deps.logger.warning(
                "entity_info user photos_failed user_id=%r error=%s%s",
                int(cast(int, self._deps.get_peer_id(user))),
                exc,
                self._deps.rid(),
                exc_info=not isinstance(exc, TimeoutError),
            )
        return avatar_history, avatar_count

    def _build_user_membership(self, user: object, blocked: bool) -> dict[str, object]:
        contact_flag = _bool_attr(user, "contact")
        mutual_contact = _bool_attr(user, "mutual_contact")
        close_friend = _bool_attr(user, "close_friend")
        return {
            "is_member": contact_flag or mutual_contact,
            "is_admin": False,
            "admin_rights": None,
            "relationship": {
                "contact": contact_flag,
                "mutual_contact": mutual_contact,
                "close_friend": close_friend,
                "blocked": blocked,
            },
        }

    def _collect_restrictions(self, entity: object) -> list[dict[str, object]]:
        return [
            {
                "platform": _opt_str_attr(rr, "platform"),
                "reason": _opt_str_attr(rr, "reason"),
                "text": _opt_str_attr(rr, "text"),
            }
            for rr in cast(Sequence[object], _attr(entity, "restriction_reason", None) or [])
        ]

    async def _search_chat_photo_history(self, peer: object, full_chat: object) -> tuple[list[dict[str, object]], int]:
        """Avatar history via messages.Search(filter=ChatPhotos)."""
        peer_id = int(self._deps.get_peer_id(peer))
        avatar_history: list[dict[str, object]] = []
        avatar_count = 0
        search_failed = False
        try:
            search_result = cast(
                _MessagesSearchResult,
                await self._deps.client(
                    self._deps.get_messages_search_request(
                        peer=peer,
                        q="",
                        filter=self._deps.input_messages_filter_chat_photos(),
                        min_date=None,
                        max_date=None,
                        offset_id=0,
                        add_offset=0,
                        limit=100,
                        max_id=0,
                        min_id=0,
                        hash=0,
                        from_id=None,
                    )
                ),
            )
            avatar_count = search_result.count
            for msg in search_result.messages:
                action = _attr(msg, "action", None)
                if not isinstance(action, self._deps.message_action_chat_edit_photo):
                    continue
                photo = _attr(action, "photo", None)
                photo_date = _attr(msg, "date", None)
                if photo is None or photo_date is None or _opt_int_attr(photo, "id") is None:
                    continue
                avatar_history.append(
                    {"photo_id": int(_opt_int_attr(photo, "id") or 0), "date": _isoformat_or_none(photo_date)},
                )
        except TelegramRpcThrottled:
            raise
        except (TimeoutError, RPCError, TypeError, AttributeError, ValueError) as exc:
            raise_if_flood_wait_error(exc)
            self._record_section_failure("avatar_history", type(exc).__name__.lower())
            search_failed = True
            self._deps.logger.warning(
                "entity_info avatar_search_failed peer_id=%r error=%s%s",
                peer_id,
                exc,
                self._deps.rid(),
            )

        return self._finalize_chat_photo_history(
            avatar_history,
            avatar_count,
            full_chat=full_chat,
            search_failed=search_failed,
        )

    def _finalize_chat_photo_history(
        self,
        avatar_history: list[dict[str, object]],
        avatar_count: int,
        *,
        full_chat: object,
        search_failed: bool,
    ) -> tuple[list[dict[str, object]], int]:
        chat_photo = _attr(full_chat, "chat_photo", None) if full_chat is not None else None
        current_photo_id = _opt_int_attr(chat_photo, "id") if chat_photo is not None else None
        if current_photo_id is not None and not any(p["photo_id"] == int(current_photo_id) for p in avatar_history):
            chat_photo_date = _attr(chat_photo, "date", None)
            avatar_history.insert(
                0,
                {
                    "photo_id": int(current_photo_id),
                    "date": _isoformat_or_none(chat_photo_date),
                },
            )
        if search_failed and current_photo_id is not None:
            avatar_count = max(avatar_count, 1)
        if not avatar_history and current_photo_id is not None:
            chat_photo_date = _attr(chat_photo, "date", None)
            avatar_history = [
                {
                    "photo_id": int(current_photo_id),
                    "date": _isoformat_or_none(chat_photo_date),
                }
            ]
            avatar_count = max(avatar_count, 1)
        return avatar_history, avatar_count

    async def _fetch_channel_detail(self, channel: object) -> dict[str, object]:
        channel_id = int(self._deps.get_peer_id(channel))
        full_context = await self._collect_full_channel_context(channel, collect_reactions=True)
        memberships = self._build_chat_membership(channel)
        contacts_subscribed, contacts_subscribed_partial, contacts_reason = await self._collect_channel_contacts(
            channel,
            is_admin=cast(bool, memberships["is_admin"]),
            subscribers_count=cast(int | None, full_context["subscribers_count"]),
        )
        avatar_history, avatar_count = await self._search_chat_photo_history(channel, full_context["full_chat"])

        return {
            "id": channel_id,
            "type": DialogType.CHANNEL.value,
            "name": _attr(channel, "title", None),
            "username": _attr(channel, "username", None),
            "about": full_context["about"],
            "my_membership": memberships,
            "avatar_history": avatar_history,
            "avatar_count": avatar_count,
            "subscribers_count": full_context["subscribers_count"],
            "linked_chat_id": full_context["linked_chat_id"],
            "pinned_msg_id": full_context["pinned_msg_id"],
            "slow_mode_seconds": full_context["slow_mode_seconds"],
            "available_reactions": full_context["available_reactions"],
            "restrictions": self._collect_restrictions(channel),
            "contacts_subscribed": contacts_subscribed,
            "contacts_subscribed_partial": contacts_subscribed_partial,
            "contacts_reason": contacts_reason,
            "_full_fetch_ok": full_context["full_channel_ok"],
        }

    async def _collect_full_channel_context(self, channel: object, *, collect_reactions: bool) -> dict[str, object]:
        context: dict[str, object] = {
            "full_chat": None,
            "subscribers_count": None,
            "linked_chat_id": None,
            "pinned_msg_id": None,
            "slow_mode_seconds": None,
            "about": None,
            "available_reactions": {"kind": "none", "emojis": []},
            "full_channel_ok": False,
        }
        try:
            full_result = cast(
                _FullChannelResult,
                await self._deps.client(self._deps.get_full_channel_request(channel=channel)),
            )
            full_chat = full_result.full_chat
            context["full_chat"] = full_chat
            context["subscribers_count"] = _opt_int_attr(full_chat, "participants_count")
            context["linked_chat_id"] = self._normalize_linked_chat_id(_opt_int_attr(full_chat, "linked_chat_id"))
            context["pinned_msg_id"] = _opt_int_attr(full_chat, "pinned_msg_id")
            context["slow_mode_seconds"] = _opt_int_attr(full_chat, "slowmode_seconds")
            context["about"] = _opt_str_attr(full_chat, "about")
            if collect_reactions:
                context["available_reactions"] = self._collect_reactions(
                    _attr(full_chat, "available_reactions", None),
                )
            context["full_channel_ok"] = True
        except TelegramRpcThrottled:
            raise
        except (TimeoutError, RPCError, RuntimeError, TypeError, AttributeError, ValueError) as exc:
            raise_if_flood_wait_error(exc)
            self._record_section_failure("full_profile", type(exc).__name__.lower())
            self._deps.logger.warning(
                "entity_info channel full_channel_failed channel_id=%r error=%s%s",
                int(self._deps.get_peer_id(channel)),
                exc,
                self._deps.rid(),
                exc_info=not isinstance(exc, TimeoutError),
            )
        return context

    def _collect_reactions(self, raw_reactions: object) -> dict[str, object]:
        if isinstance(raw_reactions, self._deps.chat_reactions_all):
            return {"kind": "all", "emojis": []}
        if isinstance(raw_reactions, self._deps.chat_reactions_some):
            emojis = [
                _opt_str_attr(r, "emoticon")
                for r in cast(Sequence[object], _attr(raw_reactions, "reactions", []) or [])
            ]
            return {"kind": "some", "emojis": [emoji for emoji in emojis if emoji]}
        if isinstance(raw_reactions, self._deps.chat_reactions_none) or raw_reactions is None:
            return {"kind": "none", "emojis": []}
        return {"kind": "none", "emojis": []}

    def _normalize_linked_chat_id(self, raw_linked_chat_id: int | None) -> int | None:
        if raw_linked_chat_id is None:
            return None
        if raw_linked_chat_id > 0:
            return int(self._deps.get_peer_id(PeerChannel(raw_linked_chat_id)))
        return int(raw_linked_chat_id)

    def _build_chat_membership(self, entity: object) -> dict[str, object]:
        is_creator = _bool_attr(entity, "creator")
        admin_rights_obj = _attr(entity, "admin_rights", None)
        admin_rights = self._extract_admin_rights(admin_rights_obj)
        is_admin = is_creator or (admin_rights is not None)
        return {
            "is_member": not _bool_attr(entity, "left"),
            "is_admin": is_admin,
            "admin_rights": admin_rights,
        }

    def _extract_admin_rights(self, admin_rights_obj: object) -> dict[str, bool] | None:
        if admin_rights_obj is None:
            return None
        return {field: bool(_attr(admin_rights_obj, field, False)) for field in _ADMIN_RIGHT_FIELDS}

    async def _collect_channel_contacts(
        self,
        channel: object,
        *,
        is_admin: bool,
        subscribers_count: int | None,
    ) -> tuple[list[dict[str, object]] | None, bool, str | None]:
        if not is_admin:
            return None, False, "not_an_admin"
        if subscribers_count is None:
            return None, False, "count_unavailable"
        if subscribers_count > _MEMBERSHIP_THRESHOLD_LARGE:
            return await self._collect_contacts_via_filter(
                channel,
                not_admin_reason="not_an_admin",
                large_error_log="entity_info channel contacts_enumeration_failed channel_id=%r error=%s%s",
            )
        return await self._collect_contacts_via_participants(
            channel,
            not_admin_reason="not_an_admin",
            small_error_log="entity_info channel contacts_enumeration_failed channel_id=%r error=%s%s",
        )

    async def _collect_contacts_via_participants(
        self,
        channel: object,
        *,
        not_admin_reason: str,
        small_error_log: str,
    ) -> tuple[list[dict[str, object]] | None, bool, str | None]:
        try:
            participant_ids: set[int] = set()
            async for p in self._deps.client.iter_participants(channel, limit=1000):
                pid = _attr(p, "id", None)
                if pid is not None:
                    participant_ids.add(int(cast(int | str, pid)))
            intersect_ids = participant_ids & self._deps.dm_peer_ids()
            return self._enrich_contact_ids_with_names(intersect_ids), False, None
        except ChatAdminRequiredError:
            return None, False, not_admin_reason
        except TelegramRpcThrottled:
            raise
        except (TimeoutError, RPCError, TypeError, AttributeError, ValueError) as exc:
            raise_if_flood_wait_error(exc)
            self._record_section_failure("contact_overlap", type(exc).__name__.lower())
            self._deps.logger.warning(
                small_error_log,
                int(cast(int, self._deps.get_peer_id(channel))),
                exc,
                self._deps.rid(),
            )
            return None, False, "enumeration_failed"

    async def _collect_contacts_via_filter(
        self,
        channel: object,
        *,
        not_admin_reason: str,
        large_error_log: str,
    ) -> tuple[list[dict[str, object]] | None, bool, str | None]:
        try:
            gp_result = await self._deps.client(
                self._deps.get_participants_request(
                    channel=channel,
                    filter=self._deps.channel_participants_contacts_request(q=""),
                    offset=0,
                    limit=200,
                    hash=0,
                )
            )
            contact_ids = {
                int(cast(int, _opt_int_attr(u, "id")))
                for u in cast(Sequence[object], _attr(gp_result, "users", []) or [])
                if _opt_int_attr(u, "id") is not None
            }
            intersect_ids = contact_ids & self._deps.dm_peer_ids()
            return self._enrich_contact_ids_with_names(intersect_ids), True, "too_large"
        except ChatAdminRequiredError:
            return None, False, not_admin_reason
        except TelegramRpcThrottled:
            raise
        except (TimeoutError, RPCError, TypeError, AttributeError, ValueError) as exc:
            raise_if_flood_wait_error(exc)
            self._record_section_failure("contact_overlap", type(exc).__name__.lower())
            self._deps.logger.warning(
                large_error_log,
                int(self._deps.get_peer_id(channel)),
                exc,
                self._deps.rid(),
            )
            return None, False, "enumeration_failed"

    def _enrich_contact_ids_with_names(self, ids: set[int]) -> list[dict[str, object]]:
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = cast(
            Sequence[tuple[object, object, object]],
            self._deps.conn.execute(
                f"SELECT id, name, username FROM entities WHERE id IN ({placeholders})",
                tuple(ids),
            ).fetchall(),
        )
        seen = {row[0] for row in rows}
        out = [{"id": row[0], "name": row[1], "username": row[2]} for row in rows]
        out.extend({"id": missing_id, "name": None, "username": None} for missing_id in ids - seen)
        return sorted(out, key=lambda d: ((d["name"] or ""), d["id"]))

    async def _fetch_supergroup_detail(self, channel: object) -> dict[str, object]:
        channel_id = int(self._deps.get_peer_id(channel))
        full_context = await self._collect_full_channel_context(channel, collect_reactions=False)
        memberships = self._build_chat_membership(channel)
        hidden_members = bool(_attr(channel, "hidden_members", False)) and not memberships["is_admin"]
        contacts_subscribed, contacts_subscribed_partial, contacts_reason = await self._collect_supergroup_contacts(
            channel,
            is_admin=cast(bool, memberships["is_admin"]),
            members_count=cast(int | None, full_context["subscribers_count"]),
            hidden_members=hidden_members,
        )
        avatar_history, avatar_count = await self._search_chat_photo_history(channel, full_context["full_chat"])

        return {
            "id": channel_id,
            "type": DialogType.SUPERGROUP.value,
            "name": _attr(channel, "title", None),
            "username": _attr(channel, "username", None),
            "about": full_context["about"],
            "my_membership": memberships,
            "avatar_history": avatar_history,
            "avatar_count": avatar_count,
            "members_count": full_context["subscribers_count"],
            "linked_broadcast_id": full_context["linked_chat_id"],
            "slow_mode_seconds": full_context["slow_mode_seconds"],
            "has_topics": bool(_attr(channel, "forum", False)),
            "restrictions": self._collect_restrictions(channel),
            "contacts_subscribed": contacts_subscribed,
            "contacts_subscribed_partial": contacts_subscribed_partial,
            "contacts_reason": contacts_reason,
            "_full_fetch_ok": full_context["full_channel_ok"],
        }

    async def _collect_supergroup_contacts(
        self,
        channel: object,
        *,
        is_admin: bool,
        members_count: int | None,
        hidden_members: bool,
    ) -> tuple[list[dict[str, object]] | None, bool, str | None]:
        if hidden_members:
            return None, False, "hidden_by_admin"
        if members_count is None:
            return None, False, "count_unavailable"
        if members_count > _MEMBERSHIP_THRESHOLD_LARGE:
            return await self._collect_contacts_via_filter(
                channel,
                not_admin_reason="hidden_by_admin",
                large_error_log="entity_info supergroup contacts_filter_failed channel_id=%r error=%s%s",
            )
        try:
            participant_ids: set[int] = set()
            async for participant in self._deps.client.iter_participants(channel, limit=1000):
                pid = _attr(participant, "id", None)
                if pid is not None:
                    participant_ids.add(int(cast(int | str, pid)))
            intersect_ids = participant_ids & self._deps.dm_peer_ids()
            return self._enrich_contact_ids_with_names(intersect_ids), False, None
        except ChatAdminRequiredError:
            return None, False, "hidden_by_admin"
        except TelegramRpcThrottled:
            raise
        except (TimeoutError, RPCError, TypeError, AttributeError, ValueError) as exc:
            raise_if_flood_wait_error(exc)
            self._record_section_failure("contact_overlap", type(exc).__name__.lower())
            self._deps.logger.warning(
                "entity_info supergroup iter_participants_failed channel_id=%r error=%s%s",
                int(cast(int, self._deps.get_peer_id(channel))),
                exc,
                self._deps.rid(),
            )
            return None, False, "enumeration_failed"

    async def _fetch_group_detail(self, chat: object) -> dict[str, object]:
        chat_id = int(self._deps.get_peer_id(chat))
        migrated_to = self._resolve_group_migrated_to(chat)
        group_meta = await self._collect_group_full_chat(chat)
        my_membership = self._build_chat_membership(chat)

        contacts_subscribed: list[dict[str, object]] | None = []
        contacts_reason: str | None = None
        try:
            participant_ids = self._extract_group_participants(cast(Sequence[object], group_meta["participants"]))
            intersect_ids = participant_ids & self._deps.dm_peer_ids()
            contacts_subscribed = self._enrich_contact_ids_with_names(intersect_ids)
        except (TypeError, AttributeError, ValueError, sqlite3.Error) as exc:
            self._record_section_failure("contact_overlap", type(exc).__name__.lower())
            self._deps.logger.warning(
                "entity_info group contacts_intersect_failed chat_id=%r error=%s%s",
                chat_id,
                exc,
                self._deps.rid(),
            )
            contacts_subscribed = None
            contacts_reason = "enumeration_failed"

        avatar_history, avatar_count = await self._search_chat_photo_history(chat, group_meta["full_chat"])

        return {
            "id": chat_id,
            "type": DialogType.GROUP.value,
            "name": _attr(chat, "title", None),
            "username": None,
            "about": group_meta["about"],
            "my_membership": my_membership,
            "avatar_history": avatar_history,
            "avatar_count": avatar_count,
            "members_count": group_meta["members_count"],
            "migrated_to": migrated_to,
            "invite_link": group_meta["invite_link"],
            "restrictions": self._collect_restrictions(chat),
            "contacts_subscribed": contacts_subscribed,
            "contacts_subscribed_partial": False,
            "contacts_reason": contacts_reason,
            "_full_fetch_ok": True,
        }

    def _resolve_group_migrated_to(self, chat: object) -> int | None:
        migrated_to_obj = _attr(chat, "migrated_to", None)
        if migrated_to_obj is None:
            return None
        try:
            return int(self._deps.get_peer_id(migrated_to_obj))
        except (TypeError, ValueError) as exc:
            self._deps.logger.warning(
                "entity_info group migrated_to_normalize_failed chat_id=%r error=%s%s",
                int(self._deps.get_peer_id(chat)),
                exc,
                self._deps.rid(),
            )
            return None

    async def _collect_group_full_chat(self, chat: object) -> dict[str, object]:
        group_meta: dict[str, object] = {
            "full_chat": None,
            "about": None,
            "invite_link": None,
            "participants": [],
            "members_count": None,
        }
        try:
            chat_id = _opt_int_attr(chat, "id")
            if chat_id is None:
                raise ValueError("chat id missing")
            full_result = cast(
                _FullChatResult, await self._deps.client(self._deps.get_full_chat_request(chat_id=chat_id))
            )
            full_chat = full_result.full_chat
            group_meta["full_chat"] = full_chat
            group_meta["about"] = _attr(full_chat, "about", None) or None
            exported_invite = _attr(full_chat, "exported_invite", None)
            if exported_invite is not None:
                group_meta["invite_link"] = _attr(exported_invite, "link", None)
            raw_participants = _attr(full_chat, "participants", None)
            if raw_participants is not None:
                participants = list(cast(Sequence[object], _attr(raw_participants, "participants", []) or []))
                group_meta["participants"] = participants
                group_meta["members_count"] = len(participants)
            if group_meta["members_count"] is None:
                group_meta["members_count"] = _attr(chat, "participants_count", None)
        except TelegramRpcThrottled:
            raise
        except (TimeoutError, RPCError, RuntimeError, TypeError, AttributeError, ValueError) as exc:
            raise_if_flood_wait_error(exc)
            self._record_section_failure("full_profile", type(exc).__name__.lower())
            self._deps.logger.warning(
                "entity_info group full_chat_failed chat_id=%r error=%s%s",
                int(self._deps.get_peer_id(chat)),
                exc,
                self._deps.rid(),
                exc_info=not isinstance(exc, TimeoutError),
            )
        return group_meta

    def _extract_group_participants(self, participants: Sequence[object]) -> set[int]:
        return {
            int(p_user_id)
            for p in participants
            if (p_user_id := _opt_int_attr(p, "user_id")) is not None and int(p_user_id) != 0
        }
