"""MCP tool: GetEntityInfo — universal entity inspector (Phase 47).

Universal replacement covering User / Bot / BroadcastChannel / Supergroup /
LegacyChat. DB-first cache with the configured entity-detail TTL on the daemon side; tool itself
maps daemon data into the structured MCP response.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

import phonenumbers
from pydantic import ConfigDict, Field, model_validator

from ..errors import (
    entity_not_found_text,
    fetch_entity_info_error_text,
)
from ..models import DialogType
from ._base import (
    DaemonNotRunningError,
    ToolAnnotations,
    ToolArgs,
    ToolResult,
    _daemon_not_running_text,
    daemon_connection,
    error_result,
    mcp_tool,
    structured_result,
)
from .structured import (
    TELEGRAM_CONTENT_OUTPUT_SCHEMA,
    StructuredWarning,
    TelegramContentKind,
    structured_warning,
    telegram_content,
)

GET_ENTITY_INFO_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "resolved_query": {
            "type": "object",
            "properties": {
                "input": {"type": "string"},
                "resolution": {"type": "string"},
                "entity_id": {"type": "integer"},
                "display_name": {"type": "string"},
            },
            "required": ["input", "resolution", "entity_id", "display_name"],
            "additionalProperties": True,
        },
        "entity_id": {"type": "integer"},
        "display_name": {"type": "string"},
        "type": {"type": "string", "enum": ["user", "bot", "channel", "supergroup", "group", "unknown"]},
        "common": {"type": "object", "additionalProperties": True},
        "avatar_history": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "type_specific": {"type": "object", "additionalProperties": True},
        "relationships": {"type": "object", "additionalProperties": True},
        "privacy_or_access": {"type": "object", "additionalProperties": True},
        "warnings": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "content_fields": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "dialog_placement": {
            "type": "object",
            "properties": {
                "archived": {"type": "boolean"},
                "folders": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer"},
                            "title": TELEGRAM_CONTENT_OUTPUT_SCHEMA,
                        },
                        "required": ["id", "title"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["archived", "folders"],
            "additionalProperties": False,
        },
    },
    "required": [
        "resolved_query",
        "entity_id",
        "display_name",
        "type",
        "common",
        "avatar_history",
        "type_specific",
        "relationships",
        "privacy_or_access",
        "warnings",
        "content_fields",
        "dialog_placement",
    ],
    "additionalProperties": True,
}


# ---------------------------------------------------------------------------
# Helpers — preserved verbatim from the old tools/user_info.py
# (SPEC Req 4: User/Bot field-surface preservation extends to formatting).
# ---------------------------------------------------------------------------


def _format_relative_ymd(iso_date: str, now: datetime | None = None) -> str:
    """Render an ISO date as a coarse relative string (year/month/day granularity)."""
    try:
        then = datetime.fromisoformat(iso_date)
    except ValueError:
        return iso_date
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    reference = now or datetime.now(tz=UTC)
    delta_days = (reference.date() - then.date()).days
    if delta_days < 0:
        return "future date"
    if delta_days == 0:
        return "today"
    years, rem = divmod(delta_days, 365)
    months, days = divmod(rem, 30)
    parts: list[str] = []
    if years:
        parts.append(f"{years}y")
    if months:
        parts.append(f"{months}mo")
    if days and not years:
        parts.append(f"{days}d")
    return (" ".join(parts) + " ago") if parts else f"{delta_days}d ago"


def _phone_country(phone: str) -> str | None:
    try:
        parsed = phonenumbers.parse(phone)
        return phonenumbers.region_code_for_number(parsed)
    except phonenumbers.NumberParseException:
        return None


def _content_field(path: str, text: str | None, kind: TelegramContentKind) -> dict[str, object] | None:
    if not text:
        return None
    return {
        "field": path,
        "content": telegram_content(text, kind),
        "untrusted_content": True,
        "trust": {
            "source": "telegram",
            "is_untrusted": True,
        },
    }


def _entity_candidate_payload(match: dict) -> dict[str, object]:
    candidate: dict[str, object] = {
        "entity_id": match.get("entity_id"),
        "score": match.get("score"),
        "entity_type": match.get("entity_type"),
        "untrusted_content": True,
        "trust": {
            "source": "telegram",
            "is_untrusted": True,
        },
    }
    if display_name := match.get("display_name"):
        candidate["display_name_content"] = telegram_content(str(display_name), "message_text")
    if username := match.get("username"):
        candidate["username_content"] = telegram_content(str(username), "message_text")
    if hint := match.get("disambiguation_hint"):
        candidate["disambiguation_hint_content"] = telegram_content(str(hint), "message_text")
    return candidate


def _restriction_payloads(restrictions: list[dict] | None, *, base_path: str) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for idx, restriction in enumerate(restrictions or []):
        text = restriction.get("text")
        payloads.append(
            {
                "platform": restriction.get("platform"),
                "reason": restriction.get("reason"),
                "text": text,
                "content": (_content_field(f"{base_path}.{idx}.text", text, "restriction_reason") if text else None),
            }
        )
    return payloads


def _common_structured(data: dict, *, entity_id: int) -> dict[str, object]:
    about = data.get("about")
    return {
        "id": data.get("id", entity_id),
        "name": data.get("name"),
        "username": data.get("username"),
        "about": _content_field("common.about", about, "about") if about else None,
        "my_membership": data.get("my_membership") or {},
        "avatar_count": data.get("avatar_count") or len(data.get("avatar_history") or []),
    }


def _relationships_structured(data: dict) -> dict[str, object]:
    contacts = data.get("contacts_subscribed")
    return {
        "membership": data.get("my_membership") or {},
        "contact": {
            "contact": data.get("contact"),
            "mutual_contact": data.get("mutual_contact"),
            "close_friend": data.get("close_friend"),
            "blocked": data.get("blocked"),
        },
        "common_chats": data.get("common_chats") or [],
        "contacts_subscribed": {
            "items": contacts,
            "available": contacts is not None,
            "partial": data.get("contacts_subscribed_partial"),
            "reason": data.get("contacts_reason"),
        },
    }


def _privacy_or_access_structured(data: dict) -> dict[str, object]:
    contacts = data.get("contacts_subscribed")
    phone = data.get("phone")
    return {
        "phone": {
            "value": phone,
            "country": _phone_country(phone) if phone else None,
            "visibility": "visible_to_operator" if phone else "absent_or_hidden",
        },
        "contacts_subscribed": {
            "items": contacts,
            "is_gated": contacts is None and data.get("contacts_reason") is not None,
            "reason": data.get("contacts_reason"),
            "partial": data.get("contacts_subscribed_partial"),
        },
        "restrictions": _restriction_payloads(
            data.get("restriction_reason") or data.get("restrictions"),
            base_path="privacy_or_access.restrictions",
        ),
        "access": {
            "left": data.get("left"),
            "membership": data.get("my_membership") or {},
        },
    }


def _entity_warnings(data: dict) -> list[StructuredWarning]:
    warnings: list[StructuredWarning] = []
    contacts_reason = data.get("contacts_reason")
    if contacts_reason:
        warnings.append(
            structured_warning(
                "contacts_subscribed_gated",
                f"contacts_subscribed unavailable: {contacts_reason}",
                severity="info",
            )
        )
    restrictions = data.get("restriction_reason") or data.get("restrictions") or []
    if restrictions:
        warnings.append(
            structured_warning(
                "entity_restricted",
                "Telegram reports restriction metadata for this entity.",
                severity="warning",
            )
        )
    warnings.extend(_personal_channel_warnings(data))
    return warnings


def _personal_channel_warnings(data: dict) -> list[StructuredWarning]:
    personal_channel_reason = data.get("personal_channel_unavailable_reason")
    if not personal_channel_reason:
        return []
    return [
        structured_warning(
            "personal_channel_enrichment_unavailable",
            f"Personal channel metadata unavailable: {personal_channel_reason}",
            severity="info",
        )
    ]


def _content_fields(data: dict) -> list[dict[str, object]]:
    maybe_fields = [
        _content_field("common.about", data.get("about"), "about"),
        _content_field("type_specific.note", data.get("note"), "note"),
        _content_field("type_specific.private_forward_name", data.get("private_forward_name"), "private_forward_name"),
    ]
    bot_info = data.get("bot_info") or {}
    maybe_fields.append(
        _content_field("type_specific.bot_info.description", bot_info.get("description"), "bot_description")
    )
    for idx, command in enumerate(bot_info.get("commands") or []):
        maybe_fields.append(
            _content_field(
                f"type_specific.bot_info.commands.{idx}.description",
                command.get("description"),
                "bot_command_description",
            )
        )
    business_intro = data.get("business_intro") or {}
    maybe_fields.extend(
        [
            _content_field("type_specific.business.intro.title", business_intro.get("title"), "business_intro"),
            _content_field(
                "type_specific.business.intro.description",
                business_intro.get("description"),
                "business_intro",
            ),
        ]
    )
    business_location = data.get("business_location") or {}
    maybe_fields.append(
        _content_field("type_specific.business.location.address", business_location.get("address"), "business_location")
    )
    maybe_fields.extend(_personal_channel_content_fields(data))
    for idx, restriction in enumerate(data.get("restriction_reason") or data.get("restrictions") or []):
        maybe_fields.append(
            _content_field(
                f"type_specific.restrictions.{idx}.text",
                restriction.get("text"),
                "restriction_reason",
            )
        )
    return [field for field in maybe_fields if field is not None]


def _personal_channel_content_fields(data: dict) -> list[dict[str, object]]:
    personal_channel = data.get("personal_channel")
    if not isinstance(personal_channel, dict):
        return []
    personal_channel_post = personal_channel.get("latest_or_attached_post")
    if not isinstance(personal_channel_post, dict):
        return []
    text_preview = personal_channel_post.get("text_preview")
    if not isinstance(text_preview, str):
        return []
    field = _content_field(
        "type_specific.personal_channel.latest_or_attached_post.text_preview",
        text_preview,
        "message_text",
    )
    return [] if field is None else [field]


def _personal_channel_structured(data: object) -> dict[str, object] | None:
    if not isinstance(data, dict):
        return None
    return {key: value for key, value in data.items() if value is not None}


def _personal_channel_type_specific(data: dict) -> dict[str, object]:
    personal_channel = _personal_channel_structured(data.get("personal_channel"))
    if personal_channel is None:
        return {}
    return {"personal_channel": personal_channel}


def _bot_info_structured(bot_info: dict | None) -> dict[str, object] | None:
    if bot_info is None:
        return None
    commands = []
    for idx, command in enumerate(bot_info.get("commands") or []):
        commands.append(
            {
                "command": command.get("command"),
                "description": command.get("description"),
                "description_content": _content_field(
                    f"type_specific.bot_info.commands.{idx}.description",
                    command.get("description"),
                    "bot_command_description",
                ),
            }
        )
    return {
        "description": bot_info.get("description"),
        "description_content": _content_field(
            "type_specific.bot_info.description",
            bot_info.get("description"),
            "bot_description",
        ),
        "commands": commands,
    }


def _business_structured(data: dict) -> dict[str, object]:
    intro = data.get("business_intro") or {}
    location = data.get("business_location") or {}
    return {
        "intro": (
            {
                "title": intro.get("title"),
                "title_content": _content_field(
                    "type_specific.business.intro.title",
                    intro.get("title"),
                    "business_intro",
                ),
                "description": intro.get("description"),
                "description_content": _content_field(
                    "type_specific.business.intro.description",
                    intro.get("description"),
                    "business_intro",
                ),
            }
            if data.get("business_intro") is not None
            else None
        ),
        "location": (
            {
                "address": location.get("address"),
                "address_content": _content_field(
                    "type_specific.business.location.address",
                    location.get("address"),
                    "business_location",
                ),
                "lat": location.get("lat"),
                "long": location.get("long"),
            }
            if data.get("business_location") is not None
            else None
        ),
        "work_hours": data.get("business_work_hours"),
    }


def _user_or_bot_structured(data: dict) -> dict[str, object]:
    phone = data.get("phone")
    payload: dict[str, object] = {
        "kind": data.get("type"),
        "identity": {
            "first_name": data.get("first_name"),
            "last_name": data.get("last_name"),
            "username": data.get("username"),
            "extra_usernames": data.get("extra_usernames") or [],
            "display_name": data.get("name"),
            "lang_code": data.get("lang_code"),
            "status": data.get("status"),
            "emoji_status_id": data.get("emoji_status_id"),
            "birthday": data.get("birthday"),
            "personal_channel_id": data.get("personal_channel_id"),
        },
        "flags": {
            "verified": data.get("verified"),
            "premium": data.get("premium"),
            "bot": data.get("bot"),
            "scam": data.get("scam"),
            "fake": data.get("fake"),
            "restricted": data.get("restricted"),
        },
        "phone": {
            "value": phone,
            "country": _phone_country(phone) if phone else None,
            "visibility": "visible_to_operator" if phone else "absent_or_hidden",
        },
        "relationship": {
            "contact": data.get("contact"),
            "mutual_contact": data.get("mutual_contact"),
            "close_friend": data.get("close_friend"),
            "blocked": data.get("blocked"),
        },
        "bot_info": _bot_info_structured(data.get("bot_info")),
        "business": _business_structured(data),
        "common_chats": data.get("common_chats") or [],
        "restrictions": _restriction_payloads(data.get("restriction_reason"), base_path="type_specific.restrictions"),
        "folder": {
            "folder_id": data.get("folder_id"),
            "folder_name": data.get("folder_name"),
        },
        "message_options": {
            "send_paid_messages_stars": data.get("send_paid_messages_stars"),
            "ttl_period": data.get("ttl_period"),
            "private_forward_name": data.get("private_forward_name"),
            "private_forward_name_content": _content_field(
                "type_specific.message_options.private_forward_name",
                data.get("private_forward_name"),
                "private_forward_name",
            ),
        },
        "note": data.get("note"),
        "note_content": _content_field("type_specific.note", data.get("note"), "note"),
    }
    payload.update(_personal_channel_type_specific(data))
    return payload


def _contacts_subscribed_structured(data: dict) -> dict[str, object]:
    contacts = data.get("contacts_subscribed")
    return {
        "items": contacts,
        "available": contacts is not None,
        "partial": data.get("contacts_subscribed_partial"),
        "reason": data.get("contacts_reason"),
    }


def _channel_structured(data: dict) -> dict[str, object]:
    return {
        "kind": "channel",
        "classification": {
            "broadcast": True,
            "megagroup": False,
        },
        "title": data.get("name"),
        "username": data.get("username"),
        "subscribers_count": data.get("subscribers_count"),
        "linked_chat_id": data.get("linked_chat_id"),
        "pinned_msg_id": data.get("pinned_msg_id"),
        "slow_mode_seconds": data.get("slow_mode_seconds"),
        "available_reactions": data.get("available_reactions"),
        "restrictions": _restriction_payloads(data.get("restrictions"), base_path="type_specific.restrictions"),
        "contacts_subscribed": _contacts_subscribed_structured(data),
        "membership": data.get("my_membership") or {},
    }


def _supergroup_structured(data: dict) -> dict[str, object]:
    return {
        "kind": "supergroup",
        "classification": {
            "broadcast": False,
            "megagroup": True,
            "forum": data.get("has_topics"),
        },
        "title": data.get("name"),
        "username": data.get("username"),
        "members_count": data.get("members_count"),
        "linked_broadcast_id": data.get("linked_broadcast_id"),
        "slow_mode_seconds": data.get("slow_mode_seconds"),
        "has_topics": data.get("has_topics"),
        "restrictions": _restriction_payloads(data.get("restrictions"), base_path="type_specific.restrictions"),
        "contacts_subscribed": _contacts_subscribed_structured(data),
        "membership": data.get("my_membership") or {},
    }


def _group_structured(data: dict) -> dict[str, object]:
    return {
        "kind": "group",
        "classification": {
            "broadcast": False,
            "megagroup": False,
        },
        "title": data.get("name"),
        "members_count": data.get("members_count"),
        "migrated_to": data.get("migrated_to"),
        "invite_link": data.get("invite_link"),
        "restrictions": _restriction_payloads(data.get("restrictions"), base_path="type_specific.restrictions"),
        "contacts_subscribed": _contacts_subscribed_structured(data),
        "membership": data.get("my_membership") or {},
        "omitted_type_specific_fields": [
            "available_reactions",
            "has_topics",
            "linked_broadcast_id",
            "linked_chat_id",
            "pinned_msg_id",
            "slow_mode_seconds",
        ],
    }


def _type_specific_structured(data: dict) -> dict[str, object]:
    entity_type = data.get("type", "unknown")
    dt = DialogType.parse(entity_type)
    if dt in (DialogType.USER, DialogType.BOT):
        return _user_or_bot_structured(data)
    if dt == DialogType.CHANNEL:
        return _channel_structured(data)
    if dt in (DialogType.SUPERGROUP, DialogType.FORUM):
        return _supergroup_structured(data)
    if dt == DialogType.GROUP:
        return _group_structured(data)
    return {"kind": entity_type}


def _entity_structured_content(
    *,
    args: GetEntityInfo,
    data: dict,
    entity_id: int,
    display_name: str,
    resolution: str,
) -> dict[str, object]:
    input_value = _entity_input_label(args)
    return {
        "resolved_query": {
            "input": input_value,
            "resolution": resolution,
            "entity_id": entity_id,
            "display_name": display_name,
        },
        "entity_id": entity_id,
        "display_name": display_name,
        "type": data.get("type", "unknown"),
        "common": _common_structured(data, entity_id=entity_id),
        "avatar_history": data.get("avatar_history") or [],
        "type_specific": _type_specific_structured(data),
        "relationships": _relationships_structured(data),
        "privacy_or_access": _privacy_or_access_structured(data),
        "warnings": _entity_warnings(data),
        "content_fields": _content_fields(data),
        "dialog_placement": _dialog_placement_structured(data.get("dialog_placement")),
    }


def _dialog_placement_structured(value: object) -> dict[str, object]:
    placement = value if isinstance(value, dict) else {}
    raw_folders = placement.get("folders")
    folders = raw_folders if isinstance(raw_folders, list) else []
    return {
        "archived": bool(placement.get("archived", False)),
        "folders": [
            {
                "id": int(folder["id"]),
                "title": telegram_content(str(folder.get("title", "")), "message_text"),
            }
            for folder in folders
            if isinstance(folder, dict) and "id" in folder
        ],
    }


def _entity_input_label(args: GetEntityInfo) -> str:
    return args.entity if args.entity is not None else str(args.exact_entity_id)


@dataclass(frozen=True, slots=True)
class _EntityLookup:
    entity_id: int
    display_name: str
    resolution: str


def _numeric_entity_lookup(entity: str) -> _EntityLookup | None:
    try:
        entity_id = int(entity)
    except ValueError:
        return None
    return _EntityLookup(entity_id=entity_id, display_name=entity, resolution="numeric_id")


async def _resolve_entity_lookup(entity: str) -> ToolResult | _EntityLookup:
    # Two daemon connections: daemon handles one request per connection.
    # Accepted race: entity_id obtained from resolve_entity could theoretically
    # become stale if the entities table is modified between the two calls, but
    # this window is negligible in practice (entities are stable once synced).
    try:
        async with daemon_connection() as conn:
            resolve_response = await conn.resolve_entity(query=entity)
    except DaemonNotRunningError:
        return error_result(_daemon_not_running_text())

    if not resolve_response.get("ok"):
        return error_result(entity_not_found_text(entity, retry_tool="GetEntityInfo"))

    resolve_data = resolve_response.get("data", {})
    resolve_status = resolve_data.get("result", "not_found")

    if resolve_status == "not_found":
        return error_result(entity_not_found_text(entity, retry_tool="GetEntityInfo"))

    if resolve_status == "candidates":
        matches = resolve_data.get("matches", [])
        err = error_result(
            "Multiple entities matched.\n"
            "Action: Retry get_entity_info with one numeric entity_id from structuredContent.candidates.",
        )
        return ToolResult(
            content=err.content,
            is_error=True,
            structured_content={
                "error": "ambiguous_entity",
                "candidates": [_entity_candidate_payload(match) for match in matches if isinstance(match, dict)],
            },
        )

    return _EntityLookup(
        entity_id=resolve_data["entity_id"],
        display_name=resolve_data["display_name"],
        resolution="resolver_match",
    )


def _entity_info_fetch_error(args: GetEntityInfo, response: dict) -> ToolResult | None:
    if response.get("ok"):
        return None

    entity = args.entity if args.entity is not None else str(args.exact_entity_id)
    error_code = response.get("error", "")
    if error_code == "entity_not_found":
        return error_result(entity_not_found_text(entity, retry_tool="GetEntityInfo"))

    error_msg = response.get("message", "Request failed.")
    return error_result(fetch_entity_info_error_text(entity, error_msg))


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------


class GetEntityInfo(ToolArgs):
    """Look up a Telegram entity by name or exact numeric id (user, bot, channel,
    supergroup, or legacy basic group). Returns a type-tagged profile:

      - user / bot:    id, name, usernames, bio, phone (with country),
                       language, online status, relationship
                       (contact/blocked), status flags (verified, premium,
                       bot, scam, fake), emoji status, personal channel
                       card (title/username/url/preview when available),
                       birthday, folder, business info, common chats,
                       profile-photo history.
      - channel:       subscribers_count, linked_chat_id, pinned_msg_id,
                       slow_mode_seconds, available_reactions, restrictions,
                       contacts_subscribed (when admin).
      - supergroup:    members_count, linked_broadcast_id, slow_mode_seconds,
                       has_topics, restrictions, contacts_subscribed.
      - group (legacy): members_count, migrated_to (id of the supergroup
                        it migrated to, if any), invite_link,
                        contacts_subscribed.

    Avatar history (`avatar_history`) returns photo_id + date metadata only —
    no download capability. The `type` field tells you which kind was
    resolved; non-applicable per-type fields are simply absent.
    Resolves the name via fuzzy match — returns candidates if ambiguous.
    Provide either `entity` for fuzzy/name lookup or `exact_entity_id` for a
    direct numeric lookup; the two inputs are mutually exclusive."""

    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {"required": ["entity"], "not": {"required": ["exact_entity_id"]}},
                {"required": ["exact_entity_id"], "not": {"required": ["entity"]}},
            ]
        }
    )

    entity: str | None = Field(
        default=None,
        max_length=500,
        description="Natural name or handle for fuzzy resolution. Mutually exclusive with exact_entity_id.",
    )
    exact_entity_id: int | None = Field(
        default=None,
        description="Exact numeric Telegram entity id for direct lookup. Mutually exclusive with entity.",
    )

    @model_validator(mode="after")
    def _validate_entity_selector(self) -> GetEntityInfo:
        if self.entity is None and self.exact_entity_id is None:
            raise ValueError("Provide either entity or exact_entity_id.")
        if self.entity is not None and self.exact_entity_id is not None:
            raise ValueError("entity and exact_entity_id are mutually exclusive.")
        return self


def _resolve_entity_lookup_sync(args: GetEntityInfo) -> _EntityLookup | None:
    if args.exact_entity_id is not None:
        return _EntityLookup(
            entity_id=args.exact_entity_id, display_name=str(args.exact_entity_id), resolution="exact_entity_id"
        )
    assert args.entity is not None
    lookup = _numeric_entity_lookup(args.entity)
    if lookup is not None:
        return lookup
    return None


async def _get_entity_lookup(args: GetEntityInfo) -> ToolResult | _EntityLookup:
    lookup = _resolve_entity_lookup_sync(args)
    if lookup is not None:
        return lookup
    assert args.entity is not None
    return await _resolve_entity_lookup(args.entity)


@mcp_tool(
    name="get_entity_info",
    title="Entity Info",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    output_schema=GET_ENTITY_INFO_OUTPUT_SCHEMA,
)
async def get_entity_info(args: GetEntityInfo) -> ToolResult:
    resolved = await _get_entity_lookup(args)
    if isinstance(resolved, ToolResult):
        return resolved
    lookup = resolved

    try:
        async with daemon_connection() as conn:
            response = await conn.get_entity_info(entity_id=lookup.entity_id)
    except DaemonNotRunningError:
        return error_result(_daemon_not_running_text())

    if err := _entity_info_fetch_error(args, response):
        return err

    data = response.get("data", {})
    # Numeric-id path only: prefer the daemon-returned title, regardless of
    # whether the lookup came from exact_entity_id or from parsing a numeric string.
    display_name = lookup.display_name
    if lookup.resolution in ("numeric_id", "exact_entity_id"):
        resolved_name = data.get("name")
        if resolved_name:
            display_name = resolved_name
    structured_content = _entity_structured_content(
        args=args,
        data=data,
        entity_id=lookup.entity_id,
        display_name=display_name,
        resolution=lookup.resolution,
    )
    return structured_result(structured_content, result_count=1)
