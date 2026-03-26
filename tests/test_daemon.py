from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from mcp_telegram.daemon import sync_main


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
    client = MagicMock()
    client.is_connected.return_value = True  # sync method
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
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
    """sync_main() calls client.connect() and logs a heartbeat INFO line."""
    shutdown_event = asyncio.Event()

    def mock_register_shutdown(conn, loop):  # noqa: ANN001
        # Schedule event set after a tiny delay so one heartbeat fires
        async def _set_after_heartbeat() -> None:
            await asyncio.sleep(0.05)
            shutdown_event.set()

        loop.create_task(_set_after_heartbeat())
        return shutdown_event

    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client),
        patch("mcp_telegram.daemon.ensure_sync_schema"),
        patch("mcp_telegram.daemon.register_shutdown_handler", side_effect=mock_register_shutdown),
        patch("mcp_telegram.daemon.get_sync_db_path"),
        patch("mcp_telegram.daemon._open_sync_db"),
        patch("mcp_telegram.daemon.HEARTBEAT_INTERVAL_S", 0.01),
        caplog.at_level(logging.INFO, logger="mcp_telegram.daemon"),
    ):
        asyncio.run(sync_main())

    mock_client.connect.assert_called_once()
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
    ):
        asyncio.run(sync_main())

    mock_reg.assert_called_once()
    call_args = mock_reg.call_args
    assert call_args[0][0] is mock_conn, "register_shutdown_handler must receive the open DB connection"


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
    ):
        asyncio.run(sync_main())

    mock_client.disconnect.assert_called_once()


def test_sync_main_heartbeat_logs_connection_state(
    mock_client: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Heartbeat INFO log includes 'connected=True' (or connection state)."""
    shutdown_event = asyncio.Event()

    def mock_register_shutdown(conn, loop):  # noqa: ANN001
        async def _set_after_heartbeat() -> None:
            await asyncio.sleep(0.05)
            shutdown_event.set()

        loop.create_task(_set_after_heartbeat())
        return shutdown_event

    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client),
        patch("mcp_telegram.daemon.ensure_sync_schema"),
        patch("mcp_telegram.daemon.register_shutdown_handler", side_effect=mock_register_shutdown),
        patch("mcp_telegram.daemon.get_sync_db_path"),
        patch("mcp_telegram.daemon._open_sync_db"),
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

    def mock_register_shutdown(conn, loop):  # noqa: ANN001
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
    worker_instance.bootstrap_dms = AsyncMock(
        side_effect=lambda: call_order.append("bootstrap_dms") or 0
    )
    worker_instance.process_one_batch = AsyncMock(return_value=True)

    worker_class = MagicMock(return_value=worker_instance)

    handler_instance = MagicMock()
    handler_instance.register = MagicMock(
        side_effect=lambda: call_order.append("register")
    )
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
        patch("mcp_telegram.daemon.FullSyncWorker", worker_class),
        patch("mcp_telegram.daemon.EventHandlerManager", handler_class),
    ):
        asyncio.run(sync_main())

    assert "register" in call_order, "handler_manager.register() was never called"
    assert "bootstrap_dms" in call_order, "worker.bootstrap_dms() was never called"
    assert call_order.index("register") < call_order.index("bootstrap_dms"), (
        f"handler_manager.register() must be called BEFORE bootstrap_dms(); "
        f"got order: {call_order}"
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

    mock_client.disconnect = AsyncMock(
        side_effect=lambda: call_order.append("disconnect")
    )

    worker_instance = MagicMock()
    worker_instance.bootstrap_dms = AsyncMock(return_value=0)
    worker_instance.process_one_batch = AsyncMock(return_value=True)

    worker_class = MagicMock(return_value=worker_instance)

    handler_instance = MagicMock()
    handler_instance.register = MagicMock()
    handler_instance.unregister = MagicMock(
        side_effect=lambda: call_order.append("unregister")
    )
    handler_instance.refresh_synced_dialogs = MagicMock()
    handler_instance.run_dm_gap_scan = AsyncMock(return_value=0)

    handler_class = MagicMock(return_value=handler_instance)

    with (
        patch("mcp_telegram.daemon.create_client", return_value=mock_client),
        patch("mcp_telegram.daemon.ensure_sync_schema"),
        patch("mcp_telegram.daemon.register_shutdown_handler", return_value=instant_shutdown_event),
        patch("mcp_telegram.daemon.get_sync_db_path"),
        patch("mcp_telegram.daemon._open_sync_db"),
        patch("mcp_telegram.daemon.FullSyncWorker", worker_class),
        patch("mcp_telegram.daemon.EventHandlerManager", handler_class),
    ):
        asyncio.run(sync_main())

    assert "unregister" in call_order, "handler_manager.unregister() was never called"
    assert "disconnect" in call_order, "client.disconnect() was never called"
    assert call_order.index("unregister") < call_order.index("disconnect"), (
        f"handler_manager.unregister() must be called BEFORE client.disconnect(); "
        f"got order: {call_order}"
    )
