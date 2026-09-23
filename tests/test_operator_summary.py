"""Operator summary CLI and report tests."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mcp_telegram import app
from mcp_telegram.operator_summary import (
    _slow_call_detail,
    _timing_attribution,
    _timing_completeness,
    _timing_contributor,
    build_operator_summary,
    parse_since,
)

runner = CliRunner()


def _database(path: Path, *, now_ms: int) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE runtime_observations (
            id INTEGER PRIMARY KEY, observed_at_ms INTEGER NOT NULL, kind TEXT NOT NULL,
            runtime_instance_id TEXT NOT NULL, operation_id TEXT, outcome TEXT,
            reason_code TEXT, dialog_id INTEGER, duration_ms REAL, tool_name TEXT,
            tool_capability TEXT, contract_version INTEGER, result_count INTEGER,
            has_cursor INTEGER, page_depth INTEGER, has_filter INTEGER, error_type TEXT,
            source_namespace TEXT, source_event_id INTEGER, payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE daemon_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE synced_dialogs (dialog_id INTEGER PRIMARY KEY, status TEXT NOT NULL);
        """
    )
    conn.execute(
        "INSERT INTO daemon_state(key,value) VALUES ('runtime_observations_history_started_at_ms',?)",
        (str(now_ms - 20 * 3600 * 1000),),
    )
    rows = [
        (now_ms - 10_000, "runtime.started", "current", "observed", None, None, None, None, "{}"),
        (now_ms - 8_000, "mcp.call", "current", "success", None, None, 25.0, "list_dialogs", "{}"),
        (now_ms - 7_000, "mcp.call", "current", "tool_error", "tool_error", None, 1500.0, "get_entity_info", "{}"),
        (
            now_ms - 6_000,
            "telegram.rpc_admission",
            "current",
            "summary",
            None,
            None,
            9000.0,
            None,
            json.dumps(
                {
                    "source": "scheduled_messages",
                    "dispatched_count": 12,
                    "max_wait_ms": 9000,
                    "queue_depth_max": 2,
                }
            ),
        ),
        (now_ms - 5_000, "sync.read_reconciliation", "current", "applied", None, None, None, None, "{}"),
    ]
    conn.executemany(
        """INSERT INTO runtime_observations(
            observed_at_ms,kind,runtime_instance_id,outcome,reason_code,error_type,
            duration_ms,tool_name,payload_json
        ) VALUES (?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.executemany(
        "INSERT INTO synced_dialogs(dialog_id,status) VALUES (?,?)",
        [(1, "synced"), (2, "synced"), (3, "access_lost")],
    )
    conn.commit()
    conn.close()


@pytest.mark.parametrize(("value", "seconds"), [("30m", 1800), ("15h", 54000), ("24h", 86400), ("2d", 172800)])
def test_parse_since(value: str, seconds: int) -> None:
    assert parse_since(value) == seconds


def test_parse_since_rejects_invalid_value() -> None:
    with pytest.raises(ValueError, match="positive duration"):
        parse_since("0h")


def test_summary_is_one_coherent_content_free_report(tmp_path: Path) -> None:
    now = 2_000_000_000.0
    db_path = tmp_path / "sync.db"
    _database(db_path, now_ms=int(now * 1000))

    report = build_operator_summary(db_path, since_seconds=15 * 3600, now=now)

    assert report.window_complete is True
    assert "Runtime: starts=1, clean stops=0, task failures=0" in report.text
    assert "Dialog state: access_lost=1, synced=2" in report.text
    assert "MCP: calls=2, errors=1 (tool_error=1)" in report.text
    assert "Slow MCP calls: get_entity_info=1.5s, attribution=unavailable" in report.text
    assert "scheduled_messages: dispatched=12, worst wait=9.0s, max queue=2" in report.text
    assert "sync.read_reconciliation applied=1" in report.text
    assert "get_entity_info: tool_error" in report.text


def test_summary_reconciles_get_full_channel_attempts_by_source_and_demand(tmp_path: Path) -> None:
    now = 2_000_000_000.0
    db_path = tmp_path / "sync.db"
    _database(db_path, now_ms=int(now * 1000))
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executemany(
            "INSERT INTO runtime_observations("
            "observed_at_ms,kind,runtime_instance_id,outcome,payload_json"
            ") VALUES (?,?,?,?,?)",
            [
                (
                    int(now * 1000) - 4_000,
                    "telegram.rpc_request",
                    "current",
                    "summary",
                    json.dumps(
                        {
                            "request_class": "get_full_channel",
                            "source": "dialog_resolution",
                            "demand_kind": "entity_lookup",
                            "acquisition_kind": "entity_lookup",
                            "actual_attempts": 2,
                        }
                    ),
                ),
                (
                    int(now * 1000) - 3_000,
                    "telegram.rpc_request",
                    "current",
                    "summary",
                    json.dumps(
                        {
                            "request_class": "get_full_channel",
                            "source": "realtime_event",
                            "demand_kind": "realtime_event_acquisition",
                            "actual_attempts": 1,
                        }
                    ),
                ),
                (
                    int(now * 1000) - 2_000,
                    "telegram.rpc_request",
                    "current",
                    "summary",
                    json.dumps(
                        {
                            "request_class": "get_full_user",
                            "source": "dialog_resolution",
                            "demand_kind": "entity_lookup",
                            "actual_attempts": 100,
                        }
                    ),
                ),
            ],
        )
        conn.commit()

    report = build_operator_summary(db_path, since_seconds=15 * 3600, now=now)

    assert (
        "GetFullChannel RPC attempts: 3 "
        "(dialog_resolution/entity_lookup/entity_lookup=2, "
        "realtime_event/realtime_event_acquisition=1)" in report.text
    )


def test_summary_reports_final_demand_outcomes_and_freshness_reason(tmp_path: Path) -> None:
    now = 2_000_000_000.0
    db_path = tmp_path / "sync.db"
    _database(db_path, now_ms=int(now * 1000))
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO runtime_observations("
            "observed_at_ms,kind,runtime_instance_id,outcome,reason_code,payload_json"
            ") VALUES (?,?,?,?,?,?)",
            (
                int(now * 1000) - 4_000,
                "telegram.demand",
                "current",
                "selected",
                None,
                json.dumps(
                    {
                        "demand_kind": "scheduled_repair",
                        "demand_units": 1,
                        "actual_attempts": 0,
                        "queue_age_seconds": 8.0,
                        "freshness_debt_seconds": 3.5,
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO runtime_observations("
            "observed_at_ms,kind,runtime_instance_id,outcome,reason_code,payload_json"
            ") VALUES (?,?,?,?,?,?)",
            (
                int(now * 1000) - 3_000,
                "telegram.demand",
                "current",
                "deferred",
                "capacity",
                json.dumps(
                    {
                        "demand_kind": "scheduled_repair",
                        "demand_units": 1,
                        "actual_attempts": 1,
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO runtime_observations("
            "observed_at_ms,kind,runtime_instance_id,outcome,reason_code,payload_json"
            ") VALUES (?,?,?,?,?,?)",
            (
                int(now * 1000) - 2_000,
                "telegram.demand",
                "current",
                "completed",
                None,
                json.dumps(
                    {
                        "demand_kind": "scheduled_discovery",
                        "demand_units": 1,
                        "actual_attempts": 2,
                    }
                ),
            ),
        )
        conn.commit()

    report = build_operator_summary(db_path, since_seconds=15 * 3600, now=now)

    assert (
        "Demand: selected=1, completed=1, deferred=1, actual attempts=3, oldest queue age=8.0s, "
        "max freshness debt=3.5s, reasons=capacity=1" in report.text
    )
    assert "scheduled_repair: selected=1, deferred=1" in report.text


def test_summary_attributes_nested_rpc_leaf_and_only_computable_remainder(tmp_path: Path) -> None:
    now = 2_000_000_000.0
    db_path = tmp_path / "sync.db"
    _database(db_path, now_ms=int(now * 1000))
    payload = {
        "version": 1,
        "phase_model": "top_level_with_nested_rpc",
        "route_attempted": "telegram_context_fallback",
        "served_source": "local",
        "request_id": "11111111",
        "resolution_ms": 10.0,
        "local_projection_ms": 20.0,
        "telegram_fallback_ms": 700.0,
        "rpc_admission_ms": 100.0,
        "rpc_execution_ms": 500.0,
        "response_shape_ms": 20.0,
        "nested_phases": {"telegram_fallback": {"rpc_admission_ms": 100.0, "rpc_execution_ms": 500.0}},
        "measured_required_phase_count": 4,
        "required_phase_count": 4,
        "unattributed_ms": 250.0,
    }
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO runtime_observations("
            "observed_at_ms,kind,runtime_instance_id,operation_id,outcome,duration_ms,tool_name,payload_json"
            ") VALUES (?,?,?,?,?,?,?,?)",
            (
                int(now * 1000) - 1_000,
                "mcp.call",
                "current",
                "op-summary",
                "success",
                1_000.0,
                "list_messages",
                "{}",
            ),
        )
        conn.execute(
            "INSERT INTO runtime_observations("
            "observed_at_ms,kind,runtime_instance_id,operation_id,outcome,duration_ms,payload_json"
            ") VALUES (?,?,?,?,?,?,?)",
            (
                int(now * 1000) - 900,
                "daemon.request_timing",
                "current",
                "op-summary",
                "success",
                1_000.0,
                json.dumps(payload),
            ),
        )
        second_payload = {**payload, "request_id": "22222222", "resolution_ms": 900.0, "response_shape_ms": 10.0}
        conn.execute(
            "INSERT INTO runtime_observations("
            "observed_at_ms,kind,runtime_instance_id,operation_id,outcome,duration_ms,payload_json"
            ") VALUES (?,?,?,?,?,?,?)",
            (
                int(now * 1000) - 850,
                "daemon.request_timing",
                "current",
                "op-summary",
                "success",
                1_200.0,
                json.dumps(second_payload),
            ),
        )
        for offset, malformed_duration in enumerate((None, "invalid"), start=1):
            conn.execute(
                "INSERT INTO runtime_observations("
                "observed_at_ms,kind,runtime_instance_id,operation_id,outcome,duration_ms,payload_json"
                ") VALUES (?,?,?,?,?,?,?)",
                (
                    int(now * 1000) - 800 + offset,
                    "daemon.request_timing",
                    "current",
                    "op-summary",
                    "success",
                    malformed_duration,
                    json.dumps(payload),
                ),
            )
        conn.commit()

    report = build_operator_summary(db_path, since_seconds=15 * 3600, now=now)

    assert "contributor=resolution_exclusive=900ms" in report.text
    assert "attribution=complete, completeness=4/4, unattributed=250ms" in report.text
    assert "request_id=22222222" in report.text
    assert "timing_rows=2/4, discarded=2, aggregation=largest_request" in report.text


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "resolution_ms": 100.0,
                "nested_phases": {"resolution": {"rpc_admission_ms": 10.0, "rpc_execution_ms": 20.0}},
            },
            "resolution_exclusive=70ms",
        ),
        (
            {
                "telegram_fallback_ms": 1_000.0,
                "nested_phases": {"telegram_fallback": {"rpc_admission_ms": 100.0, "rpc_execution_ms": 200.0}},
            },
            "telegram_fallback_exclusive=700ms",
        ),
        (
            {
                "telegram_fallback_ms": 100.0,
                "nested_phases": {"telegram_fallback": {"rpc_execution_ms": 70.0}},
                "rpc_execution_ms": 80.0,
            },
            "rpc_execution=80ms",
        ),
        ({"local_projection_ms": 10.0, "response_shape_ms": 500.0}, "response_shape_exclusive=500ms"),
        ({"rpc_execution_ms": 80.0}, "rpc_execution=80ms"),
    ],
)
def test_summary_uses_exclusive_parent_remainder_for_nested_rpc(payload: dict[str, object], expected: str) -> None:
    assert _timing_contributor(payload) == expected


def test_summary_completeness_prefers_route_required_phase_counts() -> None:
    assert _timing_attribution({"measured_required_phase_count": 3, "required_phase_count": 3}) == "complete"
    assert _timing_attribution({"measured_required_phase_count": 2, "required_phase_count": 3}) == "partial"
    assert _timing_attribution({"measured_required_phase_count": 0, "required_phase_count": 3}) == "unavailable"
    assert _timing_attribution({"attribution": "complete"}) == "unavailable"
    assert _timing_completeness({"measured_required_phase_count": 3, "required_phase_count": 3}) == "3/3"
    assert _timing_completeness({"measured_required_phase_count": 2, "required_phase_count": 3}) == "2/3"
    assert _timing_completeness({"measured_required_phase_count": 0, "required_phase_count": 3}) == "0/3"


def test_summary_accepts_local_route_remainder_when_fallback_is_not_applicable() -> None:
    payload = {
        "route_attempted": "local_history",
        "resolution_ms": 10.0,
        "local_projection_ms": 20.0,
        "telegram_fallback_ms": None,
        "response_shape_ms": 5.0,
        "measured_required_phase_count": 3,
        "required_phase_count": 3,
        "not_applicable_phases": ["telegram_fallback"],
        "unattributed_ms": 15.0,
    }
    assert _timing_attribution(payload) == "complete"
    assert _timing_completeness(payload) == "3/3"
    assert payload["unattributed_ms"] == 15.0


def test_summary_treats_malformed_or_non_object_timing_payload_as_unavailable(tmp_path: Path) -> None:
    now = 2_000_000_000.0
    db_path = tmp_path / "sync.db"
    _database(db_path, now_ms=int(now * 1000))
    with closing(sqlite3.connect(db_path)) as conn:
        for index, payload_json in enumerate(("{", "null", "[]"), start=1):
            operation_id = f"{index:032x}"
            timestamp = int(now * 1000) - index * 100
            conn.execute(
                "INSERT INTO runtime_observations("
                "observed_at_ms,kind,runtime_instance_id,operation_id,outcome,duration_ms,tool_name,payload_json"
                ") VALUES (?,?,?,?,?,?,?,?)",
                (timestamp, "mcp.call", "current", operation_id, "success", 1_000.0, "list_messages", "{}"),
            )
            conn.execute(
                "INSERT INTO runtime_observations("
                "observed_at_ms,kind,runtime_instance_id,operation_id,outcome,duration_ms,payload_json"
                ") VALUES (?,?,?,?,?,?,?)",
                (
                    timestamp + 1,
                    "daemon.request_timing",
                    "current",
                    operation_id,
                    "success",
                    1_000.0,
                    payload_json,
                ),
            )
        conn.commit()

    report = build_operator_summary(db_path, since_seconds=15 * 3600, now=now)

    assert report.text.count("list_messages=1.0s, attribution=unavailable") == 3


def test_summary_distinguishes_recorded_all_invalid_timing_rows_from_absence() -> None:
    row = {
        "tool_name": "list_messages",
        "duration_ms": 1_000.0,
        "_timing_total_count": 2,
        "_timing_valid_count": 0,
        "_timing_invalid_count": 2,
    }

    assert _slow_call_detail(row) == (
        "list_messages=1.0s, attribution=unavailable, timing_rows=0/2, discarded=2, aggregation=largest_request"
    )


def test_summary_omits_overdue_debt_when_demand_has_no_freshness_deadline(tmp_path: Path) -> None:
    now = 2_000_000_000.0
    db_path = tmp_path / "sync.db"
    _database(db_path, now_ms=int(now * 1000))
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO runtime_observations("
            "observed_at_ms,kind,runtime_instance_id,outcome,payload_json"
            ") VALUES (?,?,?,?,?)",
            (
                int(now * 1000) - 1_000,
                "telegram.demand",
                "current",
                "completed",
                json.dumps({"demand_kind": "dialog_full_reconciliation", "demand_units": 1}),
            ),
        )
        conn.commit()

    report = build_operator_summary(db_path, since_seconds=15 * 3600, now=now)

    assert "dialog_full_reconciliation: completed=1" in report.text
    assert "max freshness debt" not in report.text


def test_summary_marks_window_unreliable_when_snapshot_reports_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 2_000_000_000.0
    db_path = tmp_path / "sync.db"
    _database(db_path, now_ms=int(now * 1000))
    import mcp_telegram.operator_summary as summary_module

    original = summary_module.read_operator_summary_snapshot

    def with_loss(path: Path, since_ms: int):
        observations, history_row, dialog_rows, _coverage = original(path, since_ms)
        return observations, history_row, dialog_rows, {"loss_observed": True}

    monkeypatch.setattr(summary_module, "read_operator_summary_snapshot", with_loss)
    report = build_operator_summary(db_path, since_seconds=15 * 3600, now=now)

    assert report.window_complete is False
    assert "window=partial/unreliable (telemetry loss)" in report.text


def test_summary_cli_reads_configured_runtime_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now_ms = 2_000_000_000_000
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _database(state_dir / "sync.db", now_ms=now_ms)
    config_home = tmp_path / "config"
    config_dir = config_home / "mcp-telegram"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(f'[state]\ndir = "{state_dir}"\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    result = runner.invoke(app, ["summary", "--since", "15h"])

    assert result.exit_code == 0, result.output
    assert "mcp-telegram operational summary" in result.output
    assert "Runtime:" in result.output
    assert "MCP:" in result.output


def test_summary_cli_defaults_to_one_day(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config_home = tmp_path / "config"
    config_dir = config_home / "mcp-telegram"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(f'[state]\ndir = "{state_dir}"\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    captured: list[int] = []

    def fake_build_operator_summary(db_path: Path, *, since_seconds: int, slow_request_seconds: float):
        del db_path, slow_request_seconds
        captured.append(since_seconds)
        from mcp_telegram.operator_summary import OperatorSummary

        return OperatorSummary(text="ok", window_complete=True)

    monkeypatch.setattr("mcp_telegram.operator_summary.build_operator_summary", fake_build_operator_summary)

    result = runner.invoke(app, ["summary"])

    assert result.exit_code == 0, result.output
    assert captured == [24 * 3600]
