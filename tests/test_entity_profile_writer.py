"""Profile publications use the owner while serving from a real read-only connection."""

import sqlite3
import threading
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
from mcp_telegram.entity_profile.repository import EntityProfileRepository
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema, open_sync_db_reader
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_demand import RpcAttemptBudget
from tests.test_entity_profile_full_user_pair import _pair_service, _PairClient


async def test_profile_pair_commits_and_recovers_with_readonly_loop_connection(tmp_path: Path) -> None:
    path = tmp_path / "profiles.sqlite"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    service = cast(DaemonEntityInfoService, _pair_service(reader, _PairClient(), enabled=True))
    service._deps = replace(service._deps, writer=writer)
    observation_threads: list[int] = []

    class Observer:
        def observe_profile_pair(self, **_values: object) -> None:
            observation_threads.append(threading.get_ident())

    service.bind_profile_observer(Observer())

    def prepare(conn: sqlite3.Connection) -> None:
        profiles = EntityProfileRepository(conn, section_ttl_seconds=300)
        profiles.save_core({"id": 42, "type": "user", "name": "Target"}, now=100)
        profiles.mark_pending(42, now=100, pair_eligible_override=True)

    try:
        await writer.run(prepare)
        coordinator = service.refresh_coordinator
        assert coordinator is not None
        assert service._durable_refresh_status(100) is not None
        await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
        statuses = dict(
            reader.execute(
                "SELECT section, status FROM entity_detail_sections "
                "WHERE entity_id=42 AND section IN ('full_profile', 'personal_channel')"
            ).fetchall()
        )
        assert statuses == {"full_profile": "fresh", "personal_channel": "fresh"}
        assert observation_threads == [threading.get_ident()]
        assert service._profiles.next_due_refresh(now=100) is not None

        def leave_recoverable_measurement(conn: sqlite3.Connection) -> None:
            with write_transaction(conn):
                conn.execute(
                    "UPDATE entity_profile_refresh_state SET pair_summary_watermark=NULL, "
                    "pair_measurement_complete=0 WHERE entity_id=42"
                )

        await writer.run(leave_recoverable_measurement)
        assert service._durable_refresh_status(100) is not None
        assert service._profiles.next_due_refresh(now=100) is not None
        assert (
            reader.execute(
                "SELECT pair_measurement_complete FROM entity_profile_refresh_state WHERE entity_id=42"
            ).fetchone()[0]
            == 0
        )
        await service._write_profiles(lambda profiles: profiles.recover_pair_measurements(now=100))
        assert (
            reader.execute(
                "SELECT pair_measurement_complete FROM entity_profile_refresh_state WHERE entity_id=42"
            ).fetchone()[0]
            == 1
        )
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("DELETE FROM entity_details")
    finally:
        await service.shutdown()
        reader.close()
        await writer.aclose()
