import pytest
from jsonschema import validate

from mcp_telegram.server import _bounded_account_protection, _is_valid_active_account_protection
from mcp_telegram.tools._base import ACTIVE_ACCOUNT_PROTECTION_SCHEMA
from mcp_telegram.tools.reading import LIST_MESSAGES_OUTPUT_SCHEMA, _list_messages_coverage
from tests.test_daemon_api import _insert_synced_dialog
from tests.test_scheduled_read_surface import _create_draft_projection, _create_scheduled_table, _make_scheduled_server


def test_protection_schema_preserves_unbounded_transient_attempts_and_scheduled_trial() -> None:
    protection = {
        "status": "active",
        "outbound_acquisition": "blocked",
        "recovery": "automatic",
        "notice": "Local reads remain available.",
        "automatic_resume": {
            "enabled": True,
            "eligible": True,
            "phase": "waiting",
            "cause": "transient",
            "state": "waiting",
            "attempts": 0,
            "max_attempts": 6,
            "attempts_total": 9,
            "attempts_by_policy": {"flood_wait": 0, "transient": 9, "read_uncertainty": 0},
            "next_trial_at_utc": 1000.0,
            "next_attempt_at_utc": 1000.0,
            "known_restriction_until_utc": None,
        },
    }
    assert _is_valid_active_account_protection(protection)
    bounded = _bounded_account_protection(protection)
    validate(bounded, ACTIVE_ACCOUNT_PROTECTION_SCHEMA)
    automatic = bounded["automatic_resume"]
    assert isinstance(automatic, dict)
    assert automatic["attempts_total"] == 9
    assert "known_restriction_until_utc" not in automatic


def test_interval_coverage_survives_mcp_projection_with_retained_rows() -> None:
    coverage = _list_messages_coverage(
        {"coverage": "complete_as_of", "selection_state": "present", "interval_coverage": "beyond_horizon"}
    )
    schema = LIST_MESSAGES_OUTPUT_SCHEMA["properties"]["coverage"]
    validate(coverage, schema)
    assert coverage["selection_state"] == "present"
    assert coverage["interval_coverage"] == "beyond_horizon"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scheduled_state", "draft_state", "expected"),
    [("complete", "absent", "known_empty"), ("unknown", "absent", "unknown"), ("complete", "unknown", "unknown")],
)
async def test_combined_empty_requires_each_projection(
    monkeypatch: pytest.MonkeyPatch, scheduled_state: str, draft_state: str, expected: str
) -> None:
    server = _make_scheduled_server()
    conn = server._conn
    _insert_synced_dialog(conn, 42, status="synced", last_synced_at=100)
    _create_scheduled_table(conn)
    _create_draft_projection(conn)
    conn.execute(
        "INSERT INTO scheduled_reconciliation_state(dialog_id,coverage_state,baseline_observed_at) VALUES (42,?,50)",
        (scheduled_state,),
    )
    conn.execute("INSERT INTO draft_sync_state VALUES (7, 'ready', 'complete', 50, 50, NULL)")
    if draft_state == "absent":
        conn.execute(
            "INSERT INTO draft_current VALUES (7,42,0,0,'empty',NULL,NULL,NULL,NULL,NULL,NULL,NULL,"
            "NULL,NULL,1,'snapshot_empty',50,50,50,1,1)"
        )
    conn.commit()
    service = server._get_reading_service()
    monkeypatch.setattr(service, "_scheduled_account_coverage", lambda *_args: (7, {"state": "partial"}))

    result = await server._list_messages({"dialog_id": 42, "message_state": "all", "until_utc": "1970-01-01T00:01:40Z"})
    assert result["ok"] is True
    assert result["data"]["messages"] == []
    assert result["data"]["selection_state"] == expected
    assert result["data"]["interval_coverage"] == "covered_as_of_horizon"
