from __future__ import annotations

import logging
import sys
import typing as t
from functools import cache as functools_cache
from functools import singledispatch

from mcp.types import (
    EmbeddedResource,
    ImageContent,
    TextContent,
    Tool,
)
from pydantic import BaseModel, ConfigDict
from telethon import TelegramClient, custom, functions, types  # type: ignore[import-untyped]
from telethon.tl.functions.messages import GetCommonChatsRequest, GetPeerDialogsRequest
from xdg_base_dirs import xdg_state_home

from .cache import EntityCache
from .formatter import format_messages
from .pagination import decode_cursor, encode_cursor
from .resolver import Candidates, NotFound, resolve
from .telegram import create_client

logger = logging.getLogger(__name__)


# How to add a new tool:
#
# 1. Create a new class that inherits from ToolArgs
#    ```python
#    class NewTool(ToolArgs):
#        """Description of the new tool."""
#        pass
#    ```
#    Attributes of the class will be used as arguments for the tool.
#    The class docstring will be used as the tool description.
#
# 2. Implement the tool_runner function for the new class
#    ```python
#    @tool_runner.register
#    async def new_tool(args: NewTool) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
#        pass
#    ```
#    The function should return a sequence of TextContent, ImageContent or EmbeddedResource.
#    The function should be async and accept a single argument of the new class.
#
# 3. Done! Restart the client and the new tool should be available.


class ToolArgs(BaseModel):
    model_config = ConfigDict()


@singledispatch
async def tool_runner(
    args,  # noqa: ANN001
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    raise NotImplementedError(f"Unsupported type: {type(args)}")


def tool_description(args: type[ToolArgs]) -> Tool:
    return Tool(
        name=args.__name__,
        description=args.__doc__,
        inputSchema=args.model_json_schema(),
    )


def tool_args(tool: Tool, *args, **kwargs) -> ToolArgs:  # noqa: ANN002, ANN003
    return sys.modules[__name__].__dict__[tool.name](*args, **kwargs)


@functools_cache
def get_entity_cache() -> EntityCache:
    """Return the shared EntityCache instance (opened once per process)."""
    db_dir = xdg_state_home() / "mcp-telegram"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "entity_cache.db"
    return EntityCache(db_path)


### ListDialogs ###


class ListDialogs(ToolArgs):
    """List available dialogs, chats and channels with type and last message timestamp."""

    archived: bool = False
    ignore_pinned: bool = False


@tool_runner.register
async def list_dialogs(
    args: ListDialogs,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[ListDialogs] args[%s]", args)
    cache = get_entity_cache()
    response: list[TextContent] = []
    async with create_client() as client:
        async for dialog in client.iter_dialogs(
            archived=args.archived, ignore_pinned=args.ignore_pinned
        ):
            if dialog.is_user:
                dtype = "user"
            elif dialog.is_group:
                dtype = "group"
            elif dialog.is_channel:
                dtype = "channel"
            else:
                dtype = "unknown"
            last_at = dialog.date.isoformat() if dialog.date else "unknown"
            # Lazy cache warm-up: upsert entity metadata on every ListDialogs call
            entity = dialog.entity
            username: str | None = getattr(entity, "username", None)
            cache.upsert(dialog.id, dtype, dialog.name, username)
            msg = (
                f"name='{dialog.name}' id={dialog.id} type={dtype} "
                f"last_message_at={last_at} unread={dialog.unread_count}"
            )
            response.append(TextContent(type="text", text=msg))
    return response


### ListMessages ###


class ListMessages(ToolArgs):
    """
    List messages in a dialog by name. Returns messages newest-first in human-readable format
    (HH:mm FirstName: text) with date headers and session breaks.

    Use cursor= with the next_cursor token from a previous response to page back in time.
    Use sender= to filter messages from a specific person (name string, resolved via fuzzy match).
    Use unread=True to show only messages you haven't read yet.
    """

    dialog: str
    limit: int = 100
    cursor: str | None = None
    sender: str | None = None
    unread: bool = False


@tool_runner.register
async def list_messages(
    args: ListMessages,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[ListMessages] args[%s]", args)

    # Step 1 — Resolve dialog name
    cache = get_entity_cache()
    result = resolve(args.dialog, cache.all_names())
    if isinstance(result, NotFound):
        return [TextContent(type="text", text=f'Dialog not found: "{args.dialog}"')]
    if isinstance(result, Candidates):
        names = ", ".join(f'"{m[0]}"' for m in result.matches[:5])
        return [TextContent(type="text", text=f'Ambiguous dialog "{args.dialog}". Matches: {names}')]
    entity_id: int = result.entity_id

    # Step 2 — Build iter_messages kwargs
    iter_kwargs: dict[str, t.Any] = {
        "entity": entity_id,
        "limit": args.limit,
        "reverse": False,
    }
    if args.cursor:
        iter_kwargs["max_id"] = decode_cursor(args.cursor, entity_id)

    # Step 3 — Sender filter (resolve before opening client)
    if args.sender:
        sender_result = resolve(args.sender, cache.all_names())
        if isinstance(sender_result, NotFound):
            return [TextContent(type="text", text=f'Sender not found: "{args.sender}"')]
        if isinstance(sender_result, Candidates):
            names = ", ".join(f'"{m[0]}"' for m in sender_result.matches[:5])
            return [TextContent(type="text", text=f'Ambiguous sender "{args.sender}". Matches: {names}')]
        iter_kwargs["from_user"] = sender_result.entity_id

    # Step 4 — Unread filter + message fetch + format + cursor
    async with create_client() as client:
        if args.unread:
            input_peer = await client.get_input_entity(entity_id)
            peer_result = await client(GetPeerDialogsRequest(peers=[input_peer]))
            tl_dialog = peer_result.dialogs[0]
            iter_kwargs["min_id"] = tl_dialog.read_inbox_max_id
            iter_kwargs["limit"] = min(tl_dialog.unread_count, args.limit)

        messages = [msg async for msg in client.iter_messages(**iter_kwargs)]

        # Lazy cache population: upsert sender entities
        for msg in messages:
            sender = getattr(msg, "sender", None)
            if sender is not None:
                sender_name = " ".join(
                    filter(None, [
                        getattr(sender, "first_name", None),
                        getattr(sender, "last_name", None),
                    ])
                ) or getattr(sender, "title", "") or str(msg.sender_id)
                sender_type = "user" if getattr(sender, "first_name", None) else "group"
                cache.upsert(
                    msg.sender_id, sender_type, sender_name,
                    getattr(sender, "username", None)
                )

    text = format_messages(messages, reply_map={})
    next_cursor: str | None = None
    if len(messages) == args.limit and messages:
        next_cursor = encode_cursor(messages[-1].id, entity_id)

    result_text = text
    if next_cursor:
        result_text += f"\n\nnext_cursor: {next_cursor}"
    return [TextContent(type="text", text=result_text)]


### SearchMessages ###


class SearchMessages(ToolArgs):
    """
    Search messages in a dialog by text query. Returns each matching message with up to
    3 messages of surrounding context (before and after). Ordered newest to oldest.

    Use offset= with the next_offset value from a previous response to get the next page.
    """

    dialog: str
    query: str
    limit: int = 20
    offset: int | None = None


@tool_runner.register
async def search_messages(
    args: SearchMessages,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[SearchMessages] args[%s]", args)

    # Step 1: Resolve dialog name
    cache = get_entity_cache()
    result = resolve(args.dialog, cache.all_names())
    if isinstance(result, NotFound):
        return [TextContent(type="text", text=f'Dialog not found: "{args.dialog}"')]
    if isinstance(result, Candidates):
        names = ", ".join(f'"{m[0]}"' for m in result.matches[:5])
        return [TextContent(type="text", text=f'Ambiguous dialog "{args.dialog}". Matches: {names}')]
    entity_id: int = result.entity_id

    page_offset = args.offset or 0

    async with create_client() as client:
        # Step 2: Fetch search results page
        hits = [
            msg async for msg in client.iter_messages(
                entity_id,
                search=args.query,
                limit=args.limit,
                add_offset=page_offset,
            )
        ]

        # Step 3: For each hit, fetch ±3 context messages
        blocks: list[str] = []
        for hit in hits:
            before = list(reversed([
                m async for m in client.iter_messages(entity_id, limit=3, max_id=hit.id)
            ]))
            after = [
                m async for m in client.iter_messages(entity_id, limit=3, min_id=hit.id)
            ]
            window = before + [hit] + after
            blocks.append(format_messages(window, reply_map={}))

    # Step 4: Build output
    result_text = "\n\n---\n\n".join(blocks)
    if len(hits) == args.limit:
        result_text += f"\n\nnext_offset: {page_offset + args.limit}"
    return [TextContent(type="text", text=result_text)]


### GetMe ###


class GetMe(ToolArgs):
    """Return own account info: numeric id, display name, and username. No arguments required."""

    pass


@tool_runner.register
async def get_me(args: GetMe) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[GetMe] args[%s]", args)
    async with create_client() as client:
        me = await client.get_me()
    if me is None:
        return [TextContent(type="text", text="Not authenticated")]
    name = " ".join(filter(None, [
        getattr(me, "first_name", None),
        getattr(me, "last_name", None),
    ]))
    username = getattr(me, "username", None) or "none"
    text = f"id={me.id} name='{name}' username=@{username}"
    return [TextContent(type="text", text=text)]


### GetUserInfo ###


class GetUserInfo(ToolArgs):
    """
    Look up a Telegram user by name. Returns their profile (id, name, username) and
    the list of chats shared with this account. Resolves the name via fuzzy match.
    """

    user: str


@tool_runner.register
async def get_user_info(args: GetUserInfo) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[GetUserInfo] args[%s]", args)
    cache = get_entity_cache()
    result = resolve(args.user, cache.all_names())
    if isinstance(result, NotFound):
        return [TextContent(type="text", text=f'User not found: "{args.user}"')]
    if isinstance(result, Candidates):
        names = ", ".join(f'"{m[0]}"' for m in result.matches[:5])
        return [TextContent(type="text", text=f'Ambiguous user "{args.user}". Matches: {names}')]
    entity_id: int = result.entity_id
    display_name: str = result.display_name

    async with create_client() as client:
        try:
            user = await client.get_entity(entity_id)
            common_result = await client(GetCommonChatsRequest(
                user_id=entity_id,
                max_id=0,
                limit=100,
            ))
        except Exception as exc:
            return [TextContent(type="text", text=f"Error fetching user info: {exc}")]

    name = " ".join(filter(None, [
        getattr(user, "first_name", None),
        getattr(user, "last_name", None),
    ]))
    username = getattr(user, "username", None) or "none"
    chat_lines = []
    for chat in common_result.chats:
        chat_name = getattr(chat, "title", None) or getattr(chat, "first_name", str(chat.id))
        chat_lines.append(f"  id={chat.id} name='{chat_name}'")
    chats_text = "\n".join(chat_lines) if chat_lines else "  (none)"
    text = (
        f'[resolved: "{display_name}"]\n'
        f"id={entity_id} name='{name}' username=@{username}\n"
        f"Common chats ({len(common_result.chats)}):\n{chats_text}"
    )
    return [TextContent(type="text", text=text)]
