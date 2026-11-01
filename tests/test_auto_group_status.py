import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest

from mcp_telegram.daemon_api import _automatic_group_type
from mcp_telegram.dialog_identity import read_automatic_group_metadata
from mcp_telegram.sync_read_model import AutomaticGroupFacts, SyncReadModelContractError, automatic_group_status
from mcp_telegram.tools.sync import _decode_automatic_group_status


@pytest.mark.parametrize(
    ("enabled", "eligibility", "reason"),
    [
        (None, "eligible_for_probe", "eligible"),
        (False, "blocked", "disabled"),
        (True, "blocked", "already_enabled"),
    ],
)
def test_automatic_group_status_interprets_existing_intent(enabled: bool | None, eligibility: str, reason: str) -> None:
    facts = AutomaticGroupFacts(enabled, True, 12, 1000, False, "own_only", None)
    result = automatic_group_status(facts, now=1000, max_members=100, recent_days=365)
    assert result == {
        "eligibility": eligibility,
        "reason": reason,
        "fresh_telegram_total": "unknown",
    }


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"members": None}, "group_metadata_unknown"),
        ({"created": None}, "group_metadata_unknown"),
        ({"members": 101}, "member_limit"),
        ({"created": 1}, "outside_recent_window"),
        ({"hidden": True}, "hidden"),
        ({"coverage_status": "access_lost"}, "access_lost"),
        ({"coverage_status": "synced"}, "already_synced"),
        ({"is_group": False}, "not_a_group"),
        ({"retry_at": 40_000_001}, "retry_wait"),
    ],
)
def test_automatic_group_status_reports_local_blocker(changes: dict, reason: str) -> None:
    facts = AutomaticGroupFacts(None, True, 12, 40_000_000, False, "own_only", None)
    result = automatic_group_status(replace(facts, **changes), now=40_000_000, max_members=100, recent_days=365)
    assert result["eligibility"] == ("unknown" if reason == "group_metadata_unknown" else "blocked")
    assert result["reason"] == reason
    assert result["fresh_telegram_total"] == "unknown"
    if reason == "retry_wait":
        assert result["retry_at"] == 40_000_001


@pytest.mark.parametrize("field", ["eligibility", "reason"])
def test_malformed_group_status_fails_closed(field: str) -> None:
    value: dict[str, object] = {
        "eligibility": "blocked",
        "reason": "already_enabled",
        "fresh_telegram_total": "unknown",
    }
    value[field] = []
    with pytest.raises(SyncReadModelContractError):
        _decode_automatic_group_status(value)


def test_disabled_decision_does_not_report_obsolete_retry() -> None:
    facts = AutomaticGroupFacts(False, True, 12, 1000, False, "own_only", 2000)
    result = automatic_group_status(facts, now=1000, max_members=100, recent_days=365)
    assert result["reason"] == "disabled" and "retry_at" not in result


def test_group_metadata_uses_raw_directory_facts_and_preserves_absence() -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute(
            "CREATE TABLE dialogs(dialog_id INTEGER PRIMARY KEY,type TEXT,members INTEGER,created INTEGER,hidden INTEGER)"
        )
        conn.execute("CREATE TABLE entities(id INTEGER PRIMARY KEY,type TEXT)")
        conn.execute("INSERT INTO dialogs VALUES (42,NULL,12,1000,0)")
        conn.execute("INSERT INTO entities VALUES (42,'group')")
        assert read_automatic_group_metadata(conn, 42) == (None, 12, 1000, 0)
        assert read_automatic_group_metadata(conn, 99) is None


@pytest.mark.parametrize("value", [None, "unknown", "unrecognized"])
def test_unknown_group_type_stays_unknown(value: object) -> None:
    assert _automatic_group_type(value) is None
