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
