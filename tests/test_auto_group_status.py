import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest

from mcp_telegram.dialog_identity import read_automatic_group_metadata
from mcp_telegram.sync_read_model import AutomaticGroupFacts, SyncReadModelContractError, automatic_group_status
from mcp_telegram.tools.sync import _decode_automatic_group_status


@pytest.mark.parametrize(
    ("enabled", "source", "interpretation", "eligibility", "reason"),
    [
        (False, "migration", "migration_unknown", "eligible_for_probe", "eligible"),
        (None, None, "undecided", "eligible_for_probe", "eligible"),
        (False, "explicit", "explicit_disabled", "blocked", "enrollment_decided"),
        (False, "automatic", "automatic_rejected", "blocked", "enrollment_decided"),
        (True, "explicit", "enabled", "blocked", "already_enabled"),
    ],
)
def test_automatic_group_status_interprets_existing_intent(
    enabled: bool | None, source: str | None, interpretation: str, eligibility: str, reason: str
) -> None:
    facts = AutomaticGroupFacts(
        enabled, source, source is None or source == "migration", "group", 12, 1000, False, "own_only", None
    )
    result = automatic_group_status(facts, now=1000, max_members=100, recent_days=365)
    assert result == {
        "enrollment_interpretation": interpretation,
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
        ({"dialog_type": "user"}, "not_a_group"),
        ({"retry_at": 40_000_001}, "retry_wait"),
    ],
)
def test_automatic_group_status_reports_local_blocker(changes: dict, reason: str) -> None:
    facts = AutomaticGroupFacts(False, "migration", True, "group", 12, 40_000_000, False, "own_only", None)
    result = automatic_group_status(replace(facts, **changes), now=40_000_000, max_members=100, recent_days=365)
    assert result["eligibility"] == ("unknown" if reason == "group_metadata_unknown" else "blocked")
    assert result["reason"] == reason
    assert result["fresh_telegram_total"] == "unknown"
    if reason == "retry_wait":
        assert result["retry_at"] == 40_000_001


@pytest.mark.parametrize("field", ["eligibility", "enrollment_interpretation"])
def test_malformed_group_status_fails_closed(field: str) -> None:
    value: dict[str, object] = {
        "eligibility": "blocked",
        "enrollment_interpretation": "enabled",
        "reason": "already_enabled",
        "fresh_telegram_total": "unknown",
    }
    value[field] = []
    with pytest.raises(SyncReadModelContractError):
        _decode_automatic_group_status(value)


def test_explicit_decision_does_not_report_obsolete_retry() -> None:
    facts = AutomaticGroupFacts(False, "explicit", False, "group", 12, 1000, False, "own_only", 2000)
    result = automatic_group_status(facts, now=1000, max_members=100, recent_days=365)
    assert result["enrollment_interpretation"] == "explicit_disabled"
    assert result["reason"] == "enrollment_decided" and "retry_at" not in result


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
