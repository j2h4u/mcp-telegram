"""MCP tool for recent important daemon-observed events."""

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

LIST_IMPORTANT_EVENTS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "timezone": {"type": "string"},
        "last_hours": {"type": "integer"},
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "time": {"type": "string"},
                    "time_basis": {"type": "string", "enum": ["telegram", "observed"]},
                    "type": {"type": "string"},
                    "summary": {"type": "string"},
                    "dialog_id": {"type": ["integer", "null"]},
                    "dialog_title": {"type": ["string", "null"]},
                    "message_id": {"type": ["integer", "null"]},
                },
                "required": [
                    "time",
                    "time_basis",
                    "type",
                    "summary",
                    "dialog_id",
                    "dialog_title",
                    "message_id",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["timezone", "last_hours", "events"],
    "additionalProperties": False,
}


class ListImportantEvents(ToolArgs):
    """List important access lifecycle events observed in the recent time window."""

    last_hours: int = Field(default=24, ge=1, le=24 * 30, description="How many recent hours to include")


@mcp_tool(
    name="list_important_events",
    title="Important Events",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
    output_schema=LIST_IMPORTANT_EVENTS_OUTPUT_SCHEMA,
)
async def list_important_events(args: ListImportantEvents) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.list_important_events(last_hours=args.last_hours, timezone=args.timezone)
    except DaemonNotRunningError as exc:
        return error_result(_daemon_not_running_text(exc))
    if err := _check_daemon_response(response):
        return err
    data = response.get("data")
    if not isinstance(data, dict):
        return error_result("Error: daemon returned an invalid important events response.")
    return structured_result(
        data, result_count=len(data.get("events", [])) if isinstance(data.get("events"), list) else 0
    )


__all__ = ["ListImportantEvents", "list_important_events"]
