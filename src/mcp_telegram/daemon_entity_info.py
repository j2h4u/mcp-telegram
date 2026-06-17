"""Entity info extraction service extracted from daemon_api.

This module owns the full ``get_entity_info`` orchestration plus type-specific
helpers for user/bot/channel/supergroup/group entity details.
"""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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


@dataclass(frozen=True)
class EntityInfoDeps:
    """Dependency container for entity-info orchestration."""

    conn: sqlite3.Connection
    client: Any
    dm_peer_ids: Callable[[], set[int]]
    get_peer_id: Callable[[Any], int]
    rid: Callable[[], str]
    logger: Any
    now_provider: Callable[[], float]
    get_common_chats_request: Callable[..., Any]
    get_dialog_filters_request: Callable[..., Any]
    get_full_user_request: Callable[..., Any]
    get_user_photos_request: Callable[..., Any]
    get_messages_search_request: Callable[..., Any]
    get_full_channel_request: Callable[..., Any]
    get_participants_request: Callable[..., Any]
    channel_participants_contacts_request: Callable[..., Any]
    get_full_chat_request: Callable[..., Any]
    input_messages_filter_chat_photos: Any
    message_action_chat_edit_photo: Any
    chat_reactions_all: Any
    chat_reactions_some: Any
    chat_reactions_none: Any
    channel_type: Any
    chat_type: Any


class DaemonEntityInfoService:
    """Entity-info extraction service used by ``DaemonAPIServer._get_entity_info``."""

    def __init__(self, deps: EntityInfoDeps) -> None:
        self._deps = deps

    async def get_entity_info(self, req: dict) -> dict:
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

    def _extract_entity_id(self, req: dict) -> int | None:
        entity_id = req.get("entity_id")
        if not isinstance(entity_id, int):
            return None
        return entity_id

    def _load_cached_detail(self, entity_id: int, now: int) -> dict | None:
        try:
            row = self._deps.conn.execute(
                "SELECT detail_json, fetched_at FROM entity_details WHERE entity_id = ?",
                (entity_id,),
            ).fetchone()
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
                    detail = json.loads(detail_json)
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

    async def _resolve_entity(self, entity_id: int) -> tuple[Any | None, dict | None]:
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

    async def _build_detail_by_type(self, entity: Any) -> tuple[dict[str, Any] | None, dict | None]:
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

    def _writeback(self, entity_id: int, detail: dict[str, Any], now: int) -> None:
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
    def _error(message_key: str, message: str) -> dict[str, Any]:
        return {
            "ok": False,
            "error": message_key,
            "message": message,
            "data": None,
        }

    @staticmethod
    def _strip_envelope_schema(detail: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in detail.items() if k != "schema"}

    @staticmethod
    def _format_user_status(status: object) -> dict[str, Any] | None:
        if status is None:
            return None
        status_type = type(status).__name__
        online_like = {
            "UserStatusOnline": ("online", "expires"),
            "UserStatusOffline": ("offline", "was_online"),
        }
        if status_type in online_like:
            key, ts_key = online_like[status_type]
            value = getattr(status, "expires" if ts_key == "expires" else "was_online", None)
            return {"type": key, ts_key: value.isoformat() if value else None}
        if status_type == "UserStatusRecently":
            return {"type": "recently"}
        if status_type == "UserStatusLastWeek":
            return {"type": "last_week"}
        if status_type == "UserStatusLastMonth":
            return {"type": "last_month"}
        return None

    async def _fetch_user_detail(self, user: Any) -> dict[str, Any]:
        user_id = int(user.id)

        common_chats = await self._collect_common_chats(user_id)
        profile = await self._collect_user_profile(user_id)
        profile["folder_name"] = await self._resolve_folder_name(profile["folder_id"])
        extra_usernames = self._collect_extra_usernames(user)
        emoji_status_id = self._collect_emoji_status_id(user)
        avatar_history, avatar_count = await self._collect_user_avatar_history(user)
        my_membership = self._build_user_membership(user, profile["blocked"])

        name = " ".join(part for part in (getattr(user, "first_name", None), getattr(user, "last_name", None)) if part)
        return {
            "id": user_id,
            "type": "bot" if bool(getattr(user, "bot", False)) else "user",
            "name": name or None,
            "username": getattr(user, "username", None),
            "about": profile["about"],
            "my_membership": my_membership,
            "avatar_history": avatar_history,
            "avatar_count": avatar_count,
            "first_name": getattr(user, "first_name", None),
            "last_name": getattr(user, "last_name", None),
            "extra_usernames": extra_usernames,
            "emoji_status_id": emoji_status_id,
            "status": self._format_user_status(getattr(user, "status", None)),
            "phone": getattr(user, "phone", None),
            "lang_code": getattr(user, "lang_code", None),
            "contact": bool(getattr(user, "contact", False)),
            "mutual_contact": bool(getattr(user, "mutual_contact", False)),
            "close_friend": bool(getattr(user, "close_friend", False)),
            "send_paid_messages_stars": getattr(user, "send_paid_messages_stars", None),
            "personal_channel_id": profile["personal_channel_id"],
            "birthday": profile["birthday"],
            "verified": bool(getattr(user, "verified", False)),
            "premium": bool(getattr(user, "premium", False)),
            "bot": bool(getattr(user, "bot", False)),
            "scam": bool(getattr(user, "scam", False)),
            "fake": bool(getattr(user, "fake", False)),
            "restricted": bool(getattr(user, "restricted", False)),
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

    async def _collect_common_chats(self, user_id: int) -> list[dict[str, Any]]:
        chats: list[dict[str, Any]] = []
        try:
            common_result = await self._deps.client(
                self._deps.get_common_chats_request(user_id=user_id, max_id=0, limit=100),
            )
            chats.extend(
                {
                    "id": int(self._deps.get_peer_id(chat)),
                    "name": getattr(chat, "title", None) or str(chat.id),
                    "type": self._classify_chat_type(chat),
                }
                for chat in getattr(common_result, "chats", [])
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

    def _classify_chat_type(self, chat: Any) -> str:
        if isinstance(chat, self._deps.channel_type):
            return "supergroup" if getattr(chat, "megagroup", False) else "channel"
        if isinstance(chat, self._deps.chat_type):
            return "group"
        return "user"

    async def _collect_user_profile(self, user_id: int) -> dict[str, Any]:
        profile: dict[str, Any] = {
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
            full_result = await self._deps.client(self._deps.get_full_user_request(id=user_id))
            user_full = full_result.full_user
            profile["about"] = getattr(user_full, "about", None) or None
            profile["personal_channel_id"] = getattr(user_full, "personal_channel_id", None)
            profile["blocked"] = bool(getattr(user_full, "blocked", False))
            profile["ttl_period"] = getattr(user_full, "ttl_period", None)
            profile["private_forward_name"] = getattr(user_full, "private_forward_name", None) or None
            profile["folder_id"] = getattr(user_full, "folder_id", None)
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

    def _extract_user_note(self, user_full: Any) -> str | None:
        raw_note = getattr(user_full, "note", None)
        if raw_note is None:
            return None
        return getattr(raw_note, "text", None) or None

    def _extract_user_bot_info(self, user_full: Any) -> dict[str, Any] | None:
        raw_bot_info = getattr(user_full, "bot_info", None)
        if raw_bot_info is None:
            return None
        return {
            "description": getattr(raw_bot_info, "description", None) or None,
            "commands": [
                {
                    "command": getattr(cmd, "command", ""),
                    "description": getattr(cmd, "description", ""),
                }
                for cmd in getattr(raw_bot_info, "commands", None) or []
            ],
        }

    def _extract_user_business_location(self, user_full: Any) -> dict[str, Any] | None:
        raw_loc = getattr(user_full, "business_location", None)
        if raw_loc is None:
            return None
        geo = getattr(raw_loc, "geo_point", None)
        return {
            "address": getattr(raw_loc, "address", None),
            "lat": getattr(geo, "lat", None) if geo else None,
            "long": getattr(geo, "long", None) if geo else None,
        }

    def _extract_user_business_intro(self, user_full: Any) -> dict[str, Any] | None:
        raw_intro = getattr(user_full, "business_intro", None)
        if raw_intro is None:
            return None
        return {
            "title": getattr(raw_intro, "title", None),
            "description": getattr(raw_intro, "description", None),
        }

    def _extract_user_business_work_hours(self, user_full: Any) -> dict[str, Any] | None:
        raw_hours = getattr(user_full, "business_work_hours", None)
        if raw_hours is None:
            return None
        return {"timezone": getattr(raw_hours, "timezone_id", None)}

    def _extract_user_birthday(self, user_full: Any) -> dict[str, Any] | None:
        bday = getattr(user_full, "birthday", None)
        if bday is None:
            return None
        return {
            "day": getattr(bday, "day", None),
            "month": getattr(bday, "month", None),
            "year": getattr(bday, "year", None),
        }

    async def _resolve_folder_name(self, folder_id: int | None) -> str | None:
        if folder_id is None:
            return None
        try:
            filters = await self._deps.client(self._deps.get_dialog_filters_request())
            for item in getattr(filters, "filters", filters) or []:
                if getattr(item, "id", None) != folder_id:
                    continue
                raw_title = getattr(item, "title", None)
                return getattr(raw_title, "text", raw_title) if raw_title else None
        except Exception as exc:  # noqa: BLE001
            self._deps.logger.warning(
                "entity_info user folder_resolve_failed folder_id=%r error=%s%s",
                folder_id,
                exc,
                self._deps.rid(),
            )
        return None

    def _collect_extra_usernames(self, user: Any) -> list[str]:
        extra_usernames: list[str] = []
        for uname in getattr(user, "usernames", None) or []:
            raw = getattr(uname, "username", None)
            if raw and raw != getattr(user, "username", None):
                extra_usernames.append(raw)
        return extra_usernames

    def _collect_emoji_status_id(self, user: Any) -> int | None:
        emoji_status = getattr(user, "emoji_status", None)
        if emoji_status is None:
            return None
        return getattr(emoji_status, "document_id", None)

    async def _collect_user_avatar_history(self, user: Any) -> tuple[list[dict[str, Any]], int]:
        avatar_history: list[dict[str, Any]] = []
        avatar_count = 0
        try:
            photos_result = await self._deps.client(
                self._deps.get_user_photos_request(user_id=user, offset=0, max_id=0, limit=100)
            )
            avatar_count = int(getattr(photos_result, "count", len(getattr(photos_result, "photos", []))))
            for photo in getattr(photos_result, "photos", []):
                photo_id = getattr(photo, "id", None)
                photo_date = getattr(photo, "date", None)
                if photo_id is None or photo_date is None:
                    continue
                avatar_history.append(
                    {"photo_id": int(photo_id), "date": photo_date.isoformat()},
                )
        except Exception as exc:
            self._deps.logger.warning(
                "entity_info user photos_failed user_id=%r error=%s%s",
                int(self._deps.get_peer_id(user)),
                exc,
                self._deps.rid(),
                exc_info=True,
            )
        return avatar_history, avatar_count

    def _build_user_membership(self, user: Any, blocked: bool) -> dict[str, Any]:
        contact_flag = bool(getattr(user, "contact", False))
        mutual_contact = bool(getattr(user, "mutual_contact", False))
        close_friend = bool(getattr(user, "close_friend", False))
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

    def _collect_restrictions(self, entity: Any) -> list[dict[str, Any]]:
        return [
            {
                "platform": getattr(rr, "platform", None),
                "reason": getattr(rr, "reason", None),
                "text": getattr(rr, "text", None),
            }
            for rr in getattr(entity, "restriction_reason", None) or []
        ]

    async def _search_chat_photo_history(self, peer: Any, full_chat: Any) -> tuple[list[dict[str, Any]], int]:
        """Avatar history via messages.Search(filter=ChatPhotos)."""
        peer_id = int(self._deps.get_peer_id(peer))
        avatar_history: list[dict[str, Any]] = []
        avatar_count = 0
        search_failed = False
        try:
            search_result = await self._deps.client(
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
            )
            avatar_count = int(getattr(search_result, "count", len(getattr(search_result, "messages", []))))
            for msg in getattr(search_result, "messages", []):
                action = getattr(msg, "action", None)
                if not isinstance(action, self._deps.message_action_chat_edit_photo):
                    continue
                photo = getattr(action, "photo", None)
                photo_date = getattr(msg, "date", None)
                if photo is None or photo_date is None or getattr(photo, "id", None) is None:
                    continue
                avatar_history.append(
                    {"photo_id": int(photo.id), "date": photo_date.isoformat()},
                )
        except (RPCError, TypeError, AttributeError, ValueError) as exc:
            search_failed = True
            self._deps.logger.warning(
                "entity_info avatar_search_failed peer_id=%r error=%s%s",
                peer_id,
                exc,
                self._deps.rid(),
            )

        chat_photo = getattr(full_chat, "chat_photo", None) if full_chat is not None else None
        current_photo_id = getattr(chat_photo, "id", None) if chat_photo is not None else None
        if current_photo_id is not None and not any(p["photo_id"] == int(current_photo_id) for p in avatar_history):
            chat_photo_date = getattr(chat_photo, "date", None)
            avatar_history.insert(
                0,
                {
                    "photo_id": int(current_photo_id),
                    "date": chat_photo_date.isoformat() if chat_photo_date is not None else None,
                },
            )
        if search_failed and current_photo_id is not None:
            avatar_count = max(avatar_count, 1)
        if not avatar_history and current_photo_id is not None:
            chat_photo_date = getattr(chat_photo, "date", None)
            avatar_history = [
                {
                    "photo_id": int(current_photo_id),
                    "date": chat_photo_date.isoformat() if chat_photo_date is not None else None,
                }
            ]
            avatar_count = max(avatar_count, 1)
        return avatar_history, avatar_count

    async def _fetch_channel_detail(self, channel: Any) -> dict[str, Any]:
        channel_id = int(self._deps.get_peer_id(channel))
        full_context = await self._collect_full_channel_context(channel, collect_reactions=True)
        memberships = self._build_chat_membership(channel)
        contacts_subscribed, contacts_subscribed_partial, contacts_reason = await self._collect_channel_contacts(
            channel,
            is_admin=memberships["is_admin"],
            subscribers_count=full_context["subscribers_count"],
        )
        avatar_history, avatar_count = await self._search_chat_photo_history(channel, full_context["full_chat"])

        return {
            "id": channel_id,
            "type": "channel",
            "name": getattr(channel, "title", None),
            "username": getattr(channel, "username", None),
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

    async def _collect_full_channel_context(self, channel: Any, *, collect_reactions: bool) -> dict[str, Any]:
        context: dict[str, Any] = {
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
            full_result = await self._deps.client(self._deps.get_full_channel_request(channel=channel))
            full_chat = full_result.full_chat
            context["full_chat"] = full_chat
            context["subscribers_count"] = getattr(full_chat, "participants_count", None)
            context["linked_chat_id"] = self._normalize_linked_chat_id(getattr(full_chat, "linked_chat_id", None))
            context["pinned_msg_id"] = getattr(full_chat, "pinned_msg_id", None)
            context["slow_mode_seconds"] = getattr(full_chat, "slowmode_seconds", None)
            context["about"] = getattr(full_chat, "about", None) or None
            if collect_reactions:
                context["available_reactions"] = self._collect_reactions(
                    getattr(full_chat, "available_reactions", None),
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

    def _collect_reactions(self, raw_reactions: Any) -> dict[str, Any]:
        if isinstance(raw_reactions, self._deps.chat_reactions_all):
            return {"kind": "all", "emojis": []}
        if isinstance(raw_reactions, self._deps.chat_reactions_some):
            emojis = [getattr(r, "emoticon", None) for r in getattr(raw_reactions, "reactions", []) or []]
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

    def _build_chat_membership(self, entity: Any) -> dict[str, Any]:
        is_creator = bool(getattr(entity, "creator", False))
        admin_rights_obj = getattr(entity, "admin_rights", None)
        admin_rights = self._extract_admin_rights(admin_rights_obj)
        is_admin = is_creator or (admin_rights is not None)
        return {
            "is_member": not bool(getattr(entity, "left", False)),
            "is_admin": is_admin,
            "admin_rights": admin_rights,
        }

    def _extract_admin_rights(self, admin_rights_obj: Any) -> dict[str, bool] | None:
        if admin_rights_obj is None:
            return None
        return {field: bool(getattr(admin_rights_obj, field, False)) for field in _ADMIN_RIGHT_FIELDS}

    async def _collect_channel_contacts(
        self,
        channel: Any,
        *,
        is_admin: bool,
        subscribers_count: int | None,
    ) -> tuple[list[dict[str, Any]] | None, bool, str | None]:
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
        channel: Any,
        *,
        not_admin_reason: str,
        small_error_log: str,
    ) -> tuple[list[dict[str, Any]] | None, bool, str | None]:
        try:
            participant_ids: set[int] = set()
            async for p in self._deps.client.iter_participants(channel, limit=1000):
                pid = getattr(p, "id", None)
                if pid is not None:
                    participant_ids.add(int(pid))
            intersect_ids = participant_ids & self._deps.dm_peer_ids()
            return self._enrich_contact_ids_with_names(intersect_ids), False, None
        except ChatAdminRequiredError:
            return None, False, not_admin_reason
        except (RPCError, TypeError, AttributeError, ValueError) as exc:
            self._deps.logger.warning(
                small_error_log,
                int(self._deps.get_peer_id(channel)),
                exc,
                self._deps.rid(),
            )
            return None, False, "enumeration_failed"

    async def _collect_contacts_via_filter(
        self,
        channel: Any,
        *,
        not_admin_reason: str,
        large_error_log: str,
    ) -> tuple[list[dict[str, Any]] | None, bool, str | None]:
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
            contact_ids = {int(u.id) for u in getattr(gp_result, "users", []) if hasattr(u, "id")}
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

    def _enrich_contact_ids_with_names(self, ids: set[int]) -> list[dict[str, Any]]:
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = self._deps.conn.execute(
            f"SELECT id, name, username FROM entities WHERE id IN ({placeholders})",
            tuple(ids),
        ).fetchall()
        seen = {row[0] for row in rows}
        out = [{"id": row[0], "name": row[1], "username": row[2]} for row in rows]
        out.extend({"id": missing_id, "name": None, "username": None} for missing_id in ids - seen)
        return sorted(out, key=lambda d: ((d["name"] or ""), d["id"]))

    async def _fetch_supergroup_detail(self, channel: Any) -> dict[str, Any]:
        channel_id = int(self._deps.get_peer_id(channel))
        full_context = await self._collect_full_channel_context(channel, collect_reactions=False)
        memberships = self._build_chat_membership(channel)
        hidden_members = bool(getattr(channel, "hidden_members", False)) and not memberships["is_admin"]
        contacts_subscribed, contacts_subscribed_partial, contacts_reason = await self._collect_supergroup_contacts(
            channel,
            is_admin=memberships["is_admin"],
            members_count=full_context["subscribers_count"],
            hidden_members=hidden_members,
        )
        avatar_history, avatar_count = await self._search_chat_photo_history(channel, full_context["full_chat"])

        return {
            "id": channel_id,
            "type": "supergroup",
            "name": getattr(channel, "title", None),
            "username": getattr(channel, "username", None),
            "about": full_context["about"],
            "my_membership": memberships,
            "avatar_history": avatar_history,
            "avatar_count": avatar_count,
            "members_count": full_context["subscribers_count"],
            "linked_broadcast_id": full_context["linked_chat_id"],
            "slow_mode_seconds": full_context["slow_mode_seconds"],
            "has_topics": bool(getattr(channel, "forum", False)),
            "restrictions": self._collect_restrictions(channel),
            "contacts_subscribed": contacts_subscribed,
            "contacts_subscribed_partial": contacts_subscribed_partial,
            "contacts_reason": contacts_reason,
            "_full_fetch_ok": full_context["full_channel_ok"],
        }

    async def _collect_supergroup_contacts(
        self,
        channel: Any,
        *,
        is_admin: bool,
        members_count: int | None,
        hidden_members: bool,
    ) -> tuple[list[dict[str, Any]] | None, bool, str | None]:
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
                pid = getattr(participant, "id", None)
                if pid is not None:
                    participant_ids.add(int(pid))
            intersect_ids = participant_ids & self._deps.dm_peer_ids()
            return self._enrich_contact_ids_with_names(intersect_ids), False, None
        except ChatAdminRequiredError:
            return None, False, "hidden_by_admin"
        except (RPCError, TypeError, AttributeError, ValueError) as exc:
            self._deps.logger.warning(
                "entity_info supergroup iter_participants_failed channel_id=%r error=%s%s",
                int(self._deps.get_peer_id(channel)),
                exc,
                self._deps.rid(),
            )
            return None, False, "enumeration_failed"

    async def _fetch_group_detail(self, chat: Any) -> dict[str, Any]:
        chat_id = int(self._deps.get_peer_id(chat))
        migrated_to = self._resolve_group_migrated_to(chat)
        group_meta = await self._collect_group_full_chat(chat)
        my_membership = self._build_chat_membership(chat)

        contacts_subscribed: list[dict[str, Any]] | None = []
        contacts_reason: str | None = None
        try:
            participant_ids = self._extract_group_participants(group_meta["participants"])
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
            "name": getattr(chat, "title", None),
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

    def _resolve_group_migrated_to(self, chat: Any) -> int | None:
        migrated_to_obj = getattr(chat, "migrated_to", None)
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

    async def _collect_group_full_chat(self, chat: Any) -> dict[str, Any]:
        group_meta: dict[str, Any] = {
            "full_chat": None,
            "about": None,
            "invite_link": None,
            "participants": [],
            "members_count": None,
        }
        try:
            full_result = await self._deps.client(self._deps.get_full_chat_request(chat_id=int(chat.id)))
            full_chat = full_result.full_chat
            group_meta["full_chat"] = full_chat
            group_meta["about"] = getattr(full_chat, "about", None) or None
            exported_invite = getattr(full_chat, "exported_invite", None)
            if exported_invite is not None:
                group_meta["invite_link"] = getattr(exported_invite, "link", None)
            raw_participants = getattr(full_chat, "participants", None)
            if raw_participants is not None:
                participants = list(getattr(raw_participants, "participants", []) or [])
                group_meta["participants"] = participants
                group_meta["members_count"] = len(participants)
            if group_meta["members_count"] is None:
                group_meta["members_count"] = getattr(chat, "participants_count", None)
        except Exception as exc:
            self._deps.logger.warning(
                "entity_info group full_chat_failed chat_id=%r error=%s%s",
                int(self._deps.get_peer_id(chat)),
                exc,
                self._deps.rid(),
                exc_info=True,
            )
        return group_meta

    def _extract_group_participants(self, participants: list[Any]) -> set[int]:
        return {
            int(p_user_id)
            for p in participants
            if (p_user_id := getattr(p, "user_id", None)) is not None and int(p_user_id) != 0
        }
