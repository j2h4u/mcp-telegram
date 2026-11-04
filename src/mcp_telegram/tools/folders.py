"""Public arguments for native Telegram folder operations."""

from mcp.types import ToolAnnotations

from ..folders.arguments import FolderCommandArguments, FolderReadArguments
from ._base import ToolArgs, ToolResult, mcp_tool
from .structured import FOLDER_SNAPSHOT_OUTPUT_SCHEMA


class GetFolder(FolderReadArguments, ToolArgs):
    """Read one retained folder definition without contacting Telegram."""


class ManageFolder(FolderCommandArguments, ToolArgs):
    """Change one ordinary native folder using its locally observed version.

    Add explicitly includes a dialog and clears its exclusion; remove explicitly
    excludes it. Clear-exclusion changes only the exclusion. Changes are not
    atomic across folders. Reuse an operation key after an uncertain result.
    """


_FOLDER_DEFINITION_PROPERTIES = {
    "folder_id": {"type": "integer"},
    "title": {"type": "string", "description": "Untrusted Telegram content."},
    "namespace": {"type": "string"},
    "kind": {"enum": ["filter", "chatlist", "default"]},
    "source_position": {"type": ["integer", "null"]},
    **{
        field: {"type": "array", "items": {"type": "integer"}}
        for field in ("included_ids", "pinned_ids", "excluded_ids")
    },
    "categories": {"type": "array", "items": {"enum": ["contact", "non_contact", "bot", "group", "broadcast"]}},
    **{
        field: {"type": "boolean"}
        for field in (
            "exclude_archived",
            "exclude_read",
            "exclude_muted",
            "title_noanimate",
            "has_my_invites",
            "complete",
        )
    },
    "title_entities": {"type": "array", "items": {"type": "object"}},
    "emoticon": {"type": ["string", "null"]},
    "color": {"type": ["integer", "null"]},
}

FOLDER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "account_id": {"type": "integer"},
        "presence": {"enum": ["present", "absent", "unknown"]},
        "catalog_version": {"type": ["string", "null"]},
        "expected_version": {"type": ["string", "null"]},
        "definition": {
            "type": "object",
            "properties": _FOLDER_DEFINITION_PROPERTIES,
            "required": list(_FOLDER_DEFINITION_PROPERTIES),
            "additionalProperties": False,
        },
        "definition_complete": {"type": "boolean"},
        "supported_actions": {"type": "array", "items": {"enum": ["update", "delete"]}},
        "unsupported_reason": {"type": ["string", "null"]},
        "membership": {
            "type": "object",
            "properties": {"present": {"type": "integer"}, "unknown": {"type": "integer"}},
            "required": ["present", "unknown"],
            "additionalProperties": False,
        },
        "folder_snapshot": FOLDER_SNAPSHOT_OUTPUT_SCHEMA,
        "directory_coverage": {"type": "object"},
        "acquisition": {
            "type": "object",
            "properties": {
                "coverage": {"enum": ["complete", "unavailable"]},
                "last_attempt_at": {"type": ["integer", "null"]},
                "last_outcome": {"type": ["string", "null"]},
                "next_retry_at": {"type": ["integer", "null"]},
                "consecutive_failures": {"type": "integer"},
            },
            "required": ["coverage"],
            "additionalProperties": False,
        },
    },
    "required": ["ok", "account_id", "presence", "catalog_version"],
    "additionalProperties": False,
}
MANAGE_FOLDER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "account_id": {"type": "integer"},
        "operation_key": {"type": "string"},
        "folder_id": {"type": "integer"},
        "phase": {"enum": ["preparing", "completed"]},
        "outcome": {"enum": ["unchanged", "not_sent", "rejected", "acknowledged", "unknown"]},
        "dispatch": {"enum": ["not_sent", "sent", "unknown"]},
        "acknowledgement": {"enum": ["yes", "no", "unknown"]},
        "verification": {"enum": ["not_required", "pending", "matched", "mismatched", "unavailable", "exhausted"]},
        "next_action": {"type": "string"},
        "current_applicability": {
            "enum": ["unknown"],
            "description": "Later intake or an account change invalidated the current-state interpretation of the historical result.",
        },
        "preserved_overrides": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "Selected cleared exclusions that remain explicitly included or pinned.",
        },
    },
    "required": [
        "ok",
        "account_id",
        "operation_key",
        "folder_id",
        "phase",
        "outcome",
        "dispatch",
        "acknowledgement",
        "verification",
        "next_action",
    ],
    "additionalProperties": False,
}


@mcp_tool(
    name="get_folder",
    title="Get Folder",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
    output_schema=FOLDER_OUTPUT_SCHEMA,
)
async def get_folder(args: GetFolder) -> ToolResult:
    from ._base import (
        DaemonNotRunningError,
        _check_daemon_response,
        _daemon_not_running_text,
        daemon_connection,
        error_result,
        structured_result,
    )

    try:
        async with daemon_connection() as conn:
            response = await conn.request({"method": "get_folder", "folder_id": args.folder_id})
    except DaemonNotRunningError as exc:
        return error_result(_daemon_not_running_text(exc))
    if error := _check_daemon_response(response):
        return error
    response.pop("request_id", None)
    return structured_result(response)


@mcp_tool(
    name="manage_folder",
    title="Manage Folder",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
    ),
    output_schema=MANAGE_FOLDER_OUTPUT_SCHEMA,
)
async def manage_folder(args: ManageFolder) -> ToolResult:
    from ._base import (
        DaemonNotRunningError,
        _check_daemon_response,
        _daemon_not_running_text,
        daemon_connection,
        error_result,
        structured_result,
    )

    try:
        async with daemon_connection() as conn:
            response = await conn.request(
                {"method": "manage_folder", **args.model_dump(exclude_unset=True, exclude={"timezone"})}
            )
    except DaemonNotRunningError as exc:
        if exc.kind in {"not_running", "connect_timeout"}:
            return error_result(_daemon_not_running_text(exc))
        return error_result(
            "Folder completion is unconfirmed.\nAction: retry the exact operation using the same operation key.",
            error_code="folder_completion_unconfirmed",
        )
    if error := _check_daemon_response(response):
        return error
    response.pop("request_id", None)
    return structured_result(response)
