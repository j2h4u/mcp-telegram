"""Entity info extraction service extracted from daemon_api.

This module owns the full ``get_entity_info`` orchestration plus type-specific
helpers for user/bot/channel/supergroup/group entity details.
"""

import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast, runtime_checkable

from telethon.errors import ChatAdminRequiredError, RPCError  # type: ignore[import-untyped]
from telethon.tl.types import PeerChannel  # type: ignore[import-untyped]

from .models import DialogType

_ENTITY_DETAIL_TTL_SECONDS = 300
_ENTITY_DETAIL_SCHEMA_VERSION = 1
_MEMBERSHIP_THRESHOLD_LARGE = 1000

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

    def __call__(self, request: object) -> Awaitable[object]: ...

    def iter_participants(self, peer: object, limit: int = 0) -> AsyncIterator[object]: ...

    def iter_dialogs(self) -> AsyncIterator[object]: ...


class _CommonChatsResult(Protocol):
    chats: Sequence[object]


class _DialogFiltersResult(Protocol):
    filters: Sequence[object]


class _FullUserResult(Protocol):
    full_user: object


class _UserPhotosResult(Protocol):
    count: int
    photos: Sequence[object]


class _FullChannelResult(Protocol):
    full_chat: object


class _ParticipantsResult(Protocol):
    users: Sequence[object]


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
    get_peer_id: Callable[[object], int]
    rid: Callable[[], str]
    logger: logging.Logger
    now_provider: Callable[[], float]
    get_common_chats_request: Callable[..., object]
    get_dialog_filters_request: Callable[..., object]
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


class DaemonEntityInfoService:
    """Entity-info extraction service used by ``DaemonAPIServer._get_entity_info``."""

    def __init__(self, deps: EntityInfoDeps) -> None:
        self._deps = deps

    async def get_entity_info(self, req: Mapping[str, object]) -> dict[str, object]:
        """Type-tagged entity inspector covering 5 Telegram entity kinds."""
        entity_id = self._extract_entity_id(req)
        if entity_id is None:
            return self._error("telegram_api_error", "entity_id missing or not an integer")

        now = int(self._deps.now_provider())
        cached = self._load_cached_detail(entity_id, now)
        if cached is not None:
            return cached

        entity, resolve_error = await self._resolve_entity(entity_id)
        if resolve_error is not None:
            return resolve_error

        detail, detail_error = await self._build_detail_by_type(entity)
        if detail_error is not None:
            return detail_error
        if detail is None:
            return self._error("telegram_api_error", "per-type helper returned no detail")

        self._writeback(entity_id, detail, now)
        return {"ok": True, "data": detail}

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
            if now - fetched_at < _ENTITY_DETAIL_TTL_SECONDS:
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
        except Exception as exc:
            self._deps.logger.warning(
                "entity_info get_entity_failed entity_id=%r error=%s%s",
                entity_id,
                exc,
                self._deps.rid(),
                exc_info=True,
            )
            return None, self._error("telegram_api_error", str(exc))

    async def _build_detail_by_type(self, entity: object) -> tuple[dict[str, object] | None, dict[str, object] | None]:
        dispatch_kind = DialogType.from_entity(entity)
        if dispatch_kind in (DialogType.USER, DialogType.BOT):
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
            self._deps.conn.execute(
                "INSERT OR IGNORE INTO entities (id, type, name, username, updated_at) VALUES (?, ?, ?, ?, ?)",
                (
                    entity_id,
                    detail.get("type", "unknown"),
                    detail.get("name"),
                    detail.get("username"),
                    now,
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
            "type": "bot" if bool(_attr(user, "bot", False)) else "user",
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
        except Exception as exc:
            self._deps.logger.warning(
                "entity_info user common_chats_failed user_id=%r error=%s%s",
                user_id,
                exc,
                self._deps.rid(),
                exc_info=True,
            )
        return chats

    def _classify_chat_type(self, chat: object) -> str:
        if isinstance(chat, self._deps.channel_type):
            return "supergroup" if _attr(chat, "megagroup", False) else "channel"
        if isinstance(chat, self._deps.chat_type):
            return "group"
        return "user"

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
            profile["personal_channel_id"] = _opt_int_attr(user_full, "personal_channel_id")
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
            profile["full_user_ok"] = True
        except Exception as exc:
            self._deps.logger.warning(
                "entity_info user full_user_failed user_id=%r error=%s%s",
                user_id,
                exc,
                self._deps.rid(),
                exc_info=True,
            )
        return profile

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
            filters = cast(
                _DialogFiltersResult,
                await self._deps.client(self._deps.get_dialog_filters_request()),
            )
            for item in filters.filters:
                if _opt_int_attr(item, "id") != folder_id:
                    continue
                raw_title = _attr(item, "title", None)
                return _text_or_none(raw_title)
        except Exception as exc:  # noqa: BLE001
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
            avatar_count = int(photos_result.count)
            for photo in photos_result.photos:
                photo_id = _opt_int_attr(photo, "id")
                photo_date = _attr(photo, "date", None)
                if photo_id is None or photo_date is None:
                    continue
                avatar_history.append({"photo_id": int(photo_id), "date": _isoformat_or_none(photo_date)})
        except Exception as exc:
            self._deps.logger.warning(
                "entity_info user photos_failed user_id=%r error=%s%s",
                int(cast(int, self._deps.get_peer_id(user))),
                exc,
                self._deps.rid(),
                exc_info=True,
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
        except (RPCError, TypeError, AttributeError, ValueError) as exc:
            search_failed = True
            self._deps.logger.warning(
                "entity_info avatar_search_failed peer_id=%r error=%s%s",
                peer_id,
                exc,
                self._deps.rid(),
            )

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
            "type": "channel",
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
        except Exception as exc:
            self._deps.logger.warning(
                "entity_info channel full_channel_failed channel_id=%r error=%s%s",
                int(self._deps.get_peer_id(channel)),
                exc,
                self._deps.rid(),
                exc_info=True,
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
        except (RPCError, TypeError, AttributeError, ValueError) as exc:
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
        except (RPCError, TypeError, AttributeError, ValueError) as exc:
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
            "type": "supergroup",
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
        except (RPCError, TypeError, AttributeError, ValueError) as exc:
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
            "type": "group",
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
        except Exception as exc:
            self._deps.logger.warning(
                "entity_info group full_chat_failed chat_id=%r error=%s%s",
                int(self._deps.get_peer_id(chat)),
                exc,
                self._deps.rid(),
                exc_info=True,
            )
        return group_meta

    def _extract_group_participants(self, participants: Sequence[object]) -> set[int]:
        return {
            int(p_user_id)
            for p in participants
            if (p_user_id := _opt_int_attr(p, "user_id")) is not None and int(p_user_id) != 0
        }
