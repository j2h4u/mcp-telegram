import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from functools import cache, partial
from typing import cast

from pydantic_settings import BaseSettings, SettingsConfigDict
from telethon.sessions import SQLiteSession

from .config import McpTelegramConfig
from .flood import flood_wait_kill_switch_status, observe_flood_wait
from .sqlite_writer import SQLiteWriter
from .state import StatePaths, ensure_private_state_dir, telegram_session_lease
from .sync_db import (
    ensure_sync_schema,
    load_account_cooldown_until_utc,
    load_rpc_safety_owners,
    load_rpc_safety_record,
    open_sync_db_reader,
    save_rpc_safety_record,
)
from .telegram_demand import demand_context
from .telegram_rpc import TelegramRpcBudget, TelegramRpcGate, TelegramRpcSafetyStore
from .telegram_rpc_consumers import DemandKind

logger = logging.getLogger(__name__)


def _load_settings() -> TelegramSettings:
    return cast(TelegramSettings, TelegramSettings())  # type: ignore[call-arg]


class TelegramSettings(BaseSettings):
    """Reads TELEGRAM_* settings from environment or ``.env`` in CWD."""

    model_config = SettingsConfigDict(env_prefix="TELEGRAM_", env_file=".env", extra="ignore")

    api_id: str
    api_hash: str


async def logout_from_telegram() -> None:
    """Terminate the active Telegram session and delete the local session file."""
    from .daemon_client import daemon_connection

    async with daemon_connection() as connection:
        response = await connection.request({"method": "logout_account"})
    if response.get("ok") is not True:
        raise RuntimeError("Telegram logout was not confirmed by the owned daemon")
    print("You are now logged out from Telegram.")


@cache
def create_client(  # noqa: PLR0913 - explicit cached client construction dependencies
    api_id: str | None = None,
    api_hash: str | None = None,
    session_name: str = "mcp_telegram_session",
    catch_up: bool = False,
    *,
    config: McpTelegramConfig,
    safety_store: TelegramRpcSafetyStore,
) -> TelegramRpcGate:
    """Return a cached TelegramClient singleton for the given credentials.

    ``@cache`` means the same instance is returned for identical
    ``(api_id, api_hash, session_name, catch_up, config, safety_store)``
    arguments within the process lifetime.
    Callers should use ``connected_client()`` for connection lifecycle management.

    Single-session by design: all tool calls within one process share the same
    authenticated Telegram session. This is intentional for the single-user
    Docker deployment model — there is no per-request session isolation.

    ``catch_up=True`` enables Telethon's PTS-based missed-update replay on connect.
    The sync daemon uses it with a closed event-processing barrier, then opens
    the barrier only after its authenticated account fence succeeds. The MCP
    server never calls ``create_client()`` directly (session guard disables it),
    so there is no cache-key collision in practice.

    Warning: different argument combinations produce distinct cached instances
    that share the same session file path — avoid mixing arguments in one process.
    """
    if api_id is not None and api_hash is not None:
        settings = TelegramSettings(api_id=api_id, api_hash=api_hash)
    else:
        settings = _load_settings()
    state_home = ensure_private_state_dir(config.state.dir, mode=0o700)
    client = TelegramRpcGate(
        state_home / session_name,
        cast(int, settings.api_id),
        cast(str, settings.api_hash),
        base_logger="telethon",
        catch_up=catch_up,
        rpc_budget=TelegramRpcBudget(
            max_calls_per_period=config.telegram_rpc.max_calls_per_period,
            period_seconds=config.telegram_rpc.period_seconds,
        ),
        circuit_status=flood_wait_kill_switch_status,
        fallback_wait_seconds=config.flood_wait.fallback_wait_seconds,
        cooldown_buffer_seconds=config.flood_wait.cooldown_buffer_seconds,
        transient_retry_delays_seconds=config.telegram_rpc.transient_retry_delays_seconds,
        scheduler_policy=config.telegram_rpc.scheduler,
        flood_observer=observe_flood_wait,
        safety_store=safety_store,
    )
    client.configure_auto_resume(enabled=config.flood_wait.auto_resume_enabled)
    return client


def make_rpc_safety_store(conn: sqlite3.Connection, writer: SQLiteWriter) -> TelegramRpcSafetyStore:
    """Use the same serialized safety backend for daemon and stopped auth."""

    async def save(expected: str | None, encoded: str) -> None:
        await writer.run(lambda owned: save_rpc_safety_record(owned, expected, encoded))

    return TelegramRpcSafetyStore(
        partial(load_rpc_safety_record, conn),
        save,
        partial(load_account_cooldown_until_utc, conn),
        partial(load_rpc_safety_owners, conn),
    )


@asynccontextmanager
async def maintenance_client(
    config: McpTelegramConfig, *, acknowledge_unknown_outcome: bool = False, replace_credentials: bool = False
) -> AsyncIterator[TelegramRpcGate]:
    """Stopped-service AUTH shares the daemon's Session and safety ownership."""
    state_home = ensure_private_state_dir(config.state.dir, mode=0o700)
    paths = StatePaths.from_state_dir(state_home)
    async with AsyncExitStack() as resources:
        resources.enter_context(telegram_session_lease(state_home))
        ensure_sync_schema(paths.sync_db_path)
        writer = await SQLiteWriter.open(paths.sync_db_path)
        resources.push_async_callback(writer.aclose)
        reader = open_sync_db_reader(paths.sync_db_path)
        resources.callback(reader.close)
        client = create_client.__wrapped__(config=config, safety_store=make_rpc_safety_store(reader, writer))
        if not isinstance(client.session, SQLiteSession):
            raise RuntimeError("stopped authentication requires the owned SQLite Session")
        client.session.save_entities = False
        resources.push_async_callback(client.close_rpc_scheduler)
        resources.push_async_callback(client.disconnect)
        session_path = state_home / "mcp_telegram_session.session"
        if session_path.exists():
            session_path.chmod(0o600)
        with demand_context(DemandKind.OPERATOR_AUTH):
            await client.begin_authentication(
                acknowledge_unknown_outcome=acknowledge_unknown_outcome, replace_credentials=replace_credentials
            )
            await client.connect()
            yield client
