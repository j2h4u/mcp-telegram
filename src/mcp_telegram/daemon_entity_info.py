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
        """Type-tagged entity inspector covering 5 Telegram entity kinds.

        DB-first read from sync.db.entity_details; live MTProto fetch only on
        cache miss or staleness. Returns one of five 'type' discriminators:
        ``user`` | ``bot`` | ``channel`` | ``supergroup`` | ``group``.

        Request schema: ``{method: \"get_entity_info\", entity_id: int}``
        Response schema: same as previous daemon_api implementation.
        """
        entity_id = req.get("entity_id")
        if not isinstance(entity_id, int):
            return {
                "ok": False,
                "error": "telegram_api_error",
                "message": "entity_id missing or not an integer",
                "data": None,
            }

        now = int(self._deps.now_provider())

        # ----- DB-first read (SPEC Req 8) -----
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
            return {
                "ok": False,
                "error": "db_unavailable",
                "message": str(exc),
                "data": None,
            }

        if row is not None:
            detail_json, fetched_at = row
            if now - fetched_at < _ENTITY_DETAIL_TTL_SECONDS:
                # Cache HIT, fresh
                try:
                    detail = json.loads(detail_json)
                except json.JSONDecodeError:
                    self._deps.logger.warning(
                        "entity_info detail_json_corrupt entity_id=%r%s — treating as cache miss",
                        entity_id,
                        self._deps.rid(),
                    )
                    detail = None
                if detail is not None and detail.get("schema") == _ENTITY_DETAIL_SCHEMA_VERSION:
                    return {"ok": True, "data": self._strip_envelope_schema(detail)}
                # Schema mismatch or corrupt JSON → fall through to live fetch.

        # ----- Cache miss / stale: live fetch -----
        try:
            entity = await self._deps.client.get_entity(entity_id)
        except (ValueError, KeyError) as exc:
            self._deps.logger.warning(
                "entity_info entity_not_found entity_id=%r error=%s%s",
                entity_id,
                exc,
                self._deps.rid(),
            )
            return {
                "ok": False,
                "error": "entity_not_found",
                "message": str(exc),
                "data": None,
            }
        except Exception as exc:
            self._deps.logger.warning(
                "entity_info get_entity_failed entity_id=%r error=%s%s",
                entity_id,
                exc,
                self._deps.rid(),
                exc_info=True,
            )
            return {
                "ok": False,
                "error": "telegram_api_error",
                "message": str(exc),
                "data": None,
            }

        # ----- Dispatch by type -----
        dispatch_kind = DialogType.from_entity(entity)

        if dispatch_kind in (DialogType.USER, DialogType.BOT):
            detail = await self._fetch_user_detail(entity)
        elif dispatch_kind == DialogType.CHANNEL:
            detail = await self._fetch_channel_detail(entity)
        elif dispatch_kind in (DialogType.SUPERGROUP, DialogType.FORUM):
            detail = await self._fetch_supergroup_detail(entity)
        elif dispatch_kind == DialogType.GROUP:
            detail = await self._fetch_group_detail(entity)
        else:
            return {
                "ok": False,
                "error": "unsupported_entity_type",
                "message": f"unknown entity kind: {dispatch_kind}",
                "data": None,
            }

        if detail is None:
            return {
                "ok": False,
                "error": "telegram_api_error",
                "message": "per-type helper returned no detail",
                "data": None,
            }

        # ----- Write back: entities + entity_details -----
        # Pop internal marker before caching; skip entity_details when full fetch failed.
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

        return {"ok": True, "data": detail}

    @staticmethod
    def _strip_envelope_schema(detail: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in detail.items() if k != "schema"}

    @staticmethod
    def _format_user_status(status: object) -> dict[str, Any] | None:
        """Serialize a Telethon UserStatus object to a plain dict.

        Returns None for UserStatusEmpty or missing status.
        """
        if status is None:
            return None
        type_name = type(status).__name__
        if type_name == "UserStatusOnline":
            expires = getattr(status, "expires", None)
            return {"type": "online", "expires": expires.isoformat() if expires else None}
        if type_name == "UserStatusOffline":
            was_online = getattr(status, "was_online", None)
            return {"type": "offline", "was_online": was_online.isoformat() if was_online else None}
        if type_name == "UserStatusRecently":
            return {"type": "recently"}
        if type_name == "UserStatusLastWeek":
            return {"type": "last_week"}
        if type_name == "UserStatusLastMonth":
            return {"type": "last_month"}
        return None

    async def _fetch_user_detail(self, user: Any) -> dict[str, Any]:
        """Per-type helper: User/Bot detail."""
        user_id = int(user.id)

        # --- common_chats (existing _fetch_user_detail body) ---
        common_chats: list[dict[str, Any]] = []
        try:
            common_result = await self._deps.client(
                self._deps.get_common_chats_request(user_id=user_id, max_id=0, limit=100),
            )
            for chat in getattr(common_result, "chats", []):
                if isinstance(chat, self._deps.channel_type):
                    chat_type = "supergroup" if getattr(chat, "megagroup", False) else "channel"
                elif isinstance(chat, self._deps.chat_type):
                    chat_type = "group"
                else:
                    chat_type = "user"
                common_chats.append(
                    {
                        "id": int(self._deps.get_peer_id(chat)),
                        "name": getattr(chat, "title", None) or str(chat.id),
                        "type": chat_type,
                    }
                )
        except Exception as exc:
            self._deps.logger.warning(
                "entity_info user common_chats_failed user_id=%r error=%s%s",
                user_id,
                exc,
                self._deps.rid(),
                exc_info=True,
            )

        about: str | None = None
        personal_channel_id: int | None = None
        birthday: dict[str, Any] | None = None
        blocked: bool = False
        ttl_period: int | None = None
        private_forward_name: str | None = None
        bot_info: dict[str, Any] | None = None
        business_location: dict[str, Any] | None = None
        business_intro: dict[str, Any] | None = None
        business_work_hours: dict[str, Any] | None = None
        note: str | None = None
        folder_id: int | None = None
        folder_name: str | None = None
        full_user_ok = False
        try:
            full_result = await self._deps.client(self._deps.get_full_user_request(id=user_id))
            user_full = full_result.full_user
            about = getattr(user_full, "about", None) or None
            personal_channel_id = getattr(user_full, "personal_channel_id", None)
            blocked = bool(getattr(user_full, "blocked", False))
            ttl_period = getattr(user_full, "ttl_period", None)
            private_forward_name = getattr(user_full, "private_forward_name", None) or None
            folder_id = getattr(user_full, "folder_id", None)
            bday = getattr(user_full, "birthday", None)
            if bday is not None:
                birthday = {
                    "day": getattr(bday, "day", None),
                    "month": getattr(bday, "month", None),
                    "year": getattr(bday, "year", None),
                }
            raw_bot_info = getattr(user_full, "bot_info", None)
            if raw_bot_info is not None:
                commands = [
                    {
                        "command": getattr(cmd, "command", ""),
                        "description": getattr(cmd, "description", ""),
                    }
                    for cmd in getattr(raw_bot_info, "commands", None) or []
                ]
                bot_info = {
                    "description": getattr(raw_bot_info, "description", None) or None,
                    "commands": commands,
                }
            raw_loc = getattr(user_full, "business_location", None)
            if raw_loc is not None:
                geo = getattr(raw_loc, "geo_point", None)
                business_location = {
                    "address": getattr(raw_loc, "address", None),
                    "lat": getattr(geo, "lat", None) if geo else None,
                    "long": getattr(geo, "long", None) if geo else None,
                }
            raw_intro = getattr(user_full, "business_intro", None)
            if raw_intro is not None:
                business_intro = {
                    "title": getattr(raw_intro, "title", None),
                    "description": getattr(raw_intro, "description", None),
                }
            raw_hours = getattr(user_full, "business_work_hours", None)
            if raw_hours is not None:
                business_work_hours = {"timezone": getattr(raw_hours, "timezone_id", None)}
            raw_note = getattr(user_full, "note", None)
            if raw_note is not None:
                note = getattr(raw_note, "text", None) or None
            full_user_ok = True
        except Exception as exc:
            self._deps.logger.warning(
                "entity_info user full_user_failed user_id=%r error=%s%s",
                user_id,
                exc,
                self._deps.rid(),
                exc_info=True,
            )

        if folder_id is not None:
            try:
                filters = await self._deps.client(self._deps.get_dialog_filters_request())
                filter_list = getattr(filters, "filters", filters) or []
                for f in filter_list:
                    if getattr(f, "id", None) == folder_id:
                        raw_title = getattr(f, "title", None)
                        folder_name = getattr(raw_title, "text", raw_title) if raw_title else None
                        break
            except Exception as exc:
                self._deps.logger.warning(
                    "entity_info user folder_resolve_failed folder_id=%r error=%s%s",
                    folder_id,
                    exc,
                    self._deps.rid(),
                    exc_info=True,
                )

        extra_usernames: list[str] = []
        for uname in getattr(user, "usernames", None) or []:
            name_str = getattr(uname, "username", None)
            if name_str and name_str != getattr(user, "username", None):
                extra_usernames.append(name_str)

        emoji_status = getattr(user, "emoji_status", None)
        emoji_status_id: int | None = None
        if emoji_status is not None:
            emoji_status_id = getattr(emoji_status, "document_id", None)

        restriction_reason = [
            {
                "platform": getattr(rr, "platform", None),
                "reason": getattr(rr, "reason", None),
                "text": getattr(rr, "text", None),
            }
            for rr in getattr(user, "restriction_reason", None) or []
        ]

        avatar_history: list[dict[str, Any]] = []
        avatar_count: int = 0
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
                    {
                        "photo_id": int(photo_id),
                        "date": photo_date.isoformat(),
                    }
                )
        except Exception as exc:
            self._deps.logger.warning(
                "entity_info user photos_failed user_id=%r error=%s%s",
                user_id,
                exc,
                self._deps.rid(),
                exc_info=True,
            )

        first_name = getattr(user, "first_name", None)
        last_name = getattr(user, "last_name", None)
        name = " ".join(part for part in (first_name, last_name) if part)
        username = getattr(user, "username", None)

        contact_flag = bool(getattr(user, "contact", False))
        mutual_contact = bool(getattr(user, "mutual_contact", False))
        close_friend = bool(getattr(user, "close_friend", False))
        my_membership = {
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

        entity_type = "bot" if bool(getattr(user, "bot", False)) else "user"

        return {
            "id": user_id,
            "type": entity_type,
            "name": name or None,
            "username": username,
            "about": about,
            "my_membership": my_membership,
            "avatar_history": avatar_history,
            "avatar_count": avatar_count,
            "first_name": first_name,
            "last_name": last_name,
            "extra_usernames": extra_usernames,
            "emoji_status_id": emoji_status_id,
            "status": self._format_user_status(getattr(user, "status", None)),
            "phone": getattr(user, "phone", None),
            "lang_code": getattr(user, "lang_code", None),
            "contact": contact_flag,
            "mutual_contact": mutual_contact,
            "close_friend": close_friend,
            "send_paid_messages_stars": getattr(user, "send_paid_messages_stars", None),
            "personal_channel_id": personal_channel_id,
            "birthday": birthday,
            "verified": bool(getattr(user, "verified", False)),
            "premium": bool(getattr(user, "premium", False)),
            "bot": bool(getattr(user, "bot", False)),
            "scam": bool(getattr(user, "scam", False)),
            "fake": bool(getattr(user, "fake", False)),
            "restricted": bool(getattr(user, "restricted", False)),
            "restriction_reason": restriction_reason,
            "blocked": blocked,
            "ttl_period": ttl_period,
            "private_forward_name": private_forward_name,
            "bot_info": bot_info,
            "business_location": business_location,
            "business_intro": business_intro,
            "business_work_hours": business_work_hours,
            "note": note,
            "folder_id": folder_id,
            "folder_name": folder_name,
            "common_chats": common_chats,
            "_full_fetch_ok": full_user_ok,
        }

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
                if isinstance(action, self._deps.message_action_chat_edit_photo):
                    photo = getattr(action, "photo", None)
                    photo_date = getattr(msg, "date", None)
                    if photo is not None and photo_date is not None and getattr(photo, "id", None) is not None:
                        avatar_history.append(
                            {
                                "photo_id": int(photo.id),
                                "date": photo_date.isoformat(),
                            }
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
        """Per-type helper: Broadcast Channel detail (megagroup=False)."""
        channel_id = int(self._deps.get_peer_id(channel))

        full_chat = None
        subscribers_count: int | None = None
        linked_chat_id: int | None = None
        pinned_msg_id: int | None = None
        slow_mode_seconds: int | None = None
        available_reactions: dict[str, Any] = {"kind": "none", "emojis": []}
        about: str | None = None
        full_channel_ok = False
        try:
            full_result = await self._deps.client(self._deps.get_full_channel_request(channel=channel))
            full_chat = full_result.full_chat
            subscribers_count = getattr(full_chat, "participants_count", None)
            linked_chat_id_raw = getattr(full_chat, "linked_chat_id", None)
            if linked_chat_id_raw is not None:
                if linked_chat_id_raw > 0:
                    linked_chat_id = int(self._deps.get_peer_id(PeerChannel(linked_chat_id_raw)))
                else:
                    linked_chat_id = int(linked_chat_id_raw)
            pinned_msg_id = getattr(full_chat, "pinned_msg_id", None)
            slow_mode_seconds = getattr(full_chat, "slowmode_seconds", None)
            about = getattr(full_chat, "about", None) or None

            raw_reactions = getattr(full_chat, "available_reactions", None)
            if isinstance(raw_reactions, self._deps.chat_reactions_all):
                available_reactions = {"kind": "all", "emojis": []}
            elif isinstance(raw_reactions, self._deps.chat_reactions_some):
                emojis = []
                for r in getattr(raw_reactions, "reactions", []) or []:
                    em = getattr(r, "emoticon", None)
                    if em:
                        emojis.append(em)
                available_reactions = {"kind": "some", "emojis": emojis}
            elif isinstance(raw_reactions, self._deps.chat_reactions_none) or raw_reactions is None:
                available_reactions = {"kind": "none", "emojis": []}
            full_channel_ok = True
        except Exception as exc:
            self._deps.logger.warning(
                "entity_info channel full_channel_failed channel_id=%r error=%s%s",
                channel_id,
                exc,
                self._deps.rid(),
                exc_info=True,
            )

        restrictions = [
            {
                "platform": getattr(rr, "platform", None),
                "reason": getattr(rr, "reason", None),
                "text": getattr(rr, "text", None),
            }
            for rr in getattr(channel, "restriction_reason", None) or []
        ]

        is_creator = bool(getattr(channel, "creator", False))
        admin_rights_obj = getattr(channel, "admin_rights", None)
        is_admin = is_creator or (admin_rights_obj is not None)
        my_membership = {
            "is_member": not bool(getattr(channel, "left", False)),
            "is_admin": is_admin,
            "admin_rights": (
                {
                    field: bool(getattr(admin_rights_obj, field, False))
                    for field in (
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
                }
                if admin_rights_obj is not None
                else None
            ),
        }

        contacts_subscribed = None
        contacts_subscribed_partial = False
        contacts_reason: str | None = None

        if not is_admin:
            contacts_subscribed = None
            contacts_reason = "not_an_admin"
        elif subscribers_count is None:
            contacts_subscribed = None
            contacts_reason = "count_unavailable"
        elif subscribers_count > _MEMBERSHIP_THRESHOLD_LARGE:
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
                dm_peers = self._deps.dm_peer_ids()
                intersect_ids = contact_ids & dm_peers
                contacts_subscribed = self._enrich_contact_ids_with_names(intersect_ids)
                contacts_subscribed_partial = True
                contacts_reason = "too_large"
            except ChatAdminRequiredError:
                contacts_subscribed = None
                contacts_reason = "not_an_admin"
            except (RPCError, TypeError, AttributeError, ValueError) as exc:
                self._deps.logger.warning(
                    "entity_info channel contacts_enumeration_failed channel_id=%r error=%s%s",
                    channel_id,
                    exc,
                    self._deps.rid(),
                )
                contacts_subscribed = None
                contacts_reason = "enumeration_failed"
        else:
            try:
                participant_ids: set[int] = set()
                async for p in self._deps.client.iter_participants(channel, limit=1000):
                    pid = getattr(p, "id", None)
                    if pid is not None:
                        participant_ids.add(int(pid))
                dm_peers = self._deps.dm_peer_ids()
                intersect_ids = participant_ids & dm_peers
                contacts_subscribed = self._enrich_contact_ids_with_names(intersect_ids)
                contacts_subscribed_partial = False
                contacts_reason = None
            except ChatAdminRequiredError:
                contacts_subscribed = None
                contacts_reason = "not_an_admin"
            except (RPCError, TypeError, AttributeError, ValueError) as exc:
                self._deps.logger.warning(
                    "entity_info channel contacts_enumeration_failed channel_id=%r error=%s%s",
                    channel_id,
                    exc,
                    self._deps.rid(),
                )
                contacts_subscribed = None
                contacts_reason = "enumeration_failed"

        avatar_history, avatar_count = await self._search_chat_photo_history(channel, full_chat)

        title = getattr(channel, "title", None)
        username = getattr(channel, "username", None)

        return {
            "id": channel_id,
            "type": "channel",
            "name": title,
            "username": username,
            "about": about,
            "my_membership": my_membership,
            "avatar_history": avatar_history,
            "avatar_count": avatar_count,
            "subscribers_count": subscribers_count,
            "linked_chat_id": linked_chat_id,
            "pinned_msg_id": pinned_msg_id,
            "slow_mode_seconds": slow_mode_seconds,
            "available_reactions": available_reactions,
            "restrictions": restrictions,
            "contacts_subscribed": contacts_subscribed,
            "contacts_subscribed_partial": contacts_subscribed_partial,
            "contacts_reason": contacts_reason,
            "_full_fetch_ok": full_channel_ok,
        }

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
        """Per-type helper: Supergroup (Channel.megagroup=True)."""
        channel_id = int(self._deps.get_peer_id(channel))

        full_chat = None
        members_count: int | None = None
        linked_broadcast_id: int | None = None
        slow_mode_seconds: int | None = None
        about: str | None = None
        full_channel_ok = False
        try:
            full_result = await self._deps.client(self._deps.get_full_channel_request(channel=channel))
            full_chat = full_result.full_chat
            members_count = getattr(full_chat, "participants_count", None)
            linked_chat_raw = getattr(full_chat, "linked_chat_id", None)
            if linked_chat_raw is not None:
                if linked_chat_raw > 0:
                    linked_broadcast_id = int(self._deps.get_peer_id(PeerChannel(linked_chat_raw)))
                else:
                    linked_broadcast_id = int(linked_chat_raw)
            slow_mode_seconds = getattr(full_chat, "slowmode_seconds", None)
            about = getattr(full_chat, "about", None) or None
            full_channel_ok = True
        except Exception as exc:
            self._deps.logger.warning(
                "entity_info supergroup full_channel_failed channel_id=%r error=%s%s",
                channel_id,
                exc,
                self._deps.rid(),
                exc_info=True,
            )

        restrictions = [
            {
                "platform": getattr(rr, "platform", None),
                "reason": getattr(rr, "reason", None),
                "text": getattr(rr, "text", None),
            }
            for rr in getattr(channel, "restriction_reason", None) or []
        ]

        is_creator = bool(getattr(channel, "creator", False))
        admin_rights_obj = getattr(channel, "admin_rights", None)
        is_admin = is_creator or (admin_rights_obj is not None)
        my_membership = {
            "is_member": not bool(getattr(channel, "left", False)),
            "is_admin": is_admin,
            "admin_rights": (
                {
                    field: bool(getattr(admin_rights_obj, field, False))
                    for field in (
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
                }
                if admin_rights_obj is not None
                else None
            ),
        }

        contacts_subscribed = None
        contacts_subscribed_partial = False
        contacts_reason: str | None = None

        hidden_members = bool(getattr(channel, "hidden_members", False)) and not is_admin

        if hidden_members:
            contacts_subscribed = None
            contacts_reason = "hidden_by_admin"
        elif members_count is None:
            contacts_subscribed = None
            contacts_reason = "count_unavailable"
        elif members_count > _MEMBERSHIP_THRESHOLD_LARGE:
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
                dm_peers = self._deps.dm_peer_ids()
                intersect_ids = contact_ids & dm_peers
                contacts_subscribed = self._enrich_contact_ids_with_names(intersect_ids)
                contacts_subscribed_partial = True
                contacts_reason = "too_large"
            except ChatAdminRequiredError:
                contacts_subscribed = None
                contacts_reason = "hidden_by_admin"
            except Exception as exc:
                self._deps.logger.warning(
                    "entity_info supergroup contacts_filter_failed channel_id=%r error=%s%s",
                    channel_id,
                    exc,
                    self._deps.rid(),
                    exc_info=True,
                )
                contacts_subscribed = None
                contacts_reason = "enumeration_failed"
        else:
            try:
                participant_ids: set[int] = set()
                async for participant in self._deps.client.iter_participants(channel, limit=1000):
                    pid = getattr(participant, "id", None)
                    if pid is not None:
                        participant_ids.add(int(pid))
                dm_peers = self._deps.dm_peer_ids()
                intersect_ids = participant_ids & dm_peers
                contacts_subscribed = self._enrich_contact_ids_with_names(intersect_ids)
                contacts_subscribed_partial = False
            except ChatAdminRequiredError:
                contacts_subscribed = None
                contacts_reason = "hidden_by_admin"
            except Exception as exc:
                self._deps.logger.warning(
                    "entity_info supergroup iter_participants_failed channel_id=%r error=%s%s",
                    channel_id,
                    exc,
                    self._deps.rid(),
                    exc_info=True,
                )
                contacts_subscribed = None
                contacts_reason = "enumeration_failed"

        avatar_history, avatar_count = await self._search_chat_photo_history(channel, full_chat)

        title = getattr(channel, "title", None)
        username = getattr(channel, "username", None)

        return {
            "id": channel_id,
            "type": "supergroup",
            "name": title,
            "username": username,
            "about": about,
            "my_membership": my_membership,
            "avatar_history": avatar_history,
            "avatar_count": avatar_count,
            "members_count": members_count,
            "linked_broadcast_id": linked_broadcast_id,
            "slow_mode_seconds": slow_mode_seconds,
            "has_topics": bool(getattr(channel, "forum", False)),
            "restrictions": restrictions,
            "contacts_subscribed": contacts_subscribed,
            "contacts_subscribed_partial": contacts_subscribed_partial,
            "contacts_reason": contacts_reason,
            "_full_fetch_ok": full_channel_ok,
        }

    async def _fetch_group_detail(self, chat: Any) -> dict[str, Any]:
        """Per-type helper: legacy basic Chat (Telethon Chat, not Channel)."""
        chat_id = int(self._deps.get_peer_id(chat))

        migrated_to_obj = getattr(chat, "migrated_to", None)
        migrated_to: int | None = None
        if migrated_to_obj is not None:
            try:
                migrated_to = int(self._deps.get_peer_id(migrated_to_obj))
            except (TypeError, ValueError) as exc:
                self._deps.logger.warning(
                    "entity_info group migrated_to_normalize_failed chat_id=%r error=%s%s",
                    chat_id,
                    exc,
                    self._deps.rid(),
                )

        full_chat = None
        members_count: int | None = None
        about: str | None = None
        invite_link: str | None = None
        participants_objs: list = []
        try:
            full_result = await self._deps.client(self._deps.get_full_chat_request(chat_id=int(chat.id)))
            full_chat = full_result.full_chat
            about = getattr(full_chat, "about", None) or None
            exported_invite = getattr(full_chat, "exported_invite", None)
            if exported_invite is not None:
                invite_link = getattr(exported_invite, "link", None)
            raw_participants = getattr(full_chat, "participants", None)
            if raw_participants is not None:
                participants_objs = list(getattr(raw_participants, "participants", []) or [])
                members_count = len(participants_objs)
            if members_count is None:
                members_count = getattr(chat, "participants_count", None)
        except Exception as exc:
            self._deps.logger.warning(
                "entity_info group full_chat_failed chat_id=%r error=%s%s",
                chat_id,
                exc,
                self._deps.rid(),
                exc_info=True,
            )

        restrictions = [
            {
                "platform": getattr(rr, "platform", None),
                "reason": getattr(rr, "reason", None),
                "text": getattr(rr, "text", None),
            }
            for rr in getattr(chat, "restriction_reason", None) or []
        ]

        is_creator = bool(getattr(chat, "creator", False))
        admin_rights_obj = getattr(chat, "admin_rights", None)
        is_admin = is_creator or (admin_rights_obj is not None)
        my_membership = {
            "is_member": not bool(getattr(chat, "left", False)),
            "is_admin": is_admin,
            "admin_rights": (
                {
                    field: bool(getattr(admin_rights_obj, field, False))
                    for field in (
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
                }
                if admin_rights_obj is not None
                else None
            ),
        }

        contacts_subscribed = None
        contacts_subscribed_partial = False
        contacts_reason: str | None = None
        try:
            participant_ids = {
                int(p_user_id)
                for p in participants_objs
                if (p_user_id := getattr(p, "user_id", None)) is not None and int(p_user_id) != 0
            }
            dm_peers = self._deps.dm_peer_ids()
            intersect_ids = participant_ids & dm_peers
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

        avatar_history, avatar_count = await self._search_chat_photo_history(chat, full_chat)

        title = getattr(chat, "title", None)

        return {
            "id": chat_id,
            "type": "group",
            "name": title,
            "username": None,
            "about": about,
            "my_membership": my_membership,
            "avatar_history": avatar_history,
            "avatar_count": avatar_count,
            "members_count": members_count,
            "migrated_to": migrated_to,
            "invite_link": invite_link,
            "restrictions": restrictions,
            "contacts_subscribed": contacts_subscribed,
            "contacts_subscribed_partial": contacts_subscribed_partial,
            "contacts_reason": contacts_reason,
            "_full_fetch_ok": True,
        }
