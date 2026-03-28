from __future__ import annotations

from pydantic import Field

from ..errors import (
    ambiguous_user_text,
    fetch_user_info_error_text,
    user_not_found_text,
)
from ._base import (
    DaemonNotRunningError,
    ToolArgs,
    ToolResult,
    _daemon_not_running_text,
    _text_response,
    daemon_connection,
    mcp_tool,
)


class GetUserInfo(ToolArgs):
    """
    Look up a Telegram user by name. Returns their profile (id, name, username) and
    the list of chats shared with this account. Resolves the name via fuzzy match.
    """

    user: str = Field(max_length=500)


@mcp_tool("primary")
async def get_user_info(args: GetUserInfo) -> ToolResult:
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
            match_lines.append(line)
        return ToolResult(content=_text_response(
            ambiguous_user_text(args.user, match_lines, retry_tool="GetUserInfo"),
        ))

    # resolve_status == "resolved"
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
                fetch_user_info_error_text(args.user, "user not found by daemon")
            ))
        error_msg = response.get("message", "Daemon returned an error.")
        return ToolResult(content=_text_response(f"Error: {error_msg}"))

    data = response.get("data", {})
    name = " ".join(filter(None, [
        data.get("first_name"),
        data.get("last_name"),
    ]))
    username = data.get("username") or "none"
    common_chats = data.get("common_chats", [])
    chat_lines = []
    for chat in common_chats:
        chat_lines.append(f"  id={chat['id']} type={chat['type']} name='{chat['name']}'")
    chats_text = "\n".join(chat_lines) if chat_lines else "  (none)"
    text = (
        f'[resolved: "{display_name}"]\n'
        f"id={entity_id} name='{name}' username=@{username}\n"
        f"Common chats ({len(common_chats)}):\n{chats_text}"
    )
    return ToolResult(content=_text_response(text), result_count=1)
