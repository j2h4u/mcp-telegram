"""Explicit image preparation and local native image reads."""

import base64
import binascii
from typing import cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from mcp.types import ImageContent, ToolAnnotations
from pydantic import Field, StrictInt, model_validator

from ._base import (
    DaemonNotRunningError,
    ToolArgs,
    ToolResult,
    _check_daemon_response,
    _daemon_not_running_text,
    daemon_connection,
    error_result,
    mcp_tool,
    structured_result,
)

MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_ENCODED_BYTES = 4 * ((MAX_IMAGE_BYTES + 2) // 3)


class MessageImage(ToolArgs):
    """Select one message by its exact dialog and message identifiers."""

    exact_dialog_id: StrictInt
    message_id: int = Field(strict=True, gt=0)

    @model_validator(mode="after")
    def nonzero_dialog(self) -> MessageImage:
        if self.exact_dialog_id == 0:
            raise ValueError("exact_dialog_id must be nonzero")
        return self


class PrepareMessageImage(MessageImage):
    """Queue acquisition of the selected photo."""


class GetMessageImage(MessageImage):
    """Read the selected photo from local storage."""


_CONTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "state": {"enum": ["missing", "available", "unavailable"]},
        "mime_type": {"const": "image/jpeg"},
        "byte_size": {"type": "integer", "minimum": 1, "maximum": MAX_IMAGE_BYTES},
        "representation": {"const": "telegram_photo"},
        "width": {"type": "integer", "minimum": 1},
        "height": {"type": "integer", "minimum": 1},
        "reason": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,63}$"},
    },
    "required": ["state"],
    "additionalProperties": False,
}
IMAGE_STATUS_SCHEMA = {
    "type": "object",
    "properties": {
        "dialog_id": {"type": "integer"},
        "message_id": {"type": "integer", "minimum": 1},
        "content": _CONTENT_SCHEMA,
        "acquisition": {
            "type": "object",
            "properties": {
                "state": {"enum": ["idle", "queued", "running", "blocked", "failed", "complete"]},
                "reason": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,63}$"},
                "retry_at": {"type": "integer"},
            },
            "required": ["state"],
            "additionalProperties": False,
        },
    },
    "required": ["dialog_id", "message_id", "content", "acquisition"],
    "additionalProperties": False,
}


def decode_image(encoded: object, content: object) -> bytes:
    """Validate the private IPC payload and its public description."""
    if not isinstance(encoded, str) or len(encoded) > MAX_ENCODED_BYTES or not isinstance(content, dict):
        raise ValueError("invalid image payload")
    try:
        image = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid image payload") from exc
    if not 0 < len(image) <= MAX_IMAGE_BYTES or base64.b64encode(image).decode("ascii") != encoded:
        raise ValueError("invalid image payload")
    if (image[:3], image[-2:]) != (b"\xff\xd8\xff", b"\xff\xd9"):
        raise ValueError("invalid image payload")
    expected = {
        "state": "available",
        "mime_type": "image/jpeg",
        "representation": "telegram_photo",
        "byte_size": len(image),
    }
    if any(content.get(key) != value for key, value in expected.items()):
        raise ValueError("invalid image payload")
    return image


def _public_fields(value: object, allowed: tuple[str, ...]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("invalid image status")
    return {key: value[key] for key in allowed if key in value and value[key] is not None}


def _status(raw: object, args: MessageImage) -> dict[str, object]:
    if (
        not isinstance(raw, dict)
        or raw.get("dialog_id") != args.exact_dialog_id
        or raw.get("message_id") != args.message_id
    ):
        raise ValueError("invalid image status")
    content = _public_fields(
        raw.get("content"), ("state", "mime_type", "byte_size", "representation", "width", "height", "reason")
    )
    acquisition = _public_fields(raw.get("acquisition"), ("state", "reason", "retry_at"))
    status = {
        "dialog_id": args.exact_dialog_id,
        "message_id": args.message_id,
        "content": content,
        "acquisition": acquisition,
    }
    if not Draft202012Validator(IMAGE_STATUS_SCHEMA).is_valid(status):
        raise ValueError("invalid image status")
    if content.get("state") == "available" and any(
        key not in content for key in ("mime_type", "representation", "byte_size")
    ):
        raise ValueError("invalid image status")
    return status


async def _run(args: MessageImage, method: str) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.request(
                {"method": method, "dialog_id": args.exact_dialog_id, "message_id": args.message_id}
            )
    except DaemonNotRunningError as exc:
        return error_result(_daemon_not_running_text(exc))
    if err := _check_daemon_response(response):
        return err
    try:
        status = _status(response.get("data"), args)
        content = cast(dict[str, object], status["content"])
        encoded = response.get("image_base64")
        if method == "get_message_image" and content["state"] == "available":
            decode_image(encoded, content)
            return ToolResult(
                content=[ImageContent(type="image", data=cast(str, encoded), mime_type="image/jpeg")],
                structured_content=status,
            )
        if encoded is not None:
            raise ValueError("unexpected image payload")
        return structured_result(status)
    except ValueError:
        return error_result(
            "Invalid image response.\nAction: retry or inspect sync status.", error_code="invalid_image_response"
        )


@mcp_tool(
    name="prepare_message_image",
    title="Prepare Message Image",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
    ),
    output_schema=IMAGE_STATUS_SCHEMA,
)
async def prepare_message_image(args: PrepareMessageImage) -> ToolResult:
    """Explicitly queue bounded acquisition for one native Telegram photo."""
    return await _run(args, "prepare_message_image")


@mcp_tool(
    name="get_message_image",
    title="Get Message Image",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
    output_schema=IMAGE_STATUS_SCHEMA,
)
async def get_message_image(args: GetMessageImage) -> ToolResult:
    """Read a retained photo locally; never queue or contact Telegram."""
    return await _run(args, "get_message_image")
