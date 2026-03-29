from __future__ import annotations

import logging

from pydantic import Field

logger = logging.getLogger(__name__)

from ..errors import (
    no_active_topics_text,
    no_dialogs_text,
)
from ..resolver import parse_exact_dialog_id
from ._base import (
    DaemonNotRunningError,
    ToolArgs,
    ToolResult,
    _daemon_not_running_text,
    _text_response,
    daemon_connection,
    mcp_tool,
)


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
    try:
        async with daemon_connection() as conn:
            response = await conn.list_dialogs(
                exclude_archived=args.exclude_archived,
                ignore_pinned=args.ignore_pinned,
            )
    except DaemonNotRunningError:
        return ToolResult(content=_text_response(_daemon_not_running_text()))

    if not response.get("ok"):
        error_msg = response.get("message", "Daemon returned an error.")
        return ToolResult(content=_text_response(f"Error: {error_msg}"))

    data = response.get("data", {})
    dialogs = data.get("dialogs", [])

    if not dialogs:
        return ToolResult(content=_text_response(no_dialogs_text()))

    entity_dicts: list[dict] = []
    lines: list[str] = []

    for d in dialogs:
        dialog_id = d.get("id")
        dialog_name = d.get("name", "")
        dialog_type = d.get("type", "unknown")
        last_at = d.get("last_message_at", "unknown")
        unread_count = d.get("unread_count", 0)
        sync_status = d.get("sync_status", "unknown")

        members = d.get("members")
        created = d.get("created")
        meta = ""
        if members is not None:
            meta += f" members={members}"
        if created is not None:
            meta += f" created={created}"

        lines.append(
            f"name='{dialog_name}' id={dialog_id} type={dialog_type} "
            f"last_message_at={last_at} unread={unread_count}{meta} sync_status={sync_status}"
        )

        # Upsert entities into daemon for future name resolution
        if isinstance(dialog_id, int) and isinstance(dialog_name, str):
            entity_dicts.append({"id": dialog_id, "type": dialog_type, "name": dialog_name, "username": None})

    if entity_dicts:
        try:
            async with daemon_connection() as upsert_conn:
                await upsert_conn.upsert_entities(entities=entity_dicts)
        except Exception:
            logger.debug("entity_upsert_skipped", exc_info=True)

    result_text = "\n".join(lines)
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
    # Try to resolve dialog_id from parsing as numeric/username first
    dialog_id: int | None = parse_exact_dialog_id(args.dialog)
    dialog_name: str | None = None if dialog_id is not None else args.dialog

    try:
        async with daemon_connection() as conn:
            if dialog_id is not None and dialog_id != 0:
                response = await conn.list_topics(dialog_id=dialog_id)
            else:
                response = await conn.list_topics(dialog=dialog_name)
    except DaemonNotRunningError:
        return ToolResult(content=_text_response(_daemon_not_running_text()), has_filter=True)

    if not response.get("ok"):
        error_code = response.get("error", "")
        error_msg = response.get("message", "Daemon returned an error.")
        if error_code == "dialog_not_found":
            from ..errors import dialog_not_found_text
            return ToolResult(
                content=_text_response(
                    dialog_not_found_text(args.dialog, retry_tool="ListTopics")
                ),
                has_filter=True,
            )
        return ToolResult(content=_text_response(f"Error: {error_msg}"), has_filter=True)

    data = response.get("data", {})
    topics = data.get("topics", [])

    if not topics:
        dialog_display = args.dialog
        return ToolResult(
            content=_text_response(no_active_topics_text(dialog_display)),
            has_filter=True,
        )

    lines: list[str] = []
    for topic in topics:
        topic_id = topic.get("id")
        title = topic.get("title", "")
        lines.append(f'topic_id={topic_id} title="{title}"')

    result_text = "\n".join(lines)
    return ToolResult(content=_text_response(result_text), result_count=len(lines), has_filter=True)


class GetMyAccount(ToolArgs):
    """Return own account info: numeric id, display name, and username. No arguments required."""

    pass


@mcp_tool("secondary/helper")
async def get_my_account(args: GetMyAccount) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.get_me()
    except DaemonNotRunningError:
        return ToolResult(content=_text_response(_daemon_not_running_text()))

    if not response.get("ok"):
        error_msg = response.get("message", "Daemon returned an error.")
        return ToolResult(content=_text_response(f"Error: {error_msg}"))

    data = response.get("data", {})
    if not data:
        from ..errors import not_authenticated_text
        return ToolResult(content=_text_response(not_authenticated_text("GetMyAccount")))

    first_name = data.get("first_name") or ""
    last_name = data.get("last_name") or ""
    name = " ".join(filter(None, [first_name, last_name]))
    username = data.get("username") or "none"
    user_id = data.get("id", 0)

    text = f"id={user_id} name='{name}' username=@{username}"
    return ToolResult(content=_text_response(text), result_count=1)
