from __future__ import annotations

import logging

from pydantic import Field
from telethon.tl.functions.messages import GetCommonChatsRequest  # type: ignore[import-untyped]
from telethon.tl.types import Channel, Chat  # type: ignore[import-untyped]
from telethon.utils import get_peer_id  # type: ignore[import-untyped]

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
from ._base import ToolArgs, ToolResult, _text_response, connected_client, get_entity_cache, mcp_tool

logger = logging.getLogger(__name__)


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

    async with connected_client() as client:
        try:
            user = await client.get_entity(entity_id)
            common_result = await client(GetCommonChatsRequest(
                user_id=entity_id,
                max_id=0,
                limit=100,
            ))
        except Exception as exc:
            logger.warning("get_user_info entity_id=%r failed: %s", entity_id, exc, exc_info=True)
            return ToolResult(content=_text_response(fetch_user_info_error_text(args.user, type(exc).__name__)))

    name = " ".join(filter(None, [
        getattr(user, "first_name", None),
        getattr(user, "last_name", None),
    ]))
    username = getattr(user, "username", None) or "none"
    chat_lines = []
    for chat in common_result.chats:
        chat_name = getattr(chat, "title", None) or getattr(chat, "first_name", str(chat.id))
        full_id = get_peer_id(chat)
        if isinstance(chat, Channel):
            chat_type = "supergroup" if getattr(chat, "megagroup", False) else "channel"
        elif isinstance(chat, Chat):
            chat_type = "group"
        else:
            chat_type = "user"
        chat_lines.append(f"  id={full_id} type={chat_type} name='{chat_name}'")
    chats_text = "\n".join(chat_lines) if chat_lines else "  (none)"
    text = (
        f'[resolved: "{display_name}"]\n'
        f"id={entity_id} name='{name}' username=@{username}\n"
        f"Common chats ({len(common_result.chats)}):\n{chats_text}"
    )
    return ToolResult(content=_text_response(text), result_count=1)
