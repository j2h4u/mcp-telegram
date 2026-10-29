"""Create, update or cancel one native Telegram scheduled message."""

from collections.abc import Mapping
from typing import Literal, cast

from mcp.types import ToolAnnotations
from pydantic import Field, StrictInt, model_validator

from ..dialog_selector import (
    EXACT_DIALOG_ID_DESCRIPTION,
    NATURAL_DIALOG_SELECTOR_DESCRIPTION,
    DialogSelectorError,
    required_dialog_selector,
)
from ..scheduled_contracts import SCHEDULED_OPERATION_KEY_LIMIT
from ..temporal import parse_utc_boundary
from ._base import (
    DaemonNotRunningError,
    ToolArgs,
    ToolResult,
    _check_daemon_response,
    _daemon_not_running_text,
    daemon_connection,
    error_result,
    mcp_tool,
    omit_none_mapping_values,
    structured_result,
)
from .dialog_resolution import project_dialog_resolution_error

# Keep MCP validation aligned with the command owner's supported native intervals.
_SCHEDULED_REPEAT_PERIODS = frozenset({86_400, 604_800, 1_209_600, 2_592_000, 7_862_400, 15_724_800, 31_536_000})


class ManageScheduledMessage(ToolArgs):
    """Manage one native scheduled item in a specifically selected dialog.

    Create is text-only. Update changes only supplied text, UTC schedule time or
    repeat period; existing attachments and reply/topic context are retained.
    Cancel removes the scheduled item, including its native repeat. Read the
    scheduled row first for its queue ID and expected_version. Reuse operation_key
    only to look up the same command receipt after an interrupted request; it is
    not a guarantee that arbitrary retries are safe. Scheduling can publish
    automatically at the selected time.
    """

    action: Literal["create", "update", "cancel"]
    dialog: str | None = Field(default=None, max_length=500, description=NATURAL_DIALOG_SELECTOR_DESCRIPTION)
    exact_dialog_id: StrictInt | None = Field(default=None, description=EXACT_DIALOG_ID_DESCRIPTION)
    account_id: StrictInt | None = Field(default=None, gt=0, description="Optional expected account id.")
    topic_id: StrictInt | None = Field(default=None, gt=0, description="Optional exact topic scope within the dialog.")
    operation_key: str = Field(min_length=1, max_length=SCHEDULED_OPERATION_KEY_LIMIT)
    scheduled_message_id: StrictInt | None = Field(default=None, gt=0, description="Queue ID, not a sent-message ID.")
    expected_version: str | None = Field(default=None, min_length=1, max_length=256)
    text: str | None = Field(default=None, max_length=65_536, description="Plain text; create does not accept media.")
    schedule_at_utc: str | None = Field(
        default=None,
        description="Absolute RFC3339 UTC time (Z or +00:00). New writes need at least 60 seconds of lead at dispatch; same-key receipt replay may reuse the original time.",
    )
    repeat_period_seconds: StrictInt | None = Field(
        default=None,
        description="0 disables repeat; supported native intervals are 86400, 604800, 1209600, 2592000, 7862400, 15724800, or 31536000 seconds. Omitted means no repeat on create and preserve on update.",
        json_schema_extra={"enum": [0, *_SCHEDULED_REPEAT_PERIODS]},
    )
    drop_formatting: bool = Field(
        default=False,
        description="Explicitly discard existing formatting when replacing formatted text.",
    )

    @model_validator(mode="after")
    def validate_request(self) -> ManageScheduledMessage:
        try:
            required_dialog_selector(exact_id=self.exact_dialog_id, dialog=self.dialog)
        except DialogSelectorError as exc:
            raise ValueError(f"{exc.code}: {exc}") from exc
        if not self.operation_key.strip() or self.operation_key != self.operation_key.strip():
            raise ValueError("operation_key must contain 1 to 128 non-space-padded characters")
        if "repeat_period_seconds" in self.model_fields_set and self.repeat_period_seconds is None:
            raise ValueError("repeat_period_seconds must be an exact integer when supplied")
        _validate_scheduled_action(self, _schedule_timestamp(self.schedule_at_utc))
        return self


def _schedule_timestamp(value: str | None) -> int | None:
    return parse_utc_boundary(value, field="schedule_at_utc")


def _validate_scheduled_action(args: ManageScheduledMessage, schedule_at: int | None) -> None:
    _validate_repeat_period(args.repeat_period_seconds)
    match args.action:
        case "create":
            _validate_scheduled_create(args, schedule_at)
        case "update":
            _validate_scheduled_update(args)
        case "cancel":
            _validate_scheduled_cancel(args)


def _validate_repeat_period(value: int | None) -> None:
    if value not in (None, 0, *_SCHEDULED_REPEAT_PERIODS):
        raise ValueError("repeat_period_seconds must be 0 or a supported native interval in seconds")


def _validate_scheduled_create(args: ManageScheduledMessage, schedule_at: int | None) -> None:
    if args.scheduled_message_id is not None or args.expected_version is not None:
        raise ValueError("create does not accept scheduled_message_id or expected_version")
    if args.text is None or not args.text.strip() or schedule_at is None:
        raise ValueError("create requires nonempty text and a future schedule_at_utc")
    if args.drop_formatting:
        raise ValueError("drop_formatting applies only to text updates")


def _validate_scheduled_cancel(args: ManageScheduledMessage) -> None:
    if args.scheduled_message_id is None or args.expected_version is None:
        raise ValueError("cancel requires scheduled_message_id and expected_version from the local queue read")
    if args.model_fields_set & {"text", "schedule_at_utc", "repeat_period_seconds"}:
        raise ValueError("cancel accepts no replacement fields")
    if args.drop_formatting:
        raise ValueError("cancel accepts no replacement fields")


def _validate_scheduled_update(args: ManageScheduledMessage) -> None:
    if args.scheduled_message_id is None or args.expected_version is None:
        raise ValueError("update requires scheduled_message_id and expected_version from the local queue read")
    if not _has_actual_scheduled_edit(args):
        raise ValueError("update requires at least one requested field change")
    if args.text is not None and not args.text.strip():
        raise ValueError("replacement text must be nonempty")
    if args.drop_formatting and args.text is None:
        raise ValueError("drop_formatting is only used when replacing text")


def _has_actual_scheduled_edit(args: ManageScheduledMessage) -> bool:
    return any(
        field in args.model_fields_set and getattr(args, field) is not None
        for field in ("text", "schedule_at_utc", "repeat_period_seconds")
    )


MANAGE_SCHEDULED_MESSAGE_OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "action": {"enum": ["create", "update", "cancel"]},
        "operation_key": {"type": "string"},
        "account_id": {"type": "integer"},
        "dialog_id": {"type": "integer"},
        "scheduled_message_id": {"type": ["integer", "null"], "description": "Telegram scheduled queue identity."},
        "outcome": {"enum": ["acknowledged", "unchanged", "unknown"]},
        "mutation_dispatched": {"type": ["boolean", "null"]},
        "acknowledged": {"type": ["boolean", "null"]},
        "receipt_persisted": {"type": "boolean"},
        "current_is_operation_confirmation": {"type": "boolean"},
        "schedule_at_utc": {"type": ["string", "null"]},
    },
    "required": [
        "ok",
        "action",
        "operation_key",
        "account_id",
        "dialog_id",
        "scheduled_message_id",
        "outcome",
        "mutation_dispatched",
        "acknowledged",
        "receipt_persisted",
        "current_is_operation_confirmation",
        "schedule_at_utc",
    ],
    "additionalProperties": False,
}


def _scheduled_request(args: ManageScheduledMessage) -> dict[str, object]:
    selector = required_dialog_selector(exact_id=args.exact_dialog_id, dialog=args.dialog)
    request = args.model_dump(exclude_unset=True, exclude={"dialog", "exact_dialog_id"})
    request["method"] = "manage_scheduled_message"
    if selector.exact_id is not None:
        request["dialog_id"] = selector.exact_id
    else:
        request["dialog"] = selector.query
    return request


def _scheduled_error_response(response: Mapping[str, object]) -> ToolResult | None:
    if response.get("ok") is True:
        return None
    projection = project_dialog_resolution_error(response, fallback_action="Retry with an exact_dialog_id.")
    if projection is not None:
        return ToolResult(
            content=error_result(projection.text).content,
            is_error=True,
            structured_content=projection.structured_content,
        )
    details = (
        dict(cast(Mapping[str, object], response.get("details", {})))
        if isinstance(response.get("details"), Mapping)
        else {}
    )
    for field in (
        "action",
        "operation_key",
        "account_id",
        "dialog_id",
        "scheduled_message_id",
        "outcome",
        "mutation_dispatched",
        "acknowledged",
        "receipt_persisted",
        "current_is_operation_confirmation",
        "schedule_at_utc",
    ):
        if field in response:
            details[field] = response[field]
    return _check_daemon_response({**response, "details": details})


@mcp_tool(
    name="manage_scheduled_message",
    title="Manage Scheduled Message",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
    ),
    output_schema=MANAGE_SCHEDULED_MESSAGE_OUTPUT_SCHEMA,
)
async def manage_scheduled_message(args: ManageScheduledMessage) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.request(_scheduled_request(args))
    except DaemonNotRunningError as exc:
        if exc.kind in {"not_running", "connect_timeout"}:
            return error_result(_daemon_not_running_text(exc))
        return error_result(
            "Scheduled-message completion is unconfirmed.\nAction: inspect the local scheduled queue and replay "
            f"the same command with operation_key={args.operation_key!r} to look up its receipt; do not submit a new key.",
            error_code="scheduled_message_completion_unconfirmed",
            details={"outcome": "unknown", "operation_key": args.operation_key, "failure_kind": exc.kind},
        )
    if err := _scheduled_error_response(response):
        return err
    response.pop("request_id", None)
    return structured_result(cast(Mapping[str, object], omit_none_mapping_values(response)))
