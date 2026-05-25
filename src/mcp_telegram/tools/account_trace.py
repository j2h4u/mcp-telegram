from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from ..formatter import frame_telegram_snippet
from ._base import (
    DaemonNotRunningError,
    ToolAnnotations,
    ToolArgs,
    ToolResult,
    _daemon_not_running_text,
    _text_response,
    daemon_connection,
    error_result,
    mcp_tool,
)
from .structured import navigation_metadata, structured_warning, telegram_content

TRACE_ACCOUNT_MESSAGES_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "resolved_account": {
            "type": "object",
            "properties": {
                "confidence": {"type": "string"},
                "account_id": {"type": ["integer", "null"]},
                "display_name": {"type": ["string", "null"]},
                "username": {"type": ["string", "null"]},
                "candidate_ids": {"type": "array", "items": {"type": "integer"}},
                "display_aliases": {"type": "array", "items": {"type": "string"}},
                "resolution_source": {"type": "string"},
            },
            "required": [
                "confidence",
                "account_id",
                "display_name",
                "username",
                "candidate_ids",
                "display_aliases",
                "resolution_source",
            ],
            "additionalProperties": True,
        },
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "group_key": {"type": "string"},
                    "group_label": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source": {"type": "string"},
                                "evidence_kind": {"type": "string"},
                                "dialog_id": {"type": "integer"},
                                "dialog_title": {"type": ["string", "null"]},
                                "dialog_type": {"type": ["string", "null"]},
                                "topic_id": {"type": ["integer", "null"]},
                                "topic_title": {"type": ["string", "null"]},
                                "message_id": {"type": "integer"},
                                "sent_at": {"type": "integer"},
                                "sender_id": {"type": ["integer", "null"]},
                                "effective_sender_id": {"type": ["integer", "null"]},
                                "authorship_basis": {
                                    "type": "string",
                                    "enum": ["effective_sender_id", "post_author_signature"],
                                },
                                "author_signature": {"type": ["string", "null"]},
                                "text": {"type": ["string", "null"]},
                                "media_description": {"type": ["string", "null"]},
                                "content": {"type": "object", "additionalProperties": True},
                                "media_content": {"type": "object", "additionalProperties": True},
                                "untrusted_content": {"type": "boolean"},
                            },
                            "required": [
                                "source",
                                "evidence_kind",
                                "dialog_id",
                                "message_id",
                                "sent_at",
                                "authorship_basis",
                            ],
                            "additionalProperties": True,
                        },
                    },
                },
                "required": ["group_key", "group_label", "evidence"],
                "additionalProperties": True,
            },
        },
        "coverage": {
            "type": "object",
            "properties": {
                "state": {"type": "string", "enum": ["complete", "partial", "unknown"]},
                "observed_message_count": {"type": "integer"},
                "dialogs_considered": {"type": "integer"},
                "dialogs_considered_basis": {"type": "string"},
                "dialogs_with_hits": {"type": "integer"},
                "dialogs_with_gaps": {"type": "integer"},
                "as_of": {"type": "integer"},
            },
            "required": [
                "state",
                "observed_message_count",
                "dialogs_considered",
                "dialogs_considered_basis",
                "dialogs_with_hits",
                "dialogs_with_gaps",
                "as_of",
            ],
            "additionalProperties": True,
        },
        "gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "severity": {"type": "string", "enum": ["info", "warning", "action_required"]},
                    "dialog_id": {"type": "integer"},
                    "topic_id": {"type": "integer"},
                    "detail": {"type": "string"},
                    "action": {"type": "object"},
                    "next_action": {"type": "object"},
                },
                "required": ["kind", "severity", "detail"],
                "additionalProperties": True,
            },
        },
        "provenance": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "query_basis": {"type": "string"},
                "coverage_goal": {"type": "string", "enum": ["observed", "best_effort_visible"]},
                "coverage_bounds": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer"},
                        "exact_dialog_id": {"type": ["integer", "null"]},
                        "exact_topic_id": {"type": ["integer", "null"]},
                        "sent_after": {"type": ["string", "null"]},
                        "sent_before": {"type": ["string", "null"]},
                    },
                    "additionalProperties": True,
                },
                "authorship_basis_counts": {"type": "object", "additionalProperties": {"type": "integer"}},
                "dialogs_considered_basis": {"type": "string"},
                "local_cache_writes": {"type": "integer"},
            },
            "required": ["source", "query_basis", "coverage_goal", "authorship_basis_counts"],
            "additionalProperties": True,
        },
        "next_navigation": {"type": ["string", "null"]},
        "navigation": {"type": "object", "additionalProperties": True},
        "text_preview": {"type": "object", "additionalProperties": True},
        "warnings": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "limits": {"type": "object", "additionalProperties": True},
        "result_count_semantics": {"type": "string"},
        "is_error_conditions": {"type": "string"},
    },
    "required": [
        "resolved_account",
        "groups",
        "coverage",
        "gaps",
        "provenance",
        "next_navigation",
        "navigation",
        "text_preview",
        "warnings",
        "limits",
        "result_count_semantics",
    ],
    "additionalProperties": True,
}


class TraceAccountMessages(ToolArgs):
    """
    Find observable authored-message evidence for one account across visible message history.

    Use account for a name, username, or profile link when the numeric id is unknown; use
    exact_account_id when it is already known. Scope with dialog/exact_dialog_id, and use
    exact_topic_id only together with a dialog scope. coverage_goal="observed" reports the
    current archive view; coverage_goal="best_effort_visible" permits bounded visible sampling
    with daemon-enforced dialog, message, and time limits. Gaps describe visibility or sync limits,
    not proof that no authored message exists.
    """

    account: str | None = Field(
        default=None,
        max_length=500,
        description="Name, username, or profile link for the account to trace.",
    )
    exact_account_id: int | None = Field(
        default=None,
        description="Known numeric account id. Prefer this when available to avoid ambiguity.",
    )
    group_by: Literal["timeline", "dialog"] = Field(
        default="timeline",
        description="Group evidence by timeline day or by dialog/topic.",
    )
    dialog: str | None = Field(
        default=None,
        max_length=500,
        description="Optional dialog selector for scoping by name, link, or numeric id.",
    )
    exact_dialog_id: int | None = Field(
        default=None,
        description="Optional numeric dialog id for exact dialog scoping.",
    )
    exact_topic_id: int | None = Field(
        default=None,
        description="Optional numeric topic id. Requires dialog or exact_dialog_id.",
    )
    sent_after: str | None = Field(
        default=None,
        description="Optional lower sent-time bound. ISO-8601 strings are accepted.",
    )
    sent_before: str | None = Field(
        default=None,
        description="Optional upper sent-time bound. ISO-8601 strings are accepted.",
    )
    limit: int = Field(default=50, ge=1, le=200)
    navigation: str | None = Field(
        default=None,
        max_length=2000,
        description="Opaque next_navigation token from a previous Account Trace response.",
    )
    coverage_goal: Literal["observed", "best_effort_visible"] = Field(
        default="observed",
        description=(
            "observed returns the current archive view; best_effort_visible permits bounded "
            "visible sampling without claiming completeness."
        ),
    )

    @model_validator(mode="after")
    def _validate_scope(self) -> "TraceAccountMessages":
        if self.exact_topic_id is not None and self.dialog is None and self.exact_dialog_id is None:
            raise ValueError("exact_topic_id requires dialog or exact_dialog_id")
        if self.account is not None and self.exact_account_id is not None:
            raise ValueError("account and exact_account_id are mutually exclusive")
        return self


def _trace_evidence_count(data: dict) -> int:
    count = 0
    for group in data.get("groups", []):
        if isinstance(group, dict):
            evidence = group.get("evidence", [])
            if isinstance(evidence, list):
                count += len(evidence)
    return count


def _trace_preview(data: dict, *, evidence_count: int) -> dict[str, object]:
    gaps = data.get("gaps", [])
    gap_summary = []
    if isinstance(gaps, list):
        for gap in gaps[:5]:
            if isinstance(gap, dict):
                gap_summary.append({"kind": gap.get("kind"), "severity": gap.get("severity")})
    shown_count = min(evidence_count, 5)
    return {
        "shown_count": shown_count,
        "hidden_count": max(evidence_count - shown_count, 0),
        "gap_summary": gap_summary,
    }


def _trace_warnings(data: dict) -> list[dict[str, object]]:
    warnings: list[dict[str, object]] = []
    gaps = data.get("gaps", [])
    if not isinstance(gaps, list):
        return warnings
    for gap in gaps:
        if not isinstance(gap, dict):
            continue
        severity = gap.get("severity")
        if severity in {"warning", "action_required"}:
            warnings.append(
                structured_warning(
                    str(gap.get("kind") or "coverage_gap"),
                    str(gap.get("detail") or "Account Trace reported a coverage gap."),
                    severity=severity,
                )
            )
    return warnings


def _trace_limits(data: dict, args: "TraceAccountMessages", *, evidence_count: int) -> dict[str, object]:
    provenance = data.get("provenance") if isinstance(data.get("provenance"), dict) else {}
    coverage_bounds = provenance.get("coverage_bounds") if isinstance(provenance, dict) else {}
    return {
        "requested_limit": args.limit,
        "returned_evidence_count": evidence_count,
        "coverage_bounds": coverage_bounds or {},
    }


def _attach_trace_content_metadata(data: dict) -> None:
    for group in data.get("groups", []):
        if not isinstance(group, dict):
            continue
        evidence_items = group.get("evidence", [])
        if not isinstance(evidence_items, list):
            continue
        for item in evidence_items:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if text:
                item.setdefault("content", telegram_content(str(text), "message_text"))
                item.setdefault("untrusted_content", True)
            media_description = item.get("media_description")
            if media_description:
                item.setdefault("media_content", telegram_content(str(media_description), "media_description"))
                item.setdefault("untrusted_content", True)


def _trace_structured_content(data: dict, args: "TraceAccountMessages") -> dict[str, object]:
    _attach_trace_content_metadata(data)
    evidence_count = _trace_evidence_count(data)
    next_navigation = data.get("next_navigation")
    data.setdefault("result_count_semantics", "current_page_evidence_items")
    data.setdefault(
        "is_error_conditions",
        "Only tool validation, daemon-unavailable, and daemon protocol failures set is_error=true.",
    )
    data.setdefault("text_preview", _trace_preview(data, evidence_count=evidence_count))
    data.setdefault("warnings", _trace_warnings(data))
    data.setdefault("limits", _trace_limits(data, args, evidence_count=evidence_count))
    data.setdefault("navigation", navigation_metadata(next_navigation if isinstance(next_navigation, str) else None))
    return data


def _trace_text_summary(data: dict) -> str:
    resolved = data.get("resolved_account", {})
    coverage = data.get("coverage", {})
    gaps = data.get("gaps", [])
    groups = data.get("groups", [])
    next_navigation = data.get("next_navigation")
    evidence_count = _trace_evidence_count(data)

    label = resolved.get("display_name") or resolved.get("username") or resolved.get("account_id") or "unresolved"
    lines = [
        f"Account Trace: {label}",
        f"evidence_items={evidence_count}",
        f"coverage_state={coverage.get('state', 'unknown')}",
        f"gaps={len(gaps) if isinstance(gaps, list) else 0}",
    ]

    shown = 0
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict):
            continue
        evidence = group.get("evidence", [])
        if not isinstance(evidence, list):
            continue
        for item in evidence:
            if not isinstance(item, dict):
                continue
            shown += 1
            dialog_label = item.get("dialog_title") or item.get("dialog_id")
            topic_label = f" / {item['topic_title']}" if item.get("topic_title") else ""
            text = str(item.get("text") or item.get("media_description") or "").replace("\n", " ").strip()
            snippet = frame_telegram_snippet(text[:180]) if text else "(no text)"
            lines.append(
                f"- {dialog_label}{topic_label} msg_id={item.get('message_id')} "
                f"basis={item.get('authorship_basis')}: {snippet}"
            )
            if shown >= 5:
                break
        if shown >= 5:
            break

    if evidence_count > shown:
        lines.append(f"... {evidence_count - shown} more evidence item(s) in structured_content")

    if isinstance(gaps, list) and gaps:
        gap_bits = []
        for gap in gaps[:5]:
            if not isinstance(gap, dict):
                continue
            gap_bits.append(f"{gap.get('kind')}:{gap.get('severity')}")
        lines.append("gap_summary=" + ", ".join(gap_bits))

    if next_navigation:
        lines.append(f"next_navigation: {next_navigation}")

    return "\n".join(lines)


@mcp_tool(
    name="trace_account_messages",
    title="Account Trace",
    annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=True),
    output_schema=TRACE_ACCOUNT_MESSAGES_OUTPUT_SCHEMA,
)
async def trace_account_messages(args: TraceAccountMessages) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.trace_account_messages(
                account=args.account,
                exact_account_id=args.exact_account_id,
                group_by=args.group_by,
                dialog=args.dialog,
                exact_dialog_id=args.exact_dialog_id,
                exact_topic_id=args.exact_topic_id,
                sent_after=args.sent_after,
                sent_before=args.sent_before,
                limit=args.limit,
                navigation=args.navigation,
                coverage_goal=args.coverage_goal,
            )
    except DaemonNotRunningError:
        return error_result(_daemon_not_running_text(), has_filter=True, has_cursor=args.navigation is not None)

    if not response.get("ok"):
        error = response.get("error", "unknown")
        error_detail = response.get("message", "Request failed.")
        return error_result(
            f"Error: {error}: {error_detail}",
            has_filter=True,
            has_cursor=args.navigation is not None,
        )

    data = _trace_structured_content(dict(response.get("data", {})), args)
    evidence_count = _trace_evidence_count(data)
    next_navigation = data.get("next_navigation")
    return ToolResult(
        content=_text_response(_trace_text_summary(data)),
        structured_content=data,
        result_count=evidence_count,
        has_filter=True,
        has_cursor=args.navigation is not None or bool(next_navigation),
    )
