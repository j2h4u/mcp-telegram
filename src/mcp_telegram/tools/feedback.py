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
    _text_response,
    daemon_connection,
    error_result,
    mcp_tool,
)


class SubmitFeedback(ToolArgs):
    """
    Send feedback to the maintainer of this MCP server — bugs, confusing
    behaviour, or improvement suggestions. Use this proactively whenever
    you notice that a tool's response is unhelpful, surprising, or wrong;
    when an error message is unclear; or when you can think of a missing
    capability that would have helped on the current task.

    Fields:
      message:  required. Free-form text describing what you observed and,
                if you have one, what you would expect instead. Up to
                10,000 characters.
      severity: optional. One of:
                  bug        — the tool produced incorrect output, crashed,
                               or violated its documented contract
                  suggestion — request for a new tool, new option, or
                               improved UX
                  question   — you're unsure how something is supposed to
                               work and want the maintainer to clarify the
                               docs
      context:  optional. Which tool you were calling and what arguments
                you passed (or what task you were trying to accomplish).
                Helps the maintainer reproduce the issue. Up to 2,000 chars.
      model:    optional. Your model name (e.g. "claude-opus-4-7"). Helps
                the maintainer correlate feedback with model versions.
      harness:  optional. Your client name (e.g. "Claude Desktop", "Cursor",
                "Codex CLI"). Helps the maintainer prioritise client-specific
                issues. Up to 200 chars.

    Submissions are fire-and-forget — there is no follow-up tool, no
    tracking ID, and no read access for agents. The maintainer reviews
    feedback out-of-band and may act on it in future releases.
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


@mcp_tool("primary", annotations=ToolAnnotations(readOnlyHint=False))
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

    data = response.get("data", {})
    feedback_id = data.get("id")
    suffix = f" id={feedback_id}" if feedback_id is not None else ""
    return ToolResult(content=_text_response(f"Feedback recorded. Thank you!{suffix}"), result_count=1)
