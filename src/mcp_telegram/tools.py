from __future__ import annotations

import logging
import sys
import typing as t
from functools import singledispatch

from mcp.types import (
    EmbeddedResource,
    ImageContent,
    TextContent,
    Tool,
)
from pydantic import BaseModel, ConfigDict
from telethon import TelegramClient, custom, functions, types  # type: ignore[import-untyped]

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


### ListDialogs ###


class ListDialogs(ToolArgs):
    """List available dialogs, chats and channels."""

    unread: bool = False
    archived: bool = False
    ignore_pinned: bool = False


@tool_runner.register
async def list_dialogs(
    args: ListDialogs,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    client: TelegramClient
    logger.info("method[ListDialogs] args[%s]", args)

    response: list[TextContent] = []
    async with create_client() as client:
        dialog: custom.dialog.Dialog
        async for dialog in client.iter_dialogs(archived=args.archived, ignore_pinned=args.ignore_pinned):
            if args.unread and dialog.unread_count == 0:
                continue
            msg = (
                f"name='{dialog.name}' id={dialog.id} "
                f"unread={dialog.unread_count} mentions={dialog.unread_mentions_count}"
            )
            response.append(TextContent(type="text", text=msg))

    return response


### ListMessages ###


class ListMessages(ToolArgs):
    """
    List messages in a given dialog, chat or channel. The messages are listed in order from newest to oldest.

    If `unread` is set to `True`, only unread messages will be listed. Once a message is read, it will not be
    listed again.

    If `limit` is set, only the last `limit` messages will be listed. If `unread` is set, the limit will be
    the minimum between the unread messages and the limit.

    If `before_id` is set, only messages older than the given message ID will be listed. Use this for
    pagination: pass the ID of the oldest message from the previous page to get the next page.
    """

    dialog_id: int
    unread: bool = False
    limit: int = 100
    before_id: int | None = None


@tool_runner.register
async def list_messages(
    args: ListMessages,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    client: TelegramClient
    logger.info("method[ListMessages] args[%s]", args)

    response: list[TextContent] = []
    async with create_client() as client:
        result = await client(functions.messages.GetPeerDialogsRequest(peers=[args.dialog_id]))
        if not result:
            raise ValueError(f"Channel not found: {args.dialog_id}")

        if not isinstance(result, types.messages.PeerDialogs):
            raise TypeError(f"Unexpected result: {type(result)}")

        iter_messages_args: dict[str, t.Any] = {
            "entity": args.dialog_id,
            "reverse": False,
        }
        if args.unread:
            iter_messages_args["limit"] = min(dialog.unread_count, args.limit)
        else:
            iter_messages_args["limit"] = args.limit

        if args.before_id is not None:
            iter_messages_args["max_id"] = args.before_id

        async for message in client.iter_messages(**iter_messages_args):
            if isinstance(message, custom.Message) and message.text:
                response.append(TextContent(type="text", text=f"[id={message.id}] {message.text}"))

    return response


### GetMessage ###


class GetMessage(ToolArgs):
    """Get a single message by its ID. Use this to fetch a specific message when you know its ID."""

    dialog_id: int
    message_id: int


@tool_runner.register
async def get_message(
    args: GetMessage,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    client: TelegramClient
    logger.info("method[GetMessage] args[%s]", args)

    async with create_client() as client:
        message = await client.get_messages(args.dialog_id, ids=args.message_id)
        if message is None:
            raise ValueError(f"Message {args.message_id} not found in dialog {args.dialog_id}")
        text = message.text or "(no text)"
        date = message.date.isoformat() if message.date else "unknown"
        return [TextContent(type="text", text=f"[id={message.id}] [{date}] {text}")]


### SearchMessages ###


class SearchMessages(ToolArgs):
    """
    Search messages in a dialog by text query. Returns messages containing the query string,
    ordered from newest to oldest. Useful for finding when a topic was first discussed or
    locating a specific decision in chat history.
    """

    dialog_id: int
    query: str
    limit: int = 50


@tool_runner.register
async def search_messages(
    args: SearchMessages,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    client: TelegramClient
    logger.info("method[SearchMessages] args[%s]", args)

    response: list[TextContent] = []
    async with create_client() as client:
        async for message in client.iter_messages(args.dialog_id, search=args.query, limit=args.limit):
            if isinstance(message, custom.Message):
                text = message.text or "(no text)"
                date = message.date.isoformat() if message.date else "unknown"
                response.append(TextContent(type="text", text=f"[id={message.id}] [{date}] {text}"))

    return response


### GetDialog ###


class GetDialog(ToolArgs):
    """
    Get metadata for a dialog: name, type (private/group/channel), and date of the first message.
    Use this to understand the context of a conversation without reading its messages.
    """

    dialog_id: int


@tool_runner.register
async def get_dialog(
    args: GetDialog,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    client: TelegramClient
    logger.info("method[GetDialog] args[%s]", args)

    async with create_client() as client:
        entity = await client.get_entity(args.dialog_id)

        if isinstance(entity, types.User):
            name = " ".join(filter(None, [entity.first_name, entity.last_name]))
            dialog_type = "private"
        elif isinstance(entity, types.Chat):
            name = entity.title
            dialog_type = "group"
        elif isinstance(entity, types.Channel):
            name = entity.title
            dialog_type = "channel" if entity.broadcast else "supergroup"
        else:
            name = str(args.dialog_id)
            dialog_type = "unknown"

        first_messages = await client.get_messages(args.dialog_id, limit=1, reverse=True)
        if first_messages:
            first = first_messages[0]
            first_date = first.date.isoformat() if first.date else "unknown"
            first_id = first.id
        else:
            first_date = "unknown"
            first_id = None

        info = (
            f"id={args.dialog_id} name='{name}' type={dialog_type} "
            f"first_message_id={first_id} first_message_date={first_date}"
        )
        return [TextContent(type="text", text=info)]
