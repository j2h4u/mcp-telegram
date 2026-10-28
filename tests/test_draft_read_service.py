import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from telethon.tl import types

from mcp_telegram.config import DraftRecoveryConfig
from mcp_telegram.drafts.contracts import DraftObservationSource
from mcp_telegram.drafts.read_service import read_draft_request
from mcp_telegram.drafts.sqlite_projection import SQLiteDraftProjection
from mcp_telegram.drafts.telethon_adapter import normalize_update_draft
from mcp_telegram.sync_db import ensure_sync_schema


@pytest.fixture
def database(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, SQLiteDraftProjection]]:
    path = tmp_path / "drafts.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    projection = SQLiteDraftProjection(conn, DraftRecoveryConfig())
    projection.bind_account(42)
    try:
        yield conn, projection
    finally:
        conn.close()


def test_unknown_is_not_absent_and_read_does_not_schedule_acquisition(
    database: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, _projection = database
    before = conn.total_changes
    result = read_draft_request(conn, {"dialog_id": 100}, account_id=42)
    assert result["ok"] is True
    assert cast(dict[str, object], result["current"])["presence"] == "unknown"
    assert result["history"] == []
    assert result["acquisition_status"] == "unknown"
    assert result["coverage_status"] == "unknown"
    assert conn.total_changes == before


def test_retained_draft_read_returns_no_private_snapshot(
    database: tuple[sqlite3.Connection, SQLiteDraftProjection],
) -> None:
    conn, projection = database
    observed_at = datetime.now(UTC).replace(microsecond=0)
    observation = normalize_update_draft(
        types.UpdateDraftMessage(types.PeerUser(100), types.DraftMessage("retained text", observed_at)),
        account_id=42,
        source=DraftObservationSource.REALTIME,
        observed_at=observed_at,
    )
    assert observation is not None
    assert projection.apply_realtime(observation).accepted
    result = read_draft_request(conn, {"dialog_id": 100}, account_id=42)
    current = cast(dict[str, object], result["current"])
    assert current["presence"] == "present"
    assert current["text"] == "retained text"
    assert current["expected_version"]
    assert "restore_payload" not in str(result)


@pytest.mark.parametrize(
    "payload,error",
    [
        ({"dialog_id": True}, "invalid_params"),
        ({"dialog_id": 0}, "invalid_params"),
        ({"dialog_id": 100, "topic_id": -1}, "invalid_params"),
        ({"dialog_id": 100, "account_id": 43}, "account_mismatch"),
        ({"dialog_id": 100, "history_id": 1}, "history_unavailable"),
    ],
)
def test_invalid_or_other_account_requests_are_rejected(
    database: tuple[sqlite3.Connection, SQLiteDraftProjection], payload: dict[str, object], error: str
) -> None:
    conn, _projection = database
    assert read_draft_request(conn, payload, account_id=42)["error"] == error
