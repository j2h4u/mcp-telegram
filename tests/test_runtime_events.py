from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from mcp_telegram.runtime_observations import encode_payload, prune_runtime_observations, record_runtime_observation
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema


def test_runtime_event_payload_is_bounded_and_kind_is_allowlisted() -> None:
    assert encode_payload({"value": 1}) == '{"value":1}'
    with pytest.raises(ValueError, match="1024"):
        encode_payload({"value": "x" * 1024})
    with closing(sqlite3.connect(":memory:")) as conn:
        with pytest.raises(ValueError, match="unsupported"):
            record_runtime_observation(conn, kind="arbitrary.debug")


def test_runtime_event_pruning_applies_ttl_and_cap(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    try:
        for event_id, observed_at in enumerate((1_000, 9_000, 9_100, 9_200), start=1):
            record_runtime_observation(
                conn,
                kind="mcp.call",
                tool_name=f"tool_{event_id}",
                observed_at_ms=observed_at,
            )
        deleted = prune_runtime_observations(conn, ttl_seconds=5, row_cap=2, now_ms=10_000)
        conn.commit()
        assert deleted == 2
        assert conn.execute("SELECT tool_name FROM runtime_observations ORDER BY id").fetchall() == [
            ("tool_3",),
            ("tool_4",),
        ]
        assert conn.execute(
            "SELECT value FROM daemon_state WHERE key='runtime_observations_last_cap_truncation_ms'"
        ).fetchone() == ("10000",)
    finally:
        conn.close()
