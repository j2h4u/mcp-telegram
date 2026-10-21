"""Owner retirement rejects later diagnostics without affecting their callers."""

from pathlib import Path

import pytest

from mcp_telegram.config import RuntimeObservationConfig
from mcp_telegram.runtime_observations import RuntimeObservationSink
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema


@pytest.mark.asyncio
async def test_retired_shared_owner_reports_telemetry_loss(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    # A thread-bound result violates the owner contract and retires the connection.
    with pytest.raises(RuntimeError, match="thread-bound"):
        await writer.run(lambda conn: conn)
    sink = RuntimeObservationSink(path, writer=writer, retention_ttl_seconds=5, policy=RuntimeObservationConfig())
    assert sink.record(kind="telegram.rpc_admission")
    await sink.aclose()
    assert sink.counters.permanent_failures == 1
    assert sink.counters.successful_writes == 0
    assert not sink.record(kind="telegram.rpc_admission")
    await writer.aclose()
