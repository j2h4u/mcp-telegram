from __future__ import annotations

from pydantic import Field

from .. import capabilities
from ..dialog_target import classify_dialog
from ..errors import (
    no_active_topics_text,
    no_dialogs_text,
    not_authenticated_text,
)
from ._base import ToolArgs, ToolResult, _resolve_dialog, _text_response, connected_client, get_entity_cache, mcp_tool


class ListDialogs(ToolArgs):
    """List available dialogs, chats and channels with type and last message timestamp.

    Returns both archived and non-archived dialogs by default (Telegram uses archiving as a UI
    organization tool, not data archival). Set exclude_archived=True to show only non-archived
    dialogs (equivalent to old archived=False behavior).
    """

    exclude_archived: bool = False
    ignore_pinned: bool = False


@mcp_tool("secondary/helper")
async def list_dialogs(args: ListDialogs) -> ToolResult:
    cache = get_entity_cache()
    lines: list[str] = []
    batch_entities: list[tuple[int, str, str, str | None]] = []
    async with connected_client() as client:
        telethon_archived_param = None if not args.exclude_archived else False

        async for dialog in client.iter_dialogs(
            archived=telethon_archived_param, ignore_pinned=args.ignore_pinned
        ):
            dtype = classify_dialog(dialog)
            last_at = dialog.date.strftime("%Y-%m-%d %H:%M") if dialog.date else "unknown"
            # Collect for batch cache upsert
            dialog_id = getattr(dialog, "id", None)
            dialog_name = getattr(dialog, "name", None)
            if isinstance(dialog_id, int) and isinstance(dialog_name, str):
                entity = getattr(dialog, "entity", None)
                username = getattr(entity, "username", None) if entity is not None else None
                batch_entities.append((dialog_id, dtype, dialog_name, username))
            lines.append(
                f"name='{dialog.name}' id={dialog.id} type={dtype} "
                f"last_message_at={last_at} unread={dialog.unread_count}"
            )
    if batch_entities:
        cache.upsert_batch(batch_entities)
    result_text = "\n".join(lines) if lines else no_dialogs_text()
    return ToolResult(content=_text_response(result_text), result_count=len(lines))


class ListTopics(ToolArgs):
    """
    List forum topics for one dialog.

    Use this before topic= when working with forum supergroups so you can choose an exact topic
    name or numeric topic_id instead of guessing via fuzzy match.
    """

    dialog: str = Field(max_length=500)


@mcp_tool("secondary/helper")
async def list_topics(args: ListTopics) -> ToolResult:
    cache = get_entity_cache()
    async with connected_client() as client:
        topic_execution = await capabilities.execute_list_topics_capability(
            client,
            cache=cache,
            dialog_query=args.dialog,
            retry_tool="ListTopics",
            resolve_dialog=_resolve_dialog,
            load_topics=capabilities.load_dialog_topics,
        )
    if isinstance(
        topic_execution,
        (capabilities.DialogTargetFailure, capabilities.ForumTopicFailure),
    ):
        return ToolResult(content=_text_response(topic_execution.text), has_filter=True)

    result_count = len(topic_execution.active_topics)
    if not topic_execution.active_topics:
        text = topic_execution.resolve_prefix + no_active_topics_text(
            topic_execution.dialog_name
        )
        return ToolResult(content=_text_response(text), has_filter=True)

    lines = [capabilities.topic_row_text(topic) for topic in topic_execution.active_topics]
    result_text = topic_execution.resolve_prefix + "\n".join(lines)
    return ToolResult(content=_text_response(result_text), result_count=result_count, has_filter=True)


class GetMyAccount(ToolArgs):
    """Return own account info: numeric id, display name, and username. No arguments required."""

    pass


@mcp_tool("secondary/helper")
async def get_my_account(args: GetMyAccount) -> ToolResult:
    async with connected_client() as client:
        me = await client.get_me()
    if me is None:
        return ToolResult(content=_text_response(not_authenticated_text("GetMyAccount")))
    name = " ".join(filter(None, [
        getattr(me, "first_name", None),
        getattr(me, "last_name", None),
    ]))
    username = getattr(me, "username", None) or "none"
    text = f"id={me.id} name='{name}' username=@{username}"
    return ToolResult(content=_text_response(text), result_count=1)
