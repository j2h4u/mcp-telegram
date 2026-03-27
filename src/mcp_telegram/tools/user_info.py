from __future__ import annotations

from pydantic import Field

from ..cache import GROUP_TTL, USER_TTL
from ..errors import (
    ambiguous_user_text,
    fetch_user_info_error_text,
    user_not_found_text,
)
from ..resolver import (
    Candidates,
    NotFound,
    resolve,
)
from ._base import (
    DaemonNotRunningError,
    ToolArgs,
    ToolResult,
    _text_response,
    daemon_connection,
    get_entity_cache,
    mcp_tool,
)


def _daemon_not_running_text() -> str:
    return (
        "Sync daemon is not running.\n"
        "Action: Start it with: mcp-telegram sync"
    )


class GetUserInfo(ToolArgs):
    """
    Look up a Telegram user by name. Returns their profile (id, name, username) and
    the list of chats shared with this account. Resolves the name via fuzzy match.
    """

    user: str = Field(max_length=500)


@mcp_tool("primary")
async def get_user_info(args: GetUserInfo) -> ToolResult:
    cache = get_entity_cache()
    choices = cache.all_names_with_ttl(USER_TTL, GROUP_TTL)
    normalized = cache.all_names_normalized_with_ttl(USER_TTL, GROUP_TTL)
    resolve_result = resolve(args.user, choices, cache, normalized_choices=normalized)
    if isinstance(resolve_result, NotFound):
        return ToolResult(content=_text_response(user_not_found_text(args.user, retry_tool="GetUserInfo")))
    if isinstance(resolve_result, Candidates):
        match_lines = []
        for match in resolve_result.matches:
            line = f'id={match["entity_id"]} name="{match["display_name"]}" score={match["score"]}'
            if match.get("username"):
                line += f' @{match["username"]}'
            if match.get("entity_type"):
                line += f' [{match["entity_type"]}]'
            match_lines.append(line)
        return ToolResult(content=_text_response(
            ambiguous_user_text(args.user, match_lines, retry_tool="GetUserInfo"),
        ))
    entity_id: int = resolve_result.entity_id
    display_name: str = resolve_result.display_name

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
