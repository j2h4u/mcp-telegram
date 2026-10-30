"""The assessment distinguishes retained facts from account-wide acquisition."""

from typing import cast

import pytest
from jsonschema import validate

from mcp_telegram.sync_assessment import ASSESSMENT_SCHEMA, ASSESSMENT_TOOLS, build_sync_assessment
from mcp_telegram.sync_read_model import build_sync_read_model

HOLD = {
    "account_protection": {
        "status": "active",
        "outbound_acquisition": "blocked",
        "recovery": "automatic",
        "notice": "held",
    }
}


def _read(*, state: str = "present", interval: str = "covered_as_of_horizon") -> dict[str, object]:
    return {
        "messages": [{"text": "untrusted private text"}],
        "filters": {"message_state": "sent", "read_scope": "dialog"},
        "coverage": {"selection_state": state, "interval_coverage": interval},
    }


def test_local_result_remains_useful_under_hold() -> None:
    result = build_sync_assessment("list_messages", _read(), HOLD)
    validate(result, ASSESSMENT_SCHEMA)
    assert result["background_acquisition"] == "account_hold"
    assert "Retained local results were returned" in cast(str, result["summary"])
    assert "covered through the recorded history boundary" in cast(str, result["summary"])
    assert "private text" not in cast(str, result["summary"])


def test_event_after_horizon_uses_offset_aware_observation_times() -> None:
    result = build_sync_assessment(
        "get_sync_status",
        {
            "saved_message_count": 1,
            "history_sync_state": "complete_as_of_last_sync",
            "history_complete_at": "2026-01-01T10:00:00+02:00",
            "last_event_at": "2026-01-01T09:30:00+00:00",
            "coverage_state": "telegram_total_comparable",
            "delete_detection": "paused (DM; older deletions may be stale)",
        },
        {},
    )
    summary = cast(str, result["summary"])
    assert "event was observed after" in summary
    assert "continuous coverage" in summary
    assert "approximate" in summary and "paused" in summary
    assert result["background_acquisition"] == "no_account_hold_observed"
    read = build_sync_assessment("list_messages", _read(interval="beyond_horizon"), {})
    assert "Retained local results" in cast(str, read["summary"]) and "unverified" in cast(str, read["summary"])


def test_quiet_dialog_is_not_failure_and_absent_times_are_not_invented() -> None:
    result = build_sync_assessment(
        "get_sync_status", {"saved_message_count": 0, "history_sync_state": "not_synced"}, {}
    )
    assert "silence alone does not indicate a fault" in cast(str, result["summary"])
    assert "not necessarily empty" in cast(str, result["summary"])
    assert "healthy" not in cast(str, result["summary"])


def test_empty_fresh_selection_under_hold_remains_unknown() -> None:
    payload = _read(state="unknown", interval="beyond_horizon")
    payload["messages"] = []
    result = build_sync_assessment("list_messages", payload, HOLD)
    assert "No matching retained results" in cast(str, result["summary"])
    assert "unverified" in cast(str, result["summary"])
    assert "known empty" not in cast(str, result["summary"])
    assert result["background_acquisition"] == "account_hold"


@pytest.mark.parametrize(
    "snapshot",
    [
        {"account_protection": {"status": "unavailable"}},
        {"unexpected": 1},
        {"account_protection": {"status": "active"}},
    ],
)
def test_unavailable_or_malformed_snapshot_is_unknown(snapshot: dict[str, object]) -> None:
    assert build_sync_assessment("list_messages", _read(), snapshot)["background_acquisition"] == "unknown"


@pytest.mark.parametrize(
    "scope", [{"global": True, "message_state": "sent"}, {"global": False, "message_state": "all"}, {}]
)
def test_global_and_mixed_results_do_not_borrow_one_history_horizon(scope: dict[str, object]) -> None:
    result = build_sync_assessment(
        "search_messages",
        {"results": [], "scope": scope, "coverage": {"interval_coverage": "covered_as_of_horizon"}},
        {},
    )
    assert "Aggregate coverage is unverified" in cast(str, result["summary"])
    assert "requested interval is covered" not in cast(str, result["summary"])


def test_known_empty_and_malformed_timestamp() -> None:
    payload = _read(state="known_empty")
    payload["messages"] = []
    assert "known empty" in cast(str, build_sync_assessment("list_messages", payload, {})["summary"])
    status = build_sync_assessment(
        "get_sync_status", {"last_event_at": "bad", "history_complete_at": "2026-01-01T00:00:00Z"}, {}
    )
    assert "event was observed after" not in cast(str, status["summary"])


def test_covered_interval_does_not_prove_unknown_selection() -> None:
    payload = _read(state="unknown")
    payload["messages"] = []
    summary = cast(str, build_sync_assessment("list_messages", payload, {})["summary"])
    assert "selection completeness remains unknown" in summary


@pytest.mark.parametrize(
    "coverage_state", ["telegram_total_unknown", "telegram_total_invalid", "telegram_total_not_comparable"]
)
def test_suspect_counts_do_not_imply_missing_messages(coverage_state: str) -> None:
    result = build_sync_assessment("get_sync_status", {"coverage_state": coverage_state}, {})
    summary = cast(str, result["summary"])
    assert "cannot establish a comparable completeness count" in summary
    assert "missing messages" not in summary


def test_equal_instants_with_different_offsets_are_not_later() -> None:
    result = build_sync_assessment(
        "get_sync_status",
        {"history_complete_at": "2026-01-01T10:00:00+02:00", "last_event_at": "2026-01-01T08:00:00+00:00"},
        {},
    )
    assert "event was observed after" not in cast(str, result["summary"])


def test_canonical_complete_state_without_timestamp_has_unknown_boundary() -> None:
    model = build_sync_read_model(
        persisted_status="synced",
        enrollment_enabled=True,
        last_synced_at=None,
        last_event_at=None,
        last_delta_checked_at=None,
        saved_message_count=1,
        total_messages=None,
        now=100,
    )
    payload = {key: value for key, value in model.to_wire().items() if value is not None}
    assert "history_complete_at" not in payload
    summary = cast(str, build_sync_assessment("get_sync_status", payload, {})["summary"])
    assert "Completed history acquisition is recorded" in summary
    assert "boundary is unknown" in summary
    assert "through the reported boundary" not in summary


def test_held_unfinished_history_and_normal_reconciliation_wording() -> None:
    payload = {
        "history_sync_state": "syncing",
        "last_delta_checked_at": "2026-01-01T08:00:00+00:00",
        "delete_detection": "reliable (channel)",
    }
    summary = cast(str, build_sync_assessment("get_sync_status", payload, HOLD)["summary"])
    assert "History synchronization is unfinished" in summary
    assert "A reconciliation was recorded." in summary
    assert "in progress" not in summary
    assert "Deletion detection policy" not in summary
    assert "continuous catch-up" not in summary


def test_only_three_supported_tools() -> None:
    assert {"get_sync_status", "list_messages", "search_messages"} == ASSESSMENT_TOOLS
    with pytest.raises(ValueError):
        build_sync_assessment("get_inbox", {}, {})
