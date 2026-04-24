from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_telegram.daemon import _log_heartbeat, sync_main

# ---------------------------------------------------------------------------
# CLI registration
# ---------------------------------------------------------------------------


def test_sync_command_exists() -> None:
    """Typer CLI has a 'sync' command — verified via --help output."""
    from typer.testing import CliRunner

    from mcp_telegram import app

    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "sync" in result.output


# ---------------------------------------------------------------------------
# sync_main lifecycle
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_client() -> MagicMock:
    """Return a mock TelegramClient with connection tracking.

    is_connected() is a synchronous call in TelegramClient, so we use MagicMock
    for the base object and AsyncMock only for the async methods.
    """
    from helpers import MockTotalList

    client = MagicMock()
    client.is_connected.return_value = True  # sync method
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    # get_messages used by backfill and probe-worker
    client.get_messages = AsyncMock(return_value=MockTotalList([], total=0))
    # Phase 39.1: sync_main caches self_id from client.get_me() at startup
    _me = MagicMock()
    _me.id = 11111
    client.get_me = AsyncMock(return_value=_me)
    return client


@pytest.fixture()
def instant_shutdown_event() -> asyncio.Event:
    """Return a pre-set asyncio.Event so the daemon exits immediately."""
    event = asyncio.Event()
    event.set()
    return event


def test_sync_main_connects_and_heartbeats(
    mock_client: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """sync_main() calls client.connect() and invokes _log_heartbeat."""
    shutdown_event = asyncio.Event()

    def mock_register_shutdown(conn, loop):
        return shutdown_event

    def heartbeat_then_shutdown(*args):
        _log_heartbeat(*args)
        shutdown_event.set()

    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client),
        patch("mcp_telegram.daemon.ensure_sync_schema"),
        patch("mcp_telegram.daemon.register_shutdown_handler", side_effect=mock_register_shutdown),
        patch("mcp_telegram.daemon.get_sync_db_path"),
        patch("mcp_telegram.daemon._open_sync_db"),
        patch("mcp_telegram.daemon.backfill_fts_index", return_value=0),
        patch("mcp_telegram.daemon._log_heartbeat", side_effect=heartbeat_then_shutdown) as mock_hb,
        patch("mcp_telegram.daemon.HEARTBEAT_INTERVAL_S", 0.01),
        caplog.at_level(logging.INFO, logger="mcp_telegram.daemon"),
    ):
        asyncio.run(sync_main())

    mock_client.connect.assert_called_once()
    mock_hb.assert_called()
    assert any("heartbeat" in record.message for record in caplog.records)


def test_sync_main_ensures_schema(
    mock_client: AsyncMock,
    instant_shutdown_event: asyncio.Event,
) -> None:
    """sync_main() calls ensure_sync_schema before entering the heartbeat loop."""
    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client),
        patch("mcp_telegram.daemon.ensure_sync_schema") as mock_ensure,
        patch("mcp_telegram.daemon.register_shutdown_handler", return_value=instant_shutdown_event),
        patch("mcp_telegram.daemon.get_sync_db_path") as mock_get_path,
        patch("mcp_telegram.daemon._open_sync_db"),
        patch("mcp_telegram.daemon.backfill_fts_index", return_value=0),
    ):
        asyncio.run(sync_main())

    mock_ensure.assert_called_once_with(mock_get_path.return_value)


def test_sync_main_registers_shutdown(
    mock_client: AsyncMock,
    instant_shutdown_event: asyncio.Event,
) -> None:
    """sync_main() calls register_shutdown_handler with the open DB connection."""
    mock_conn = MagicMock()

    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client),
        patch("mcp_telegram.daemon.ensure_sync_schema"),
        patch("mcp_telegram.daemon.register_shutdown_handler", return_value=instant_shutdown_event) as mock_reg,
        patch("mcp_telegram.daemon.get_sync_db_path"),
        patch("mcp_telegram.daemon._open_sync_db", return_value=mock_conn),
        patch("mcp_telegram.daemon.backfill_fts_index", return_value=0),
    ):
        asyncio.run(sync_main())

    mock_reg.assert_called_once()
    call_args = mock_reg.call_args
    assert call_args[0][0] is mock_conn, "register_shutdown_handler must receive the open DB connection"


def test_self_id_cached_at_startup(
    mock_client: MagicMock,
    instant_shutdown_event: asyncio.Event,
) -> None:
    """Phase 39.1: sync_main() caches client.get_me().id on DaemonAPIServer.self_id.

    get_me() must be called exactly once, and the cached integer must be
    exposed via api_server.self_id for Plan 02's SQL parameter binding.
    """
    me_user = MagicMock()
    me_user.id = 12345
    mock_client.get_me = AsyncMock(return_value=me_user)

    captured: dict[str, object] = {}

    class _Capturing:
        def __init__(self, conn, client, shutdown_event):
            self._conn = conn
            self._client = client
            self._shutdown_event = shutdown_event
            self.self_id = None
            captured["instance"] = self

        async def handle_client(self, reader, writer):  # pragma: no cover
            pass

    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client),
        patch("mcp_telegram.daemon.ensure_sync_schema"),
        patch("mcp_telegram.daemon.register_shutdown_handler", return_value=instant_shutdown_event),
        patch("mcp_telegram.daemon.get_sync_db_path"),
        patch("mcp_telegram.daemon._open_sync_db"),
        patch("mcp_telegram.daemon.backfill_fts_index", return_value=0),
        patch("mcp_telegram.daemon.DaemonAPIServer", _Capturing),
        patch("mcp_telegram.daemon.migrate_legacy_databases"),
    ):
        asyncio.run(sync_main())

    instance = captured.get("instance")
    assert instance is not None, "DaemonAPIServer was not instantiated"
    assert instance.self_id == 12345, (  # type: ignore[attr-defined]
        f"expected self_id=12345, got {instance.self_id!r}"  # type: ignore[attr-defined]
    )
    assert mock_client.get_me.call_count == 1, (
        f"get_me must be called exactly once at startup, got {mock_client.get_me.call_count}"
    )


def test_sync_main_disconnects_client_on_shutdown(
    mock_client: AsyncMock,
    instant_shutdown_event: asyncio.Event,
) -> None:
    """When shutdown_event is set, sync_main() calls client.disconnect() before returning."""
    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client),
        patch("mcp_telegram.daemon.ensure_sync_schema"),
        patch("mcp_telegram.daemon.register_shutdown_handler", return_value=instant_shutdown_event),
        patch("mcp_telegram.daemon.get_sync_db_path"),
        patch("mcp_telegram.daemon._open_sync_db"),
        patch("mcp_telegram.daemon.backfill_fts_index", return_value=0),
    ):
        asyncio.run(sync_main())

    mock_client.disconnect.assert_called_once()


def test_sync_main_heartbeat_logs_connection_state(
    mock_client: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Heartbeat INFO log includes 'connected=True' (or connection state)."""
    shutdown_event = asyncio.Event()

    def mock_register_shutdown(conn, loop):
        return shutdown_event

    def heartbeat_then_shutdown(*args):
        _log_heartbeat(*args)
        shutdown_event.set()

    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client),
        patch("mcp_telegram.daemon.ensure_sync_schema"),
        patch("mcp_telegram.daemon.register_shutdown_handler", side_effect=mock_register_shutdown),
        patch("mcp_telegram.daemon.get_sync_db_path"),
        patch("mcp_telegram.daemon._open_sync_db"),
        patch("mcp_telegram.daemon.backfill_fts_index", return_value=0),
        patch("mcp_telegram.daemon._log_heartbeat", side_effect=heartbeat_then_shutdown),
        patch("mcp_telegram.daemon.HEARTBEAT_INTERVAL_S", 0.01),
        caplog.at_level(logging.INFO, logger="mcp_telegram.daemon"),
    ):
        asyncio.run(sync_main())

    heartbeat_logs = [r.message for r in caplog.records if "heartbeat" in r.message]
    assert heartbeat_logs, "Expected at least one heartbeat log"
    assert any("connected=" in msg for msg in heartbeat_logs), (
        f"Heartbeat logs did not include 'connected=': {heartbeat_logs}"
    )


def test_sync_main_survives_connection_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If client.connect() raises ConnectionError, sync_main logs error and exits without raising."""
    mock_client = MagicMock()
    mock_client.is_connected.return_value = False
    mock_client.connect = AsyncMock(side_effect=ConnectionError("test connection failure"))
    mock_client.disconnect = AsyncMock()

    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client),
        patch("mcp_telegram.daemon.ensure_sync_schema"),
        patch("mcp_telegram.daemon.register_shutdown_handler"),
        patch("mcp_telegram.daemon.get_sync_db_path"),
        patch("mcp_telegram.daemon._open_sync_db"),
        patch("mcp_telegram.daemon.backfill_fts_index", return_value=0),
        caplog.at_level(logging.ERROR, logger="mcp_telegram.daemon"),
    ):
        # Must not raise
        asyncio.run(sync_main())

    error_logs = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_logs, "Expected an ERROR log for connection failure"


# ---------------------------------------------------------------------------
# FullSyncWorker integration tests (Plan 26-02)
# ---------------------------------------------------------------------------


def test_sync_main_calls_bootstrap_dms(
    mock_client: MagicMock,
    instant_shutdown_event: asyncio.Event,
) -> None:
    """After connect(), sync_main() creates FullSyncWorker and calls bootstrap_dms() before any
    process_one_batch() call."""
    worker_instance = MagicMock()
    worker_instance.bootstrap_dms = AsyncMock(return_value=5)
    worker_instance.process_one_batch = AsyncMock(return_value=True)  # immediately idle

    worker_class = MagicMock(return_value=worker_instance)

    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client),
        patch("mcp_telegram.daemon.ensure_sync_schema"),
        patch("mcp_telegram.daemon.register_shutdown_handler", return_value=instant_shutdown_event),
        patch("mcp_telegram.daemon.get_sync_db_path"),
        patch("mcp_telegram.daemon._open_sync_db"),
        patch("mcp_telegram.daemon.backfill_fts_index", return_value=0),
        patch("mcp_telegram.daemon.FullSyncWorker", worker_class),
    ):
        asyncio.run(sync_main())

    worker_instance.bootstrap_dms.assert_called_once()


def test_sync_main_calls_process_one_batch(
    mock_client: MagicMock,
) -> None:
    """sync_main() calls worker.process_one_batch() at least once before shutdown."""
    # Use an event that fires after process_one_batch returns True (idle path triggers shutdown)
    shutdown_event = asyncio.Event()

    async def process_one_batch_then_shutdown() -> bool:
        shutdown_event.set()  # trigger shutdown after first batch
        return True  # all synced — enter idle mode (which respects shutdown_event)

    worker_instance = MagicMock()
    worker_instance.bootstrap_dms = AsyncMock(return_value=0)
    worker_instance.process_one_batch = AsyncMock(side_effect=process_one_batch_then_shutdown)

    worker_class = MagicMock(return_value=worker_instance)

    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client),
        patch("mcp_telegram.daemon.ensure_sync_schema"),
        patch("mcp_telegram.daemon.register_shutdown_handler", return_value=shutdown_event),
        patch("mcp_telegram.daemon.get_sync_db_path"),
        patch("mcp_telegram.daemon._open_sync_db"),
        patch("mcp_telegram.daemon.backfill_fts_index", return_value=0),
        patch("mcp_telegram.daemon.FullSyncWorker", worker_class),
    ):
        asyncio.run(sync_main())

    worker_instance.process_one_batch.assert_called()


def test_sync_main_idles_when_all_synced(
    mock_client: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When process_one_batch() returns True (all synced), daemon falls back to heartbeat-only
    wait. Verify heartbeat log appears after idle starts."""
    shutdown_event = asyncio.Event()

    def mock_register_shutdown(conn, loop):
        async def _set_after_delay() -> None:
            await asyncio.sleep(0.05)
            shutdown_event.set()

        loop.create_task(_set_after_delay())
        return shutdown_event

    worker_instance = MagicMock()
    worker_instance.bootstrap_dms = AsyncMock(return_value=0)
    worker_instance.process_one_batch = AsyncMock(return_value=True)  # all synced immediately

    worker_class = MagicMock(return_value=worker_instance)

    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client),
        patch("mcp_telegram.daemon.ensure_sync_schema"),
        patch("mcp_telegram.daemon.register_shutdown_handler", side_effect=mock_register_shutdown),
        patch("mcp_telegram.daemon.get_sync_db_path"),
        patch("mcp_telegram.daemon._open_sync_db"),
        patch("mcp_telegram.daemon.backfill_fts_index", return_value=0),
        patch("mcp_telegram.daemon.HEARTBEAT_INTERVAL_S", 0.01),
        patch("mcp_telegram.daemon.FullSyncWorker", worker_class),
        caplog.at_level(logging.INFO, logger="mcp_telegram.daemon"),
    ):
        asyncio.run(sync_main())

    heartbeat_logs = [r.message for r in caplog.records if "heartbeat" in r.message]
    assert heartbeat_logs, "Expected heartbeat log when daemon is in idle mode after all synced"


def test_sync_main_logs_heartbeat_during_sync(
    mock_client: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """While process_one_batch returns False (work in progress), daemon still emits
    periodic heartbeat logs."""
    shutdown_event = asyncio.Event()
    call_count = 0

    async def process_one_batch_side_effect() -> bool:
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            shutdown_event.set()
        return False  # always more work

    worker_instance = MagicMock()
    worker_instance.bootstrap_dms = AsyncMock(return_value=0)
    worker_instance.process_one_batch = AsyncMock(side_effect=process_one_batch_side_effect)

    worker_class = MagicMock(return_value=worker_instance)

    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client),
        patch("mcp_telegram.daemon.ensure_sync_schema"),
        patch("mcp_telegram.daemon.register_shutdown_handler", return_value=shutdown_event),
        patch("mcp_telegram.daemon.get_sync_db_path"),
        patch("mcp_telegram.daemon._open_sync_db"),
        patch("mcp_telegram.daemon.backfill_fts_index", return_value=0),
        patch("mcp_telegram.daemon.HEARTBEAT_INTERVAL_S", 0.0),  # instant heartbeat
        patch("mcp_telegram.daemon.FullSyncWorker", worker_class),
        caplog.at_level(logging.INFO, logger="mcp_telegram.daemon"),
    ):
        asyncio.run(sync_main())

    heartbeat_logs = [r.message for r in caplog.records if "heartbeat" in r.message]
    assert heartbeat_logs, "Expected heartbeat log during active sync"


# ---------------------------------------------------------------------------
# EventHandlerManager integration tests (Plan 27-02)
# ---------------------------------------------------------------------------


def test_handlers_registered_before_worker(
    mock_client: MagicMock,
    instant_shutdown_event: asyncio.Event,
) -> None:
    """handler_manager.register() must be called BEFORE worker.bootstrap_dms() (D-06)."""
    call_order: list[str] = []

    worker_instance = MagicMock()
    worker_instance.bootstrap_dms = AsyncMock(side_effect=lambda: call_order.append("bootstrap_dms") or 0)
    worker_instance.process_one_batch = AsyncMock(return_value=True)

    worker_class = MagicMock(return_value=worker_instance)

    handler_instance = MagicMock()
    handler_instance.register = MagicMock(side_effect=lambda: call_order.append("register"))
    handler_instance.unregister = MagicMock()
    handler_instance.refresh_synced_dialogs = MagicMock()
    handler_instance.run_dm_gap_scan = AsyncMock(return_value=0)

    handler_class = MagicMock(return_value=handler_instance)

    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client),
        patch("mcp_telegram.daemon.ensure_sync_schema"),
        patch("mcp_telegram.daemon.register_shutdown_handler", return_value=instant_shutdown_event),
        patch("mcp_telegram.daemon.get_sync_db_path"),
        patch("mcp_telegram.daemon._open_sync_db"),
        patch("mcp_telegram.daemon.backfill_fts_index", return_value=0),
        patch("mcp_telegram.daemon.FullSyncWorker", worker_class),
        patch("mcp_telegram.daemon.EventHandlerManager", handler_class),
    ):
        asyncio.run(sync_main())

    assert "register" in call_order, "handler_manager.register() was never called"
    assert "bootstrap_dms" in call_order, "worker.bootstrap_dms() was never called"
    assert call_order.index("register") < call_order.index("bootstrap_dms"), (
        f"handler_manager.register() must be called BEFORE bootstrap_dms(); got order: {call_order}"
    )


def test_heartbeat_refreshes_synced_dialogs(
    mock_client: MagicMock,
) -> None:
    """refresh_synced_dialogs() must be called at least once during heartbeat."""
    shutdown_event = asyncio.Event()
    call_count = 0

    async def process_one_batch_side_effect() -> bool:
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            shutdown_event.set()
        return False  # keep looping

    worker_instance = MagicMock()
    worker_instance.bootstrap_dms = AsyncMock(return_value=0)
    worker_instance.process_one_batch = AsyncMock(side_effect=process_one_batch_side_effect)

    worker_class = MagicMock(return_value=worker_instance)

    handler_instance = MagicMock()
    handler_instance.register = MagicMock()
    handler_instance.unregister = MagicMock()
    handler_instance.refresh_synced_dialogs = MagicMock()
    handler_instance.run_dm_gap_scan = AsyncMock(return_value=0)

    handler_class = MagicMock(return_value=handler_instance)

    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client),
        patch("mcp_telegram.daemon.ensure_sync_schema"),
        patch("mcp_telegram.daemon.register_shutdown_handler", return_value=shutdown_event),
        patch("mcp_telegram.daemon.get_sync_db_path"),
        patch("mcp_telegram.daemon._open_sync_db"),
        patch("mcp_telegram.daemon.backfill_fts_index", return_value=0),
        patch("mcp_telegram.daemon.HEARTBEAT_INTERVAL_S", 0.0),  # instant heartbeat
        patch("mcp_telegram.daemon.FullSyncWorker", worker_class),
        patch("mcp_telegram.daemon.EventHandlerManager", handler_class),
    ):
        asyncio.run(sync_main())

    assert handler_instance.refresh_synced_dialogs.call_count >= 1, (
        "Expected refresh_synced_dialogs() to be called at least once during heartbeat"
    )


def test_gap_scan_runs_on_weekly_schedule(
    mock_client: MagicMock,
) -> None:
    """run_dm_gap_scan() is called when enough time has elapsed (simulated 7 days).

    Uses GAP_SCAN_INTERVAL_S=0.0 so the elapsed check is always satisfied, and
    a shutdown_event that fires after the first batch to ensure the loop body runs.
    """
    shutdown_event = asyncio.Event()

    async def process_then_shutdown() -> bool:
        shutdown_event.set()  # trigger shutdown after first batch
        return True  # all synced

    worker_instance = MagicMock()
    worker_instance.bootstrap_dms = AsyncMock(return_value=0)
    worker_instance.process_one_batch = AsyncMock(side_effect=process_then_shutdown)

    worker_class = MagicMock(return_value=worker_instance)

    handler_instance = MagicMock()
    handler_instance.register = MagicMock()
    handler_instance.unregister = MagicMock()
    handler_instance.refresh_synced_dialogs = MagicMock()
    handler_instance.run_dm_gap_scan = AsyncMock(return_value=0)

    handler_class = MagicMock(return_value=handler_instance)

    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client),
        patch("mcp_telegram.daemon.ensure_sync_schema"),
        patch("mcp_telegram.daemon.register_shutdown_handler", return_value=shutdown_event),
        patch("mcp_telegram.daemon.get_sync_db_path"),
        patch("mcp_telegram.daemon._open_sync_db"),
        patch("mcp_telegram.daemon.backfill_fts_index", return_value=0),
        patch("mcp_telegram.daemon.GAP_SCAN_INTERVAL_S", 0.0),
        patch("mcp_telegram.daemon.FullSyncWorker", worker_class),
        patch("mcp_telegram.daemon.EventHandlerManager", handler_class),
    ):
        asyncio.run(sync_main())

    handler_instance.run_dm_gap_scan.assert_called()


def test_gap_scan_not_called_before_interval(
    mock_client: MagicMock,
    instant_shutdown_event: asyncio.Event,
) -> None:
    """run_dm_gap_scan() is NOT called when less than GAP_SCAN_INTERVAL_S has elapsed."""
    worker_instance = MagicMock()
    worker_instance.bootstrap_dms = AsyncMock(return_value=0)
    worker_instance.process_one_batch = AsyncMock(return_value=True)

    worker_class = MagicMock(return_value=worker_instance)

    handler_instance = MagicMock()
    handler_instance.register = MagicMock()
    handler_instance.unregister = MagicMock()
    handler_instance.refresh_synced_dialogs = MagicMock()
    handler_instance.run_dm_gap_scan = AsyncMock(return_value=0)

    handler_class = MagicMock(return_value=handler_instance)

    # Keep GAP_SCAN_INTERVAL_S at a large value — not enough time will have elapsed
    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client),
        patch("mcp_telegram.daemon.ensure_sync_schema"),
        patch("mcp_telegram.daemon.register_shutdown_handler", return_value=instant_shutdown_event),
        patch("mcp_telegram.daemon.get_sync_db_path"),
        patch("mcp_telegram.daemon._open_sync_db"),
        patch("mcp_telegram.daemon.backfill_fts_index", return_value=0),
        patch("mcp_telegram.daemon.HEARTBEAT_INTERVAL_S", 0.0),
        patch("mcp_telegram.daemon.GAP_SCAN_INTERVAL_S", 9999999.0),  # far future
        patch("mcp_telegram.daemon.FullSyncWorker", worker_class),
        patch("mcp_telegram.daemon.EventHandlerManager", handler_class),
    ):
        asyncio.run(sync_main())

    handler_instance.run_dm_gap_scan.assert_not_called()


def test_handlers_unregistered_on_shutdown(
    mock_client: MagicMock,
    instant_shutdown_event: asyncio.Event,
) -> None:
    """handler_manager.unregister() must be called before client.disconnect() in finally block."""
    call_order: list[str] = []

    mock_client.disconnect = AsyncMock(side_effect=lambda: call_order.append("disconnect"))

    worker_instance = MagicMock()
    worker_instance.bootstrap_dms = AsyncMock(return_value=0)
    worker_instance.process_one_batch = AsyncMock(return_value=True)

    worker_class = MagicMock(return_value=worker_instance)

    handler_instance = MagicMock()
    handler_instance.register = MagicMock()
    handler_instance.unregister = MagicMock(side_effect=lambda: call_order.append("unregister"))
    handler_instance.refresh_synced_dialogs = MagicMock()
    handler_instance.run_dm_gap_scan = AsyncMock(return_value=0)

    handler_class = MagicMock(return_value=handler_instance)

    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client),
        patch("mcp_telegram.daemon.ensure_sync_schema"),
        patch("mcp_telegram.daemon.register_shutdown_handler", return_value=instant_shutdown_event),
        patch("mcp_telegram.daemon.get_sync_db_path"),
        patch("mcp_telegram.daemon._open_sync_db"),
        patch("mcp_telegram.daemon.backfill_fts_index", return_value=0),
        patch("mcp_telegram.daemon.FullSyncWorker", worker_class),
        patch("mcp_telegram.daemon.EventHandlerManager", handler_class),
    ):
        asyncio.run(sync_main())

    assert "unregister" in call_order, "handler_manager.unregister() was never called"
    assert "disconnect" in call_order, "client.disconnect() was never called"
    assert call_order.index("unregister") < call_order.index("disconnect"), (
        f"handler_manager.unregister() must be called BEFORE client.disconnect(); got order: {call_order}"
    )


# ---------------------------------------------------------------------------
# DeltaSyncWorker integration tests (Plan 28-02)
# ---------------------------------------------------------------------------


def test_create_client_called_with_catch_up(
    mock_client: MagicMock,
    instant_shutdown_event: asyncio.Event,
) -> None:
    """sync_main() calls create_client(catch_up=True) for PTS catch-up (D-05)."""
    worker_instance = MagicMock()
    worker_instance.bootstrap_dms = AsyncMock(return_value=0)
    worker_instance.process_one_batch = AsyncMock(return_value=True)

    delta_instance = MagicMock()
    delta_instance.run_delta_catch_up = AsyncMock(return_value=0)

    handler_instance = MagicMock()
    handler_instance.register = MagicMock()
    handler_instance.unregister = MagicMock()
    handler_instance.refresh_synced_dialogs = MagicMock()
    handler_instance.run_dm_gap_scan = AsyncMock(return_value=0)

    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client) as mock_create,
        patch("mcp_telegram.daemon.ensure_sync_schema"),
        patch("mcp_telegram.daemon.register_shutdown_handler", return_value=instant_shutdown_event),
        patch("mcp_telegram.daemon.get_sync_db_path"),
        patch("mcp_telegram.daemon._open_sync_db"),
        patch("mcp_telegram.daemon.backfill_fts_index", return_value=0),
        patch("mcp_telegram.daemon.FullSyncWorker", return_value=worker_instance),
        patch("mcp_telegram.daemon.DeltaSyncWorker", return_value=delta_instance),
        patch("mcp_telegram.daemon.EventHandlerManager", return_value=handler_instance),
    ):
        asyncio.run(sync_main())

    mock_create.assert_called_once_with(catch_up=True)


def test_create_client_catch_up_default_false() -> None:
    """create_client() signature has catch_up parameter with default False (backward compat)."""
    import inspect

    from mcp_telegram.telegram import create_client

    sig = inspect.signature(create_client.__wrapped__)  # unwrap @cache
    assert "catch_up" in sig.parameters
    assert sig.parameters["catch_up"].default is False


def test_delta_catch_up_runs_before_bootstrap(
    mock_client: MagicMock,
    instant_shutdown_event: asyncio.Event,
) -> None:
    """DeltaSyncWorker.run_delta_catch_up() runs AFTER register() but BEFORE bootstrap_dms() (D-08)."""
    call_order: list[str] = []

    worker_instance = MagicMock()
    worker_instance.bootstrap_dms = AsyncMock(side_effect=lambda: call_order.append("bootstrap_dms") or 0)
    worker_instance.process_one_batch = AsyncMock(return_value=True)

    delta_instance = MagicMock()
    delta_instance.run_delta_catch_up = AsyncMock(side_effect=lambda: call_order.append("delta_catch_up") or 0)

    handler_instance = MagicMock()
    handler_instance.register = MagicMock(side_effect=lambda: call_order.append("register"))
    handler_instance.unregister = MagicMock()
    handler_instance.refresh_synced_dialogs = MagicMock()
    handler_instance.run_dm_gap_scan = AsyncMock(return_value=0)

    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client),
        patch("mcp_telegram.daemon.ensure_sync_schema"),
        patch("mcp_telegram.daemon.register_shutdown_handler", return_value=instant_shutdown_event),
        patch("mcp_telegram.daemon.get_sync_db_path"),
        patch("mcp_telegram.daemon._open_sync_db"),
        patch("mcp_telegram.daemon.backfill_fts_index", return_value=0),
        patch("mcp_telegram.daemon.FullSyncWorker", return_value=worker_instance),
        patch("mcp_telegram.daemon.DeltaSyncWorker", return_value=delta_instance),
        patch("mcp_telegram.daemon.EventHandlerManager", return_value=handler_instance),
    ):
        asyncio.run(sync_main())

    assert "register" in call_order, "handler_manager.register() was never called"
    assert "delta_catch_up" in call_order, "delta_worker.run_delta_catch_up() was never called"
    assert "bootstrap_dms" in call_order, "worker.bootstrap_dms() was never called"
    assert call_order.index("register") < call_order.index("delta_catch_up"), (
        f"register() must be called BEFORE delta_catch_up; got order: {call_order}"
    )
    assert call_order.index("delta_catch_up") < call_order.index("bootstrap_dms"), (
        f"delta_catch_up must be called BEFORE bootstrap_dms; got order: {call_order}"
    )


def test_delta_catch_up_logged(
    mock_client: MagicMock,
    instant_shutdown_event: asyncio.Event,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """sync_main() logs a 'delta_catch_up' INFO message during startup."""
    worker_instance = MagicMock()
    worker_instance.bootstrap_dms = AsyncMock(return_value=0)
    worker_instance.process_one_batch = AsyncMock(return_value=True)

    delta_instance = MagicMock()
    delta_instance.run_delta_catch_up = AsyncMock(return_value=0)

    handler_instance = MagicMock()
    handler_instance.register = MagicMock()
    handler_instance.unregister = MagicMock()
    handler_instance.refresh_synced_dialogs = MagicMock()
    handler_instance.run_dm_gap_scan = AsyncMock(return_value=0)

    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client),
        patch("mcp_telegram.daemon.ensure_sync_schema"),
        patch("mcp_telegram.daemon.register_shutdown_handler", return_value=instant_shutdown_event),
        patch("mcp_telegram.daemon.get_sync_db_path"),
        patch("mcp_telegram.daemon._open_sync_db"),
        patch("mcp_telegram.daemon.backfill_fts_index", return_value=0),
        patch("mcp_telegram.daemon.FullSyncWorker", return_value=worker_instance),
        patch("mcp_telegram.daemon.DeltaSyncWorker", return_value=delta_instance),
        patch("mcp_telegram.daemon.EventHandlerManager", return_value=handler_instance),
        caplog.at_level(logging.INFO, logger="mcp_telegram.daemon"),
    ):
        asyncio.run(sync_main())

    assert any("delta_catch_up" in record.message for record in caplog.records), (
        "Expected 'delta_catch_up' in INFO log output during startup"
    )


def test_delta_catch_up_result_logged(
    mock_client: MagicMock,
    instant_shutdown_event: asyncio.Event,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """sync_main() logs the return value of run_delta_catch_up() in the startup message."""
    worker_instance = MagicMock()
    worker_instance.bootstrap_dms = AsyncMock(return_value=0)
    worker_instance.process_one_batch = AsyncMock(return_value=True)

    delta_instance = MagicMock()
    delta_instance.run_delta_catch_up = AsyncMock(return_value=5)  # 5 new messages

    handler_instance = MagicMock()
    handler_instance.register = MagicMock()
    handler_instance.unregister = MagicMock()
    handler_instance.refresh_synced_dialogs = MagicMock()
    handler_instance.run_dm_gap_scan = AsyncMock(return_value=0)

    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client),
        patch("mcp_telegram.daemon.ensure_sync_schema"),
        patch("mcp_telegram.daemon.register_shutdown_handler", return_value=instant_shutdown_event),
        patch("mcp_telegram.daemon.get_sync_db_path"),
        patch("mcp_telegram.daemon._open_sync_db"),
        patch("mcp_telegram.daemon.backfill_fts_index", return_value=0),
        patch("mcp_telegram.daemon.FullSyncWorker", return_value=worker_instance),
        patch("mcp_telegram.daemon.DeltaSyncWorker", return_value=delta_instance),
        patch("mcp_telegram.daemon.EventHandlerManager", return_value=handler_instance),
        caplog.at_level(logging.INFO, logger="mcp_telegram.daemon"),
    ):
        asyncio.run(sync_main())

    delta_logs = [r.message for r in caplog.records if "delta_catch_up" in r.message]
    assert delta_logs, "Expected at least one log message containing 'delta_catch_up'"
    assert any("5" in msg for msg in delta_logs), f"Expected log to contain return value '5'; got: {delta_logs}"


# ---------------------------------------------------------------------------
# Phase 29-02: DaemonAPIServer + FTS backfill in daemon.py
# ---------------------------------------------------------------------------


def _make_standard_mocks(instant_shutdown_event: asyncio.Event) -> dict:
    """Return a dict of standard mock instances for daemon integration tests."""
    worker_instance = MagicMock()
    worker_instance.bootstrap_dms = AsyncMock(return_value=0)
    worker_instance.process_one_batch = AsyncMock(return_value=True)

    delta_instance = MagicMock()
    delta_instance.run_delta_catch_up = AsyncMock(return_value=0)

    handler_instance = MagicMock()
    handler_instance.register = MagicMock()
    handler_instance.unregister = MagicMock()
    handler_instance.refresh_synced_dialogs = MagicMock()
    handler_instance.run_dm_gap_scan = AsyncMock(return_value=0)

    return {
        "worker": worker_instance,
        "delta": delta_instance,
        "handler": handler_instance,
    }


def test_sync_main_starts_api_server(
    mock_client: MagicMock,
    instant_shutdown_event: asyncio.Event,
) -> None:
    """sync_main() calls asyncio.start_unix_server with the correct socket path."""
    mocks = _make_standard_mocks(instant_shutdown_event)

    mock_unix_server = MagicMock()
    mock_unix_server.close = MagicMock()
    mock_unix_server.wait_closed = AsyncMock()

    captured_args: list = []

    async def mock_start_unix_server(handler, path=None, **kwargs):
        captured_args.append((handler, path))
        return mock_unix_server

    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client),
        patch("mcp_telegram.daemon.ensure_sync_schema"),
        patch("mcp_telegram.daemon.register_shutdown_handler", return_value=instant_shutdown_event),
        patch("mcp_telegram.daemon.get_sync_db_path"),
        patch("mcp_telegram.daemon._open_sync_db"),
        patch("mcp_telegram.daemon.backfill_fts_index", return_value=0),
        patch("mcp_telegram.daemon.FullSyncWorker", return_value=mocks["worker"]),
        patch("mcp_telegram.daemon.DeltaSyncWorker", return_value=mocks["delta"]),
        patch("mcp_telegram.daemon.EventHandlerManager", return_value=mocks["handler"]),
        patch("mcp_telegram.daemon.asyncio.start_unix_server", side_effect=mock_start_unix_server),
        patch("mcp_telegram.daemon.os.chmod"),
    ):
        asyncio.run(sync_main())

    assert len(captured_args) == 1, "asyncio.start_unix_server must be called exactly once"
    handler_fn, socket_path = captured_args[0]
    assert socket_path is not None, "socket path must be provided"
    assert callable(handler_fn), "handler must be callable"


def test_sync_main_runs_fts_backfill(
    mock_client: MagicMock,
    instant_shutdown_event: asyncio.Event,
) -> None:
    """sync_main() schedules backfill_fts_index(conn) as a background task."""
    mocks = _make_standard_mocks(instant_shutdown_event)
    mock_conn = MagicMock()

    mock_unix_server = MagicMock()
    mock_unix_server.close = MagicMock()
    mock_unix_server.wait_closed = AsyncMock()

    mock_backfill = MagicMock(return_value=0)

    # Run backfill_fts_index synchronously inside the test so the assertion
    # fires before asyncio.run() returns, regardless of shutdown timing.
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client),
        patch("mcp_telegram.daemon.ensure_sync_schema"),
        patch("mcp_telegram.daemon.register_shutdown_handler", return_value=instant_shutdown_event),
        patch("mcp_telegram.daemon.get_sync_db_path"),
        patch("mcp_telegram.daemon._open_sync_db", return_value=mock_conn),
        patch("mcp_telegram.daemon.backfill_fts_index", mock_backfill),
        patch("mcp_telegram.daemon.FullSyncWorker", return_value=mocks["worker"]),
        patch("mcp_telegram.daemon.DeltaSyncWorker", return_value=mocks["delta"]),
        patch("mcp_telegram.daemon.EventHandlerManager", return_value=mocks["handler"]),
        patch("mcp_telegram.daemon.asyncio.start_unix_server", new=AsyncMock(return_value=mock_unix_server)),
        patch("mcp_telegram.daemon.asyncio.to_thread", side_effect=fake_to_thread),
        patch("mcp_telegram.daemon.os.chmod"),
    ):
        asyncio.run(sync_main())

    mock_backfill.assert_called_once_with(mock_conn)


def test_sync_main_cleans_socket_on_shutdown(
    mock_client: MagicMock,
    instant_shutdown_event: asyncio.Event,
    tmp_path,
) -> None:
    """Socket file does not exist after sync_main() exits (cleanup in finally block)."""
    fake_socket_path = tmp_path / "mcp_telegram.sock"
    mocks = _make_standard_mocks(instant_shutdown_event)

    mock_unix_server = MagicMock()
    mock_unix_server.close = MagicMock()
    mock_unix_server.wait_closed = AsyncMock()

    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client),
        patch("mcp_telegram.daemon.ensure_sync_schema"),
        patch("mcp_telegram.daemon.register_shutdown_handler", return_value=instant_shutdown_event),
        patch("mcp_telegram.daemon.get_sync_db_path"),
        patch("mcp_telegram.daemon._open_sync_db"),
        patch("mcp_telegram.daemon.backfill_fts_index", return_value=0),
        patch("mcp_telegram.daemon.get_daemon_socket_path", return_value=fake_socket_path),
        patch("mcp_telegram.daemon.FullSyncWorker", return_value=mocks["worker"]),
        patch("mcp_telegram.daemon.DeltaSyncWorker", return_value=mocks["delta"]),
        patch("mcp_telegram.daemon.EventHandlerManager", return_value=mocks["handler"]),
        patch("mcp_telegram.daemon.asyncio.start_unix_server", new=AsyncMock(return_value=mock_unix_server)),
        patch("mcp_telegram.daemon.os.chmod"),
    ):
        asyncio.run(sync_main())

    assert not fake_socket_path.exists(), "Socket file must not exist after daemon shuts down"


# ---------------------------------------------------------------------------
# R-8: _backfill_total_messages — FloodWait shutdown interrupt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_total_messages_returns_early_when_shutdown_during_flood_wait(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """shutdown_event set during FloodWait sleep → function returns early with filled=0."""
    import sqlite3

    from telethon.errors import FloodWaitError  # type: ignore[import-untyped]

    from mcp_telegram.daemon import _backfill_total_messages

    conn = sqlite3.connect(":memory:")
    from mcp_telegram.sync_db import _apply_migrations

    _apply_migrations(conn)
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, total_messages) VALUES (?, 'synced', NULL)",
        (1001,),
    )
    conn.commit()

    shutdown_event = asyncio.Event()

    err = FloodWaitError(request=None)
    err.seconds = 30

    client = MagicMock()
    client.get_messages = AsyncMock(side_effect=err)

    async def _mock_wait_for(coro: object, timeout: float) -> None:
        # Simulate shutdown completing before flood wait expires.
        import inspect

        if inspect.iscoroutine(coro):
            coro.close()  # prevent "coroutine never awaited" warning
        shutdown_event.set()

    with patch("mcp_telegram.daemon.asyncio.wait_for", side_effect=_mock_wait_for):
        filled = await _backfill_total_messages(client, conn, shutdown_event)

    assert filled == 0, "No rows filled when shutdown fires during FloodWait"


# ---------------------------------------------------------------------------
# _initialize_read_positions — bootstrap task tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_read_positions_fills_null_rows(tmp_path):
    """Given a synced dialog with NULL read_inbox_max_id, bootstrap fills it from API."""
    import sqlite3
    from types import SimpleNamespace
    from unittest.mock import patch

    from mcp_telegram.daemon import _initialize_read_positions
    from mcp_telegram.sync_db import _apply_migrations

    conn = sqlite3.connect(":memory:")
    _apply_migrations(conn)
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, read_inbox_max_id) VALUES (?, 'synced', NULL)",
        (1001,),
    )
    conn.commit()

    shutdown_event = asyncio.Event()

    client = MagicMock()
    client.get_input_entity = AsyncMock(return_value=SimpleNamespace())

    fake_dialog = SimpleNamespace(peer=SimpleNamespace(), read_inbox_max_id=42)
    fake_response = SimpleNamespace(dialogs=[fake_dialog])
    client.side_effect = AsyncMock(return_value=fake_response)

    with patch("mcp_telegram.daemon.telethon_utils.get_peer_id", return_value=1001):
        filled = await _initialize_read_positions(client, conn, shutdown_event)

    assert filled == 1
    row = conn.execute(
        "SELECT read_inbox_max_id FROM synced_dialogs WHERE dialog_id = ?",
        (1001,),
    ).fetchone()
    assert row[0] == 42


@pytest.mark.asyncio
async def test_initialize_read_positions_is_monotonic_vs_live_event(tmp_path):
    """Review-mandated: if a live MessageRead event has already updated the row
    to a higher value before bootstrap arrives, bootstrap MUST NOT regress it.
    """
    import sqlite3

    from mcp_telegram.daemon import _initialize_read_positions  # noqa: F401
    from mcp_telegram.read_state import apply_read_cursor
    from mcp_telegram.sync_db import _apply_migrations

    conn = sqlite3.connect(":memory:")
    _apply_migrations(conn)
    # Simulate a dialog with a high value already set by a live event.
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, read_inbox_max_id) VALUES (?, 'synced', ?)",
        (1001, 100),
    )
    conn.commit()

    # Invoke the shared primitive that bootstrap uses, with a lower value (42 < 100).
    # Verifies MAX(COALESCE(existing, 0), incoming) never regresses — single
    # source of truth for the monotonic-write pattern now lives in read_state.
    apply_read_cursor(conn, 1001, "inbox", 42)
    conn.commit()

    row = conn.execute(
        "SELECT read_inbox_max_id FROM synced_dialogs WHERE dialog_id = ?",
        (1001,),
    ).fetchone()
    assert row[0] == 100, f"Expected 100 (monotonic), got {row[0]} (bootstrap regressed!)"


@pytest.mark.asyncio
async def test_initialize_read_positions_skips_when_no_null_rows(tmp_path):
    """Given no NULL rows (all already bootstrapped), returns 0 without calling client."""
    import sqlite3

    from mcp_telegram.daemon import _initialize_read_positions
    from mcp_telegram.sync_db import _apply_migrations

    conn = sqlite3.connect(":memory:")
    _apply_migrations(conn)
    # Phase 39.3-02: both cursors must be non-NULL for the row to be skipped.
    # The extended SELECT now picks up rows with EITHER cursor NULL.
    conn.execute(
        "INSERT INTO synced_dialogs "
        "(dialog_id, status, read_inbox_max_id, read_outbox_max_id) "
        "VALUES (?, 'synced', ?, ?)",
        (1001, 5, 7),
    )
    conn.commit()

    shutdown_event = asyncio.Event()
    client = MagicMock()
    client.get_input_entity = AsyncMock()

    filled = await _initialize_read_positions(client, conn, shutdown_event)
    assert filled == 0
    client.get_input_entity.assert_not_called()


@pytest.mark.asyncio
async def test_initialize_read_positions_returns_early_when_shutdown_during_flood_wait(tmp_path):
    """shutdown_event set during FloodWait sleep → function returns early with filled=0."""
    import inspect
    import sqlite3
    from unittest.mock import patch

    from telethon.errors import FloodWaitError  # type: ignore[import-untyped]

    from mcp_telegram.daemon import _initialize_read_positions
    from mcp_telegram.sync_db import _apply_migrations

    conn = sqlite3.connect(":memory:")
    _apply_migrations(conn)
    conn.execute(
        "INSERT INTO synced_dialogs (dialog_id, status, read_inbox_max_id) VALUES (?, 'synced', NULL)",
        (1001,),
    )
    conn.commit()

    shutdown_event = asyncio.Event()
    err = FloodWaitError(request=None)
    err.seconds = 30

    client = MagicMock()
    client.get_input_entity = AsyncMock(side_effect=err)

    async def _mock_wait_for(coro, timeout):
        if inspect.iscoroutine(coro):
            coro.close()
        shutdown_event.set()

    with patch("mcp_telegram.daemon.asyncio.wait_for", side_effect=_mock_wait_for):
        filled = await _initialize_read_positions(client, conn, shutdown_event)

    assert filled == 0


@pytest.mark.asyncio
async def test_initialize_read_positions_excludes_non_synced_status(tmp_path):
    """Only rows with status='synced' AND read_inbox_max_id IS NULL are backfilled."""
    import sqlite3
    from types import SimpleNamespace
    from unittest.mock import patch

    from mcp_telegram.daemon import _initialize_read_positions
    from mcp_telegram.sync_db import _apply_migrations

    conn = sqlite3.connect(":memory:")
    _apply_migrations(conn)
    conn.executemany(
        "INSERT INTO synced_dialogs (dialog_id, status, read_inbox_max_id) VALUES (?, ?, NULL)",
        [
            (1001, "synced"),
            (1002, "access_lost"),
            (1003, "not_synced"),
        ],
    )
    conn.commit()

    shutdown_event = asyncio.Event()

    called_with: list[int] = []

    async def _fake_get_input_entity(did):
        called_with.append(did)
        return SimpleNamespace()

    client = MagicMock()
    client.get_input_entity = _fake_get_input_entity

    fake_dialog = SimpleNamespace(peer=SimpleNamespace(), read_inbox_max_id=42)
    client.side_effect = AsyncMock(return_value=SimpleNamespace(dialogs=[fake_dialog]))

    with patch("mcp_telegram.daemon.telethon_utils.get_peer_id", return_value=1001):
        await _initialize_read_positions(client, conn, shutdown_event)

    assert called_with == [1001], f"Only the 'synced' dialog should be queried; got {called_with}"


def test_sync_main_registers_read_positions_bootstrap_after_handler(tmp_path):
    """Review-mandated startup ordering: _initialize_read_positions task must be
    created AFTER handler_manager.register() in the sync_main() source code, so
    no MessageRead events are dropped during the bootstrap window.
    """
    import inspect

    from mcp_telegram import daemon as daemon_mod

    src = inspect.getsource(daemon_mod.sync_main)
    # Find both anchor lines and verify ordering
    register_idx = src.find("handler_manager.register()")
    if register_idx == -1:
        register_idx = src.find(".register()")  # broader match
    bootstrap_idx = src.find('name="initialize_read_positions"')

    assert register_idx != -1, "handler .register() call not found in sync_main"
    assert bootstrap_idx != -1, "initialize_read_positions task not found in sync_main"
    assert register_idx < bootstrap_idx, (
        f"Startup order wrong: handler register at {register_idx}, "
        f"bootstrap at {bootstrap_idx} — handler must register FIRST to avoid "
        f"missed MessageRead events during bootstrap window."
    )
