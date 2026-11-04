"""Public arguments for native private contact notes."""

from typing import Literal, Self, cast

from mcp.types import ToolAnnotations
from pydantic import ConfigDict, Field, StrictInt, model_validator
from pydantic.config import JsonSchemaExtraCallable

from ._base import ToolArgs, ToolResult, mcp_tool


def _note_schema(schema: dict[str, object]) -> None:
    schema["allOf"] = [
        {
            "if": {"properties": {"action": {"const": action}}, "required": ["action"]},
            "then": {"required": ["text"]}
            if action == "update"
            else {
                "required": ["text"] if action == "create" else [],
                "not": {
                    "anyOf": [
                        {"required": [field]}
                        for field in (("drop_formatting",) if action == "create" else ("text", "drop_formatting"))
                    ]
                },
            },
        }
        for action in ("create", "update", "clear")
    ]


class ManageContactNote(ToolArgs):
    """Change one contact note using its version from get_entity_info.

    Telegram has no atomic compare-and-swap: another client can change a note
    between preflight and the write. Reuse the operation key to inspect an
    interrupted operation instead of starting a second write.
    """

    model_config = ConfigDict(json_schema_extra=cast(JsonSchemaExtraCallable, _note_schema))

    dialog_id: StrictInt
    action: Literal["create", "update", "clear"]
    expected_version: str = Field(min_length=64, max_length=64)
    operation_key: str = Field(min_length=1, max_length=128)
    text: str | None = Field(default=None, min_length=1, max_length=65536)
    drop_formatting: bool = False

    @model_validator(mode="after")
    def validate_command(self) -> Self:
        if self.dialog_id <= 0 or not self.operation_key.strip() or not self.expected_version.strip():
            raise ValueError("dialog_id, operation_key and expected_version are required")
        self._validate_action()
        return self

    def _validate_action(self) -> None:
        if self.action in {"create", "update"} and (self.text is None or not self.text):
            raise ValueError("create/update require text")
        if self.action == "clear" and self.model_fields_set & {"text", "drop_formatting"}:
            raise ValueError("clear does not accept text or drop_formatting")
        if self.action == "create" and "drop_formatting" in self.model_fields_set:
            raise ValueError("drop_formatting applies only to update")


MANAGE_CONTACT_NOTE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "account_id": {"type": "integer"},
        "operation_key": {"type": "string"},
        "outcome": {"enum": ["unchanged", "not_sent", "rejected", "acknowledged", "unknown"]},
        "verification": {
            "type": "string",
            "description": "Historical observation; it does not guarantee the note remains unchanged.",
        },
        "current_applicability": {"enum": ["unknown"]},
        "required_action": {"type": "string"},
        "verification_order": {
            "type": "integer",
            "description": "Order of the accepted historical observation, when available.",
        },
    },
    "required": ["ok", "account_id", "operation_key", "outcome", "verification"],
    "additionalProperties": False,
}


@mcp_tool(
    name="manage_contact_note",
    title="Manage Contact Note",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
    ),
    output_schema=MANAGE_CONTACT_NOTE_OUTPUT_SCHEMA,
)
async def manage_contact_note(args: ManageContactNote) -> ToolResult:
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
                {"method": "manage_contact_note", **args.model_dump(exclude_unset=True, exclude={"timezone"})}
            )
    except DaemonNotRunningError as exc:
        if exc.kind in {"not_running", "connect_timeout"}:
            return error_result(_daemon_not_running_text(exc))
        return error_result(
            "Contact note completion is unconfirmed.\nAction: retry the exact operation using the same operation key.",
            error_code="contact_note_completion_unconfirmed",
        )
    if error := _check_daemon_response(response):
        return error
    response.pop("request_id", None)
    if response.get("verification") == "inconclusive" and response.get("outcome") in {"unknown", "acknowledged"}:
        response["required_action"] = (
            "The outcome remains unknown. Inspect the current local note and reuse the exact operation key and request body to inspect it; "
            "do not repeat the write while the outcome is unresolved."
            if response.get("outcome") == "unknown"
            else "The write was acknowledged, but its result could not be verified. Inspect the current local note; "
            "do not retry the write with the same or a new key."
        )
    return structured_result(response)
