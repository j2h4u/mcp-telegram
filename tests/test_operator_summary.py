"""Operator summary CLI and report tests."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mcp_telegram import app
from mcp_telegram.operator_summary import build_operator_summary, parse_since

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
    assert "Slow MCP calls: get_entity_info=1.5s" in report.text
    assert "scheduled_messages: dispatched=12, worst wait=9.0s, max queue=2" in report.text
    assert "sync.read_reconciliation applied=1" in report.text
    assert "get_entity_info: tool_error" in report.text


def test_summary_reports_demand_units_capacity_and_overdue_reason(tmp_path: Path) -> None:
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
                "offered",
                None,
                json.dumps({"demand_kind": "scheduled_repair", "demand_units": 4, "actual_attempts": 0}),
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
                        "demand_units": 2,
                        "actual_attempts": 1,
                        "oldest_overdue_seconds": 3.5,
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
                "predicted_selection",
                None,
                json.dumps(
                    {
                        "demand_kind": "scheduled_discovery",
                        "demand_units": 1,
                        "actual_attempts": 0,
                        "queue_age_seconds": 8.0,
                        "predicted_kind": "scheduled_repair",
                        "selection_match": False,
                    }
                ),
            ),
        )
        conn.commit()

    report = build_operator_summary(db_path, since_seconds=15 * 3600, now=now)

    assert (
        "Demand: offered=4, predicted selection=1, deferred=2, actual attempts=1, selection matches=0, "
        "mismatches=1, oldest queue age=8.0s, oldest overdue=3.5s, reasons=capacity=2" in report.text
    )
    assert "scheduled_repair: offered=4, deferred=2" in report.text


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

    def fake_build_operator_summary(db_path: Path, *, since_seconds: int):
        captured.append(since_seconds)
        from mcp_telegram.operator_summary import OperatorSummary

        return OperatorSummary(text="ok", window_complete=True)

    monkeypatch.setattr("mcp_telegram.operator_summary.build_operator_summary", fake_build_operator_summary)

    result = runner.invoke(app, ["summary"])

    assert result.exit_code == 0, result.output
    assert captured == [24 * 3600]
