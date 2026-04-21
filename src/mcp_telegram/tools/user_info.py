
import phonenumbers
from pydantic import Field

from ..errors import (
    ambiguous_user_text,
    fetch_user_info_error_text,
    user_not_found_text,
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


def _phone_country(phone: str) -> str | None:
    """Best-effort ISO 3166-1 alpha-2 country code from E.164 phone number."""
    try:
        parsed = phonenumbers.parse(phone)
        return phonenumbers.region_code_for_number(parsed)
    except phonenumbers.NumberParseException:
        return None


def _format_status(status: dict | None) -> str | None:
    """Convert a status dict from the daemon into a human-readable string."""
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


class GetUserInfo(ToolArgs):
    """
    Look up a Telegram user by name. Returns their full profile: id, name, username(s),
    bio, phone (with country), language, online status, relationship (contact/blocked),
    status flags (verified, premium, bot, scam, fake), emoji status, personal channel,
    birthday, folder, business info, and common chats with this account.
    Resolves the name via fuzzy match — returns candidates if ambiguous.
    """

    user: str = Field(max_length=500)


@mcp_tool("primary", annotations=ToolAnnotations(readOnlyHint=True))
async def get_user_info(args: GetUserInfo) -> ToolResult:
    # Two separate daemon connections: the daemon handles one request per
    # connection, so resolve_entity and get_user_info cannot share one.
    try:
        async with daemon_connection() as conn:
            resolve_response = await conn.resolve_entity(query=args.user)
    except DaemonNotRunningError:
        return ToolResult(content=_text_response(_daemon_not_running_text()))

    if not resolve_response.get("ok"):
        return ToolResult(content=_text_response(
            user_not_found_text(args.user, retry_tool="GetUserInfo")
        ))

    resolve_data = resolve_response.get("data", {})
    resolve_status = resolve_data.get("result", "not_found")

    if resolve_status == "not_found":
        return ToolResult(content=_text_response(
            user_not_found_text(args.user, retry_tool="GetUserInfo")
        ))

    if resolve_status == "candidates":
        matches = resolve_data.get("matches", [])
        match_lines = []
        for match in matches:
            line = f'id={match["entity_id"]} name="{match["display_name"]}" score={match["score"]}'
            if match.get("username"):
                line += f' @{match["username"]}'
            if match.get("entity_type"):
                line += f' [{match["entity_type"]}]'
            if match.get("disambiguation_hint"):
                line += f'  hint="{match["disambiguation_hint"]}"'
            match_lines.append(line)
        return ToolResult(content=_text_response(
            ambiguous_user_text(args.user, match_lines, retry_tool="GetUserInfo"),
        ))

    entity_id: int = resolve_data["entity_id"]
    display_name: str = resolve_data["display_name"]

    try:
        async with daemon_connection() as conn:
            response = await conn.get_user_info(user_id=entity_id)
    except DaemonNotRunningError:
        return ToolResult(content=_text_response(_daemon_not_running_text()))

    if not response.get("ok"):
        error_code = response.get("error", "")
        if error_code == "user_not_found":
            return ToolResult(content=_text_response(
                fetch_user_info_error_text(args.user, "user not found")
            ))
        error_msg = response.get("message", "Request failed.")
        return ToolResult(content=_text_response(f"Error: {error_msg}"))

    data = response.get("data", {})

    name = " ".join(filter(None, [data.get("first_name"), data.get("last_name")]))
    username = data.get("username") or "none"
    extra_usernames: list[str] = data.get("extra_usernames") or []
    phone = data.get("phone")
    lang_code = data.get("lang_code")
    about = data.get("about")
    personal_channel_id = data.get("personal_channel_id")
    emoji_status_id = data.get("emoji_status_id")
    birthday = data.get("birthday")
    common_chats = data.get("common_chats", [])
    status_str = _format_status(data.get("status"))
    contact: bool = data.get("contact", False)
    mutual_contact: bool = data.get("mutual_contact", False)
    close_friend: bool = data.get("close_friend", False)
    blocked: bool = data.get("blocked", False)
    send_paid_stars: int | None = data.get("send_paid_messages_stars")
    ttl_period: int | None = data.get("ttl_period")
    private_forward_name: str | None = data.get("private_forward_name")
    restriction_reason: list[dict] = data.get("restriction_reason") or []
    bot_info: dict | None = data.get("bot_info")
    business_location: dict | None = data.get("business_location")
    business_intro: dict | None = data.get("business_intro")
    business_work_hours: dict | None = data.get("business_work_hours")
    note: str | None = data.get("note")
    folder_id: int | None = data.get("folder_id")
    folder_name: str | None = data.get("folder_name")

    # Flags — only show true ones to keep output compact
    flags = [
        label for label, val in [
            ("verified", data.get("verified")),
            ("premium", data.get("premium")),
            ("bot", data.get("bot")),
            ("scam", data.get("scam")),
            ("fake", data.get("fake")),
            ("restricted", data.get("restricted")),
        ] if val
    ]

    chat_lines = [
        f"  id={chat['id']} type={chat['type']} name='{chat['name']}'"
        for chat in common_chats
    ]
    chats_text = "\n".join(chat_lines) if chat_lines else "  (none)"

    lines: list[str] = [f'[resolved: "{display_name}"]']

    # Identity line
    id_line = f"id={entity_id} name='{name}' username=@{username}"
    if extra_usernames:
        id_line += " also=@" + ", @".join(extra_usernames)
    lines.append(id_line)

    # Flags and status
    if flags:
        lines.append("flags: " + ", ".join(flags))
    if status_str:
        lines.append(f"status: {status_str}")

    # Relationship
    if contact or mutual_contact or close_friend or blocked:
        rel_parts = []
        if contact:
            rel_parts.append("mutual" if mutual_contact else "contact")
        if close_friend:
            rel_parts.append("close friend")
        if blocked:
            rel_parts.append("blocked by you")
        lines.append("relationship: " + ", ".join(rel_parts))

    # Contact info
    if phone:
        country = _phone_country(phone)
        country_suffix = f" ({country})" if country else ""
        lines.append(f"phone: {phone}{country_suffix}")
    if lang_code:
        lines.append(f"lang: {lang_code}")

    # Profile
    if about:
        lines.append(f"bio: {about}")
    if birthday:
        bday_parts = list(filter(None, [
            str(birthday.get("day")) if birthday.get("day") else None,
            str(birthday.get("month")) if birthday.get("month") else None,
            str(birthday.get("year")) if birthday.get("year") else None,
        ]))
        lines.append(f"birthday: {'/'.join(bday_parts)}")
    if personal_channel_id:
        lines.append(f"personal_channel_id={personal_channel_id}")
    if emoji_status_id:
        lines.append(f"emoji_status_id={emoji_status_id}")

    # Folder
    if folder_id is not None:
        folder_display = f"{folder_name} (id={folder_id})" if folder_name else f"id={folder_id}"
        lines.append(f"folder: {folder_display}")

    # Messaging constraints
    if send_paid_stars:
        lines.append(f"paid_messages: {send_paid_stars} stars required")
    if ttl_period:
        days = ttl_period // 86400
        lines.append(f"auto_delete: {days}d" if days else f"auto_delete: {ttl_period}s")
    if private_forward_name:
        lines.append(f"forwards_as: {private_forward_name}")
    if restriction_reason:
        for rr in restriction_reason:
            lines.append(
                f"restriction: [{rr.get('platform')}] {rr.get('reason')} — {rr.get('text')}"
            )

    # Bot info (only present when bot=True)
    if bot_info:
        if bot_info.get("description"):
            lines.append(f"bot_description: {bot_info['description']}")
        cmds = bot_info.get("commands") or []
        if cmds:
            cmd_str = ", ".join(f"/{c['command']}" for c in cmds)
            lines.append(f"bot_commands: {cmd_str}")

    # Business profile
    if business_intro:
        parts = list(filter(None, [
            business_intro.get("title"),
            business_intro.get("description"),
        ]))
        lines.append("business_intro: " + " / ".join(parts))
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
    if business_work_hours:
        tz = business_work_hours.get("timezone")
        lines.append(f"business_hours: configured (timezone={tz})")

    # Personal note
    if note:
        lines.append(f"note: {note}")

    lines.append(f"Common chats ({len(common_chats)}):\n{chats_text}")

    return ToolResult(content=_text_response("\n".join(lines)), result_count=1)
