"""Coverage must describe canonical acquisition time, never completion time."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from mcp_telegram.dialog_directory_coverage import read_dialog_directory_coverage
from mcp_telegram.sync_db import ensure_sync_schema


def test_published_directory_age_uses_acquisition_start_at_exact_stale_boundary(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE dialog_directory_publication SET generation=3,observation_started_at=100,observation_completed_at=9_000"
        )
        conn.execute(
            "INSERT INTO dialogs(dialog_id,type,identity_complete,identity_observed_at,hidden) VALUES (1,'user',1,100,0)"
        )
        assert read_dialog_directory_coverage(conn, now=999).to_wire()["status"] == "complete"
        assert read_dialog_directory_coverage(conn, now=999).to_wire()["age_seconds"] == 899
        assert read_dialog_directory_coverage(conn, now=1_000).to_wire()["status"] == "stale"
        assert read_dialog_directory_coverage(conn, now=1_001).to_wire()["age_seconds"] == 901
    finally:
        conn.close()


def test_unpublished_or_missing_start_never_claims_coverage(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE dialog_directory_state SET status='in_progress',observation_started_at=100")
        coverage = read_dialog_directory_coverage(conn, now=10_000)
        assert (coverage.status, coverage.age_seconds, coverage.observation_started_at) == ("in_progress", None, None)
        conn.execute("UPDATE dialog_directory_publication SET generation=1,observation_completed_at=200")
        coverage = read_dialog_directory_coverage(conn, now=10_000)
        assert (coverage.status, coverage.age_seconds, coverage.observation_started_at) == ("in_progress", None, None)
        conn.execute("UPDATE dialog_directory_state SET status='invalid'")
        assert read_dialog_directory_coverage(conn, now=10_000).status == "never"
    finally:
        conn.close()


def test_lookup_coverage_uses_only_visible_published_candidates_and_exposes_reason(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE dialog_directory_publication SET generation=3,observation_started_at=100,observation_completed_at=200"
        )
        conn.execute("UPDATE dialog_directory_state SET status='incomplete',reason='ordinary:TimeoutError'")
        conn.executemany(
            "INSERT INTO dialogs(dialog_id,type,identity_complete,identity_observed_at,hidden) VALUES (?,?,?,?,?)",
            [(1, "user", 1, 100, 0), (2, "user", 0, None, 1), (3, "unknown", 0, None, 1)],
        )
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (2,'access_lost')")
        coverage = read_dialog_directory_coverage(conn, now=200)
        assert coverage.lookup_complete is True
        assert coverage.lookup_fresh is True
        assert coverage.to_wire()["reason"] == "ordinary:TimeoutError"
    finally:
        conn.close()
