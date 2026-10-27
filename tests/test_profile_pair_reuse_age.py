"""Receipt-age telemetry uses both original observations without renewing them."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
from mcp_telegram.entity_profile.repository import EntityRefreshCursor
from mcp_telegram.sync_transactions import write_savepoint
from tests.demand_adapter_helpers import run_ready_slice
from tests.test_entity_profile_full_user_pair import _PairClient, _prepare

type AcquiredPair = tuple[sqlite3.Connection, DaemonEntityInfoService, EntityRefreshCursor]


@pytest.fixture
async def acquired_pair(tmp_path: Path) -> AsyncIterator[AcquiredPair]:
    conn, service = _prepare(tmp_path / "pair-age.sqlite", migrated=True)
    assert isinstance(service, DaemonEntityInfoService)
    try:
        cursor = service._profiles.next_due_refresh(now=100)
        assert cursor is not None
        coordinator = service.refresh_coordinator
        assert coordinator is not None
        await run_ready_slice(EntityProfileDemandAdapter(coordinator), now=100)
        client = service._deps.client
        assert isinstance(client, _PairClient)
        assert client.full_user_calls == 1
        for section in ("full_profile", "personal_channel"):
            evidence = service._profiles.read_section_evidence(42, section)
            assert evidence is not None
            assert evidence["observation_started_at"] == 100
            assert evidence["identity"] is not None
        yield conn, service, cursor
    finally:
        await service.shutdown()
        conn.close()


@pytest.mark.parametrize(
    ("full_start", "channel_start", "now", "expected_ms"),
    ((100, 100, 125, 25_000.0), (90, 100, 125, 35_000.0), (100, 90, 125, 35_000.0), (100, 100, 95, 0.0)),
)
async def test_pair_age_uses_oldest_observation_and_clamps_clock_regression(
    acquired_pair: AcquiredPair, full_start: int, channel_start: int, now: int, expected_ms: float
) -> None:
    conn, service, cursor = acquired_pair
    with write_savepoint(conn):
        conn.executemany(
            "UPDATE entity_detail_sections SET observation_started_at=? WHERE entity_id=42 AND section=?",
            ((full_start, "full_profile"), (channel_start, "personal_channel")),
        )
    before = conn.total_changes

    assert service._pair_reused_age_ms(cursor, now=now) == expected_ms
    assert conn.total_changes == before
    assert isinstance(service._deps.client, _PairClient)
    assert service._deps.client.full_user_calls == 1


@pytest.mark.parametrize("section", ("full_profile", "personal_channel"))
async def test_pair_age_is_unknown_when_either_receipt_is_missing(acquired_pair: AcquiredPair, section: str) -> None:
    conn, service, cursor = acquired_pair
    with write_savepoint(conn):
        conn.execute("DELETE FROM entity_detail_sections WHERE entity_id=42 AND section=?", (section,))

    assert service._pair_reused_age_ms(cursor, now=125) is None


@pytest.mark.parametrize("started_at", (None, -1, "invalid", 100.5))
async def test_pair_age_rejects_malformed_stored_timestamp(acquired_pair: AcquiredPair, started_at: object) -> None:
    conn, service, cursor = acquired_pair
    with write_savepoint(conn):
        conn.execute(
            "UPDATE entity_detail_sections SET observation_started_at=? WHERE entity_id=42 AND section='full_profile'",
            (started_at,),
        )

    assert service._pair_reused_age_ms(cursor, now=125) is None


async def test_pair_age_rejects_boolean_timestamp(acquired_pair: AcquiredPair, monkeypatch: pytest.MonkeyPatch) -> None:
    _, service, cursor = acquired_pair
    read_evidence = service._profiles.read_section_evidence

    def boolean_timestamp(entity_id: int, section: str) -> dict[str, object] | None:
        evidence = read_evidence(entity_id, section)
        if evidence is not None and section == "full_profile":
            # SQLite coerces bool to int; exercise the projection boundary directly.
            evidence["observation_started_at"] = True
        return evidence

    monkeypatch.setattr(service._profiles, "read_section_evidence", boolean_timestamp)

    assert service._pair_reused_age_ms(cursor, now=125) is None
