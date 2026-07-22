"""Read-only custom Telegram folder discovery."""

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

LIST_FOLDERS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "folders": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "title": {"type": "string"}},
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
    folders = response.get("data", {}).get("folders", [])
    return structured_result({"folders": folders, "count": len(folders)}, result_count=len(folders))
