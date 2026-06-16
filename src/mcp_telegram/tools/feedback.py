"""MCP tool: SubmitFeedback — agent-facing feedback channel (Phase 48).

Routes feedback messages from the agent → daemon socket → feedback.db.
No read tool exists by design (admin-only via mcp-telegram feedback list CLI).
"""

from typing import Literal

from mcp.types import ToolAnnotations
from pydantic import Field, field_validator

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

SUBMIT_FEEDBACK_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "accepted": {"type": "boolean"},
        "severity": {"type": ["string", "null"], "enum": ["bug", "suggestion", "question", None]},
        "has_context": {"type": "boolean"},
        "has_model": {"type": "boolean"},
        "has_harness": {"type": "boolean"},
        "follow_up_available": {"type": "boolean"},
        "tracking_id": {"type": "null"},
        "status": {"type": "string"},
        "message": {"type": "string"},
    },
    "required": [
        "accepted",
        "severity",
        "has_context",
        "has_model",
        "has_harness",
        "follow_up_available",
        "tracking_id",
    ],
    "additionalProperties": True,
}


class SubmitFeedback(ToolArgs):
    """
    Report a bug, confusing response, unclear error, or missing capability to
    the maintainer. Use proactively when the current task exposes tool friction.
    Submissions are fire-and-forget: no read-back tool, tracking ID, or agent
    access to the feedback queue.
    """

    message: str = Field(min_length=1, max_length=10000)
    severity: Literal["bug", "suggestion", "question"] | None = Field(default=None)
    context: str | None = Field(default=None, max_length=2000)
    model: str | None = Field(default=None, max_length=200)
    harness: str | None = Field(default=None, max_length=200)

    @field_validator("message", mode="before")
    @classmethod
    def strip_and_require_nonempty(cls, v: str) -> str:
        """Strip whitespace and reject whitespace-only payloads.

        Pydantic ``min_length=1`` allows strings like ``"   "`` to pass
        schema validation; the daemon then rejects them with a
        ``.strip()`` check, which costs an extra socket round-trip.
        Validating + normalising here gives the agent an immediate
        local error and guarantees the daemon receives an already-
        stripped message.
        """
        if not isinstance(v, str):
            raise TypeError("message must be a string")
        stripped = v.strip()
        if not stripped:
            raise ValueError("message must not be empty or whitespace-only")
        return stripped


@mcp_tool(
    name="submit_feedback",
    title="Submit Feedback",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
    output_schema=SUBMIT_FEEDBACK_OUTPUT_SCHEMA,
)
async def submit_feedback(args: SubmitFeedback) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.submit_feedback(
                message=args.message,
                severity=args.severity,
                context=args.context,
                model=args.model,
                harness=args.harness,
            )
    except DaemonNotRunningError:
        return error_result(_daemon_not_running_text())

    if err := _check_daemon_response(response):
        return err

    message = "Feedback recorded. Thank you!"
    structured_content = {
        "accepted": True,
        "severity": args.severity,
        "has_context": args.context is not None,
        "has_model": args.model is not None,
        "has_harness": args.harness is not None,
        "follow_up_available": False,
        "tracking_id": None,
        "status": "accepted",
        "message": message,
    }
    return structured_result(structured_content, result_count=1)
