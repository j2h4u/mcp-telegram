"""Explain local read evidence without acquiring new Telegram data."""

from collections.abc import Mapping
from datetime import datetime
from typing import Literal, TypedDict

ASSESSMENT_TOOLS = frozenset({"get_sync_status", "list_messages", "search_messages"})
ASSESSMENT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "local_read": {"const": "completed_without_telegram"},
        "background_acquisition": {"enum": ["account_hold", "no_account_hold_observed", "unknown"]},
        "summary": {"type": "string"},
    },
    "required": ["local_read", "background_acquisition", "summary"],
    "additionalProperties": False,
}


class _Assessment(TypedDict):
    local_read: Literal["completed_without_telegram"]
    background_acquisition: Literal["account_hold", "no_account_hold_observed", "unknown"]
    summary: str


def _acquisition(snapshot: Mapping[str, object]) -> Literal["account_hold", "no_account_hold_observed", "unknown"]:
    if not snapshot:
        return "no_account_hold_observed"
    protection = snapshot.get("account_protection")
    if (
        isinstance(protection, Mapping)
        and protection.get("status") == "active"
        and protection.get("outbound_acquisition") == "blocked"
        and protection.get("recovery") in ("manual", "automatic")
        and isinstance(protection.get("notice"), str)
    ):
        return "account_hold"
    return "unknown"


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return timestamp if timestamp.utcoffset() is not None else None


def _event_after_boundary(payload: Mapping[str, object]) -> bool:
    event = _timestamp(payload.get("last_event_at"))
    boundary = _timestamp(payload.get("history_complete_at"))
    return event is not None and boundary is not None and event > boundary


def _history_summary(payload: Mapping[str, object]) -> str:
    state = payload.get("history_sync_state")
    if not isinstance(state, str):
        return "History coverage is unknown."
    if state == "complete_as_of_last_sync":
        if _timestamp(payload.get("history_complete_at")) is None:
            return "Completed history acquisition is recorded; its current boundary is unknown."
        return "Completed history acquisition is recorded through the reported boundary."
    return {
        "syncing": "History synchronization is unfinished; complete history is not established.",
        "own_messages_only": "History coverage is limited to own messages.",
        "fragment_only": "Only fragmentary history coverage is recorded.",
        "access_lost_archive": "This is a retained archive with lost access.",
        "not_synced": "Completed history acquisition is not recorded.",
    }.get(state, "History coverage is unknown.")


def _status_summary(payload: Mapping[str, object]) -> list[str]:
    count = payload.get("saved_message_count")
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        parts = ["Retained local messages are available."]
    elif count == 0 and not isinstance(count, bool):
        parts = ["No retained local message rows were counted; Telegram history is not necessarily empty."]
    else:
        parts = ["The local status read completed; the retained message count is unknown."]
    parts.append(_history_summary(payload))
    if payload.get("last_delta_checked_at") is not None:
        parts.append("A reconciliation was recorded.")
    if payload.get("delta_refresh_requested_at") is not None:
        parts.append("A reconciliation is requested; completion is not established by the request.")
    if _event_after_boundary(payload):
        parts.append("An event was observed after the history boundary; it does not establish continuous coverage.")
    elif payload.get("last_event_at") is None:
        parts.append("No observed event is recorded; silence alone does not indicate a fault.")
    return parts


def _status_limits(payload: Mapping[str, object]) -> list[str]:
    parts: list[str] = []
    deletion = payload.get("delete_detection")
    if isinstance(deletion, str) and deletion.startswith("paused"):
        parts.append("Deletion verification is paused; older deletions may be stale.")
    elif isinstance(deletion, str) and deletion.startswith("best-effort"):
        parts.append("Deletion verification is best effort, not proof of complete deletion detection.")
    coverage = payload.get("coverage_state")
    if coverage == "telegram_total_comparable":
        parts.append("The local-to-Telegram count comparison is approximate, not a completeness guarantee.")
    elif coverage in ("telegram_total_unknown", "telegram_total_invalid", "telegram_total_not_comparable"):
        parts.append("The stored Telegram total cannot establish a comparable completeness count.")
    return parts


def _read_coverage(payload: Mapping[str, object]) -> str:
    coverage = payload.get("coverage")
    if not isinstance(coverage, Mapping):
        return "The selection's coverage is unknown."
    if coverage.get("selection_state") == "known_empty":
        return "The bounded selection is known empty as of the recorded history boundary."
    interval = coverage.get("interval_coverage")
    if interval == "covered_as_of_horizon":
        if coverage.get("selection_state") != "present":
            return "The interval is covered through the history boundary; selection completeness remains unknown."
        return "The requested interval is covered through the recorded history boundary."
    if interval == "beyond_horizon":
        return "The requested interval extends beyond the recorded history boundary and is unverified there."
    return "The selection's interval coverage is unknown."


def _read_summary(tool: str, payload: Mapping[str, object]) -> list[str]:
    collection = payload.get("messages" if tool == "list_messages" else "results")
    if isinstance(collection, list) and collection:
        parts = ["Retained local results were returned."]
    elif isinstance(collection, list):
        parts = ["No matching retained results were returned on this page."]
    else:
        parts = ["The local read completed."]
    scope = payload.get("filters" if tool == "list_messages" else "scope")
    if not isinstance(scope, Mapping):
        return [*parts, "The selection's aggregate coverage is unknown."]
    scoped = scope.get("read_scope") == "dialog" if tool == "list_messages" else scope.get("global") is False
    if scope.get("message_state") != "sent" or not scoped:
        return [*parts, "Aggregate coverage is unverified; consult the separate scope and state coverage."]
    parts.append(_read_coverage(payload))
    return parts


def build_sync_assessment(
    tool_name: str, payload: Mapping[str, object], protection_snapshot: Mapping[str, object]
) -> dict[str, object]:
    """Project the already-rendered successful result and checked protection snapshot."""
    if tool_name not in ASSESSMENT_TOOLS:
        raise ValueError("unsupported assessment tool")
    acquisition = _acquisition(protection_snapshot)
    parts = (
        _status_summary(payload) + _status_limits(payload)
        if tool_name == "get_sync_status"
        else _read_summary(tool_name, payload)
    )
    parts.append(
        {
            "account_hold": "Background Telegram acquisition is held; retained local reads remain available.",
            "no_account_hold_observed": "No account acquisition hold was observed.",
            "unknown": "Account acquisition protection status is unknown.",
        }[acquisition]
    )
    assessment: _Assessment = {
        "local_read": "completed_without_telegram",
        "background_acquisition": acquisition,
        "summary": " ".join(parts),
    }
    return dict(assessment)
