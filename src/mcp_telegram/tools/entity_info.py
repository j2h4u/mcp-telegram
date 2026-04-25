"""MCP tool: GetEntityInfo — universal entity inspector (Phase 47).

Universal replacement covering User / Bot / BroadcastChannel / Supergroup /
LegacyChat. DB-first cache with 5-minute TTL on the daemon side; tool itself
is the formatter.
"""

from datetime import UTC, datetime

import phonenumbers
from pydantic import Field

from ..errors import (
    ambiguous_entity_text,
    entity_not_found_text,
    fetch_entity_info_error_text,
)
from ._base import (
    DaemonNotRunningError,
    ToolAnnotations,
    ToolArgs,
    ToolResult,
    _daemon_not_running_text,
    _text_response,
    daemon_connection,
    mcp_tool,
)


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


def _format_status(status: dict | None) -> str | None:
    if not status:
        return None
    kind = status.get("type")
    if kind == "online":
        return "online"
    if kind == "offline":
        was_online = status.get("was_online")
        return f"last seen {was_online}" if was_online else "offline"
    if kind == "recently":
        return "last seen recently"
    if kind == "last_week":
        return "last seen last week"
    if kind == "last_month":
        return "last seen last month"
    return None


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------


class GetEntityInfo(ToolArgs):
    """
    Look up a Telegram entity by name (user, bot, channel, supergroup, or
    legacy basic group). Returns a type-tagged profile:

      - user / bot:    id, name, usernames, bio, phone (with country),
                       language, online status, relationship
                       (contact/blocked), status flags (verified, premium,
                       bot, scam, fake), emoji status, personal channel,
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
    """

    entity: str = Field(max_length=500)


@mcp_tool("primary", annotations=ToolAnnotations(readOnlyHint=True))
async def get_entity_info(args: GetEntityInfo) -> ToolResult:
    # Two daemon connections: daemon handles one request per connection.
    # Accepted race: entity_id obtained from resolve_entity could theoretically
    # become stale if the entities table is modified between the two calls, but
    # this window is negligible in practice (entities are stable once synced).
    try:
        async with daemon_connection() as conn:
            resolve_response = await conn.resolve_entity(query=args.entity)
    except DaemonNotRunningError:
        return ToolResult(content=_text_response(_daemon_not_running_text()))

    if not resolve_response.get("ok"):
        return ToolResult(content=_text_response(entity_not_found_text(args.entity, retry_tool="GetEntityInfo")))

    resolve_data = resolve_response.get("data", {})
    resolve_status = resolve_data.get("result", "not_found")

    if resolve_status == "not_found":
        return ToolResult(content=_text_response(entity_not_found_text(args.entity, retry_tool="GetEntityInfo")))

    if resolve_status == "candidates":
        matches = resolve_data.get("matches", [])
        match_lines = []
        for match in matches:
            line = f'id={match["entity_id"]} name="{match["display_name"]}" score={match["score"]}'
            if match.get("username"):
                line += f" @{match['username']}"
            if match.get("entity_type"):
                line += f" [{match['entity_type']}]"
            if match.get("disambiguation_hint"):
                line += f'  hint="{match["disambiguation_hint"]}"'
            match_lines.append(line)
        return ToolResult(
            content=_text_response(
                ambiguous_entity_text(args.entity, match_lines, retry_tool="GetEntityInfo"),
            )
        )

    entity_id: int = resolve_data["entity_id"]
    display_name: str = resolve_data["display_name"]

    try:
        async with daemon_connection() as conn:
            response = await conn.get_entity_info(entity_id=entity_id)
    except DaemonNotRunningError:
        return ToolResult(content=_text_response(_daemon_not_running_text()))

    if not response.get("ok"):
        error_code = response.get("error", "")
        if error_code == "entity_not_found":
            return ToolResult(content=_text_response(entity_not_found_text(args.entity, retry_tool="GetEntityInfo")))
        error_msg = response.get("message", "Request failed.")
        return ToolResult(content=_text_response(fetch_entity_info_error_text(args.entity, error_msg)))

    data = response.get("data", {})
    entity_type = data.get("type", "unknown")

    # ---------- Common envelope (rendered for ALL types) ----------
    lines: list[str] = [f'[resolved: "{display_name}"]']
    name = data.get("name") or "?"
    username = data.get("username") or "none"
    id_line = f"id={entity_id} type={entity_type} name='{name}' username=@{username}"
    lines.append(id_line)

    about = data.get("about")
    if about:
        lines.append(f"about: {about}")

    my_membership = data.get("my_membership") or {}
    if my_membership:
        mem_parts = []
        if my_membership.get("is_member"):
            mem_parts.append("member")
        if my_membership.get("is_admin"):
            mem_parts.append("admin")
        if mem_parts:
            lines.append("my_membership: " + ", ".join(mem_parts))

    avatar_history = data.get("avatar_history") or []
    avatar_count = data.get("avatar_count") or len(avatar_history)
    if avatar_history:
        now = datetime.now(tz=UTC)
        avatar_lines = [f"avatars ({avatar_count} total, showing {len(avatar_history)}):"]
        for idx, photo in enumerate(avatar_history, start=1):
            iso = photo.get("date", "") or ""
            relative = _format_relative_ymd(iso, now=now) if iso else "?"
            absolute = iso[:10] if iso else "?"
            avatar_lines.append(f"  {idx}. {relative} ({absolute}) id={photo.get('photo_id')}")
        lines.append("\n".join(avatar_lines))

    # ---------- Per-type rendering ----------
    if entity_type in ("user", "bot"):
        _render_user_or_bot(data, lines)
    elif entity_type == "channel":
        _render_channel(data, lines)
    elif entity_type == "supergroup":
        _render_supergroup(data, lines)
    elif entity_type == "group":
        _render_group(data, lines)

    return ToolResult(content=_text_response("\n".join(lines)), result_count=1)


# ---------------------------------------------------------------------------
# Per-type rendering — User/Bot block preserved verbatim from old user_info.py
# ---------------------------------------------------------------------------


def _render_user_or_bot(data: dict, lines: list[str]) -> None:
    flags = [
        label
        for label, val in [
            ("verified", data.get("verified")),
            ("premium", data.get("premium")),
            ("bot", data.get("bot")),
            ("scam", data.get("scam")),
            ("fake", data.get("fake")),
            ("restricted", data.get("restricted")),
        ]
        if val
    ]
    if flags:
        lines.append("flags: " + ", ".join(flags))

    status_str = _format_status(data.get("status"))
    if status_str:
        lines.append(f"status: {status_str}")

    contact: bool = data.get("contact", False)
    mutual_contact: bool = data.get("mutual_contact", False)
    close_friend: bool = data.get("close_friend", False)
    blocked: bool = data.get("blocked", False)
    if contact or mutual_contact or close_friend or blocked:
        rel_parts = []
        if contact:
            rel_parts.append("mutual" if mutual_contact else "contact")
        if close_friend:
            rel_parts.append("close friend")
        if blocked:
            rel_parts.append("blocked by you")
        lines.append("relationship: " + ", ".join(rel_parts))

    phone = data.get("phone")
    if phone:
        country = _phone_country(phone)
        country_suffix = f" ({country})" if country else ""
        lines.append(f"phone: {phone}{country_suffix}")
    lang_code = data.get("lang_code")
    if lang_code:
        lines.append(f"lang: {lang_code}")

    birthday = data.get("birthday")
    if birthday:
        bday_parts = list(filter(None, [
            str(birthday.get("day")) if birthday.get("day") else None,
            str(birthday.get("month")) if birthday.get("month") else None,
            str(birthday.get("year")) if birthday.get("year") else None,
        ]))
        lines.append(f"birthday: {'/'.join(bday_parts)}")
    personal_channel_id = data.get("personal_channel_id")
    if personal_channel_id:
        lines.append(f"personal_channel_id={personal_channel_id}")
    emoji_status_id = data.get("emoji_status_id")
    if emoji_status_id:
        lines.append(f"emoji_status_id={emoji_status_id}")

    folder_id: int | None = data.get("folder_id")
    folder_name: str | None = data.get("folder_name")
    if folder_id is not None:
        folder_display = f"{folder_name} (id={folder_id})" if folder_name else f"id={folder_id}"
        lines.append(f"folder: {folder_display}")

    send_paid_stars = data.get("send_paid_messages_stars")
    if send_paid_stars:
        lines.append(f"paid_messages: {send_paid_stars} stars required")
    ttl_period = data.get("ttl_period")
    if ttl_period:
        days = ttl_period // 86400
        lines.append(f"auto_delete: {days}d" if days else f"auto_delete: {ttl_period}s")
    private_forward_name = data.get("private_forward_name")
    if private_forward_name:
        lines.append(f"forwards_as: {private_forward_name}")
    for rr in data.get("restriction_reason") or []:
        lines.append(f"restriction: [{rr.get('platform')}] {rr.get('reason')} — {rr.get('text')}")

    bot_info = data.get("bot_info")
    if bot_info:
        if bot_info.get("description"):
            lines.append(f"bot_description: {bot_info['description']}")
        cmds = bot_info.get("commands") or []
        if cmds:
            cmd_str = ", ".join(f"/{c['command']}" for c in cmds)
            lines.append(f"bot_commands: {cmd_str}")

    business_intro = data.get("business_intro")
    if business_intro:
        parts = list(filter(None, [
            business_intro.get("title"),
            business_intro.get("description"),
        ]))
        lines.append("business_intro: " + " / ".join(parts))
    business_location = data.get("business_location")
    if business_location:
        addr = business_location.get("address")
        lat = business_location.get("lat")
        lon = business_location.get("long")
        loc_parts = []
        if addr:
            loc_parts.append(addr)
        if lat is not None and lon is not None:
            loc_parts.append(f"({lat}, {lon})")
        lines.append("business_location: " + ", ".join(loc_parts))
    business_work_hours = data.get("business_work_hours")
    if business_work_hours:
        tz = business_work_hours.get("timezone")
        lines.append(f"business_hours: configured (timezone={tz})")

    note = data.get("note")
    if note:
        lines.append(f"note: {note}")

    common_chats = data.get("common_chats") or []
    chat_lines = [f"  id={chat['id']} type={chat['type']} name='{chat['name']}'" for chat in common_chats]
    chats_text = "\n".join(chat_lines) if chat_lines else "  (none)"
    lines.append(f"Common chats ({len(common_chats)}):\n{chats_text}")


def _render_channel(data: dict, lines: list[str]) -> None:
    subs = data.get("subscribers_count")
    if subs is not None:
        lines.append(f"subscribers_count: {subs}")
    linked = data.get("linked_chat_id")
    if linked is not None:
        lines.append(f"linked_chat_id: {linked}")
    pinned = data.get("pinned_msg_id")
    if pinned is not None:
        lines.append(f"pinned_msg_id: {pinned}")
    slow = data.get("slow_mode_seconds")
    if slow is not None:
        lines.append(f"slow_mode_seconds: {slow}")
    ar = data.get("available_reactions") or {}
    if ar:
        kind = ar.get("kind", "none")
        if kind == "all":
            lines.append("available_reactions: all")
        elif kind == "some":
            lines.append("available_reactions: " + ", ".join(ar.get("emojis", [])))
        else:
            lines.append("available_reactions: none")
    for rr in data.get("restrictions") or []:
        lines.append(f"restriction: [{rr.get('platform')}] {rr.get('reason')} — {rr.get('text')}")
    _render_contacts_subscribed(data, lines)


def _render_supergroup(data: dict, lines: list[str]) -> None:
    mc = data.get("members_count")
    if mc is not None:
        lines.append(f"members_count: {mc}")
    linked = data.get("linked_broadcast_id")
    if linked is not None:
        lines.append(f"linked_broadcast_id: {linked}")
    slow = data.get("slow_mode_seconds")
    if slow is not None:
        lines.append(f"slow_mode_seconds: {slow}")
    if data.get("has_topics"):
        lines.append("has_topics: yes")
    for rr in data.get("restrictions") or []:
        lines.append(f"restriction: [{rr.get('platform')}] {rr.get('reason')} — {rr.get('text')}")
    _render_contacts_subscribed(data, lines)


def _render_group(data: dict, lines: list[str]) -> None:
    mc = data.get("members_count")
    if mc is not None:
        lines.append(f"members_count: {mc}")
    migrated = data.get("migrated_to")
    if migrated is not None:
        lines.append(f"migrated_to: {migrated}  (re-run GetEntityInfo with this id to inspect the migrated supergroup)")
    invite = data.get("invite_link")
    if invite:
        lines.append(f"invite_link: {invite}")
    for rr in data.get("restrictions") or []:
        lines.append(f"restriction: [{rr.get('platform')}] {rr.get('reason')} — {rr.get('text')}")
    _render_contacts_subscribed(data, lines)


def _render_contacts_subscribed(data: dict, lines: list[str]) -> None:
    contacts = data.get("contacts_subscribed")
    partial = data.get("contacts_subscribed_partial", False)
    reason = data.get("contacts_reason")
    if contacts is None:
        tag = f" (reason: {reason})" if reason else ""
        lines.append(f"contacts_subscribed: null{tag}")
        return
    if not contacts:
        tag = f" (reason: {reason})" if reason else ""
        lines.append(f"contacts_subscribed: (none in your DM peers){tag}")
        return
    partial_tag = " — partial (contact-filter only)" if partial else ""
    contact_lines = [f"contacts_subscribed ({len(contacts)}{partial_tag}):"]
    for c in contacts:
        entry = f"  id={c['id']}"
        if c.get("name"):
            entry += f" name='{c['name']}'"
        if c.get("username"):
            entry += f" @{c['username']}"
        contact_lines.append(entry)
    lines.append("\n".join(contact_lines))
