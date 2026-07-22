"""Read-only custom Telegram folder discovery."""

from pydantic import Field

from ._base import (
    DaemonNotRunningError,
    ToolAnnotations,
    ToolArgs,
    ToolResult,
    _check_daemon_response,
    _daemon_not_running_text,
    daemon_connection,
    error_result,
    mcp_tool,
    structured_result,
)
from .structured import TELEGRAM_CONTENT_OUTPUT_SCHEMA, telegram_content

LIST_FOLDERS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
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
        "count": {"type": "integer"},
    },
    "required": ["folders", "count"],
    "additionalProperties": False,
}


class ListFolders(ToolArgs):
    """List custom Telegram dialog folders. Archive is intentionally not a folder."""


@mcp_tool(
    name="list_folders",
    title="List Telegram Folders",
    posture="secondary/helper",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    output_schema=LIST_FOLDERS_OUTPUT_SCHEMA,
)
async def list_folders(args: ListFolders) -> ToolResult:
    del args
    try:
        async with daemon_connection() as conn:
            response = await conn.list_folders()
    except DaemonNotRunningError as exc:
        return error_result(_daemon_not_running_text(exc))
    if err := _check_daemon_response(response):
        return err
    folders = [
        {
            "id": int(folder["id"]),
            "title": telegram_content(str(folder.get("title", "")), "message_text"),
        }
        for folder in response.get("data", {}).get("folders", [])
    ]
    return structured_result(
        {
            "folders": folders,
            "count": len(folders),
        },
        result_count=len(folders),
    )


LIST_FOLDER_MESSAGES_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "folder_id": {"type": "integer"},
        "messages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "dialog_id": {"type": "integer"},
                    "message_id": {"type": "integer"},
                    "sent_at": {"type": "integer"},
                    "dialog_name": {
                        "type": ["object", "null"],
                        "properties": TELEGRAM_CONTENT_OUTPUT_SCHEMA["properties"],
                        "required": TELEGRAM_CONTENT_OUTPUT_SCHEMA["required"],
                        "additionalProperties": False,
                    },
                    "content": {
                        "type": ["object", "null"],
                        "properties": TELEGRAM_CONTENT_OUTPUT_SCHEMA["properties"],
                        "required": TELEGRAM_CONTENT_OUTPUT_SCHEMA["required"],
                        "additionalProperties": False,
                    },
                },
                "required": ["dialog_id", "message_id", "sent_at", "dialog_name", "content"],
                "additionalProperties": False,
            },
        },
        "count": {"type": "integer"},
        "partial": {"type": "boolean"},
        "incomplete_dialog_ids": {"type": "array", "items": {"type": "integer"}},
        "next_navigation": {"type": "null"},
    },
    "required": ["folder_id", "messages", "count", "partial", "incomplete_dialog_ids", "next_navigation"],
    "additionalProperties": False,
}


class ListFolderMessages(ToolArgs):
    """List locally stored messages across one custom Telegram folder, newest first."""

    folder_id: int
    limit: int = Field(default=20, ge=1, le=100)


@mcp_tool(
    name="list_folder_messages",
    title="List Folder Messages",
    posture="primary",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    output_schema=LIST_FOLDER_MESSAGES_OUTPUT_SCHEMA,
)
async def list_folder_messages(args: ListFolderMessages) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.list_folder_messages(folder_id=args.folder_id, limit=args.limit)
    except DaemonNotRunningError as exc:
        return error_result(_daemon_not_running_text(exc))
    if err := _check_daemon_response(response):
        return err
    data = response.get("data", {})
    messages = []
    for row in data.get("messages", []):
        item = dict(row)
        text = item.pop("text", None)
        dialog_name = item.get("dialog_name")
        item["dialog_name"] = telegram_content(str(dialog_name), "message_text") if dialog_name is not None else None
        item["content"] = telegram_content(str(text), "message_text") if text is not None else None
        messages.append(item)
    payload = {
        "folder_id": args.folder_id,
        "messages": messages,
        "count": len(messages),
        "partial": bool(data.get("partial", False)),
        "incomplete_dialog_ids": list(data.get("incomplete_dialog_ids", [])),
        "next_navigation": None,
    }
    return structured_result(payload, result_count=len(messages))
