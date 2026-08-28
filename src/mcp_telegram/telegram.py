import logging
from functools import cache
from typing import cast

from pydantic_settings import BaseSettings, SettingsConfigDict
from telethon import TelegramClient  # type: ignore[import-untyped]

from .config import McpTelegramConfig, load_config
from .flood import flood_wait_kill_switch_status, observe_flood_wait
from .state import ensure_private_state_dir
from .telegram_rpc import TelegramRpcBudget, TelegramRpcGate

logger = logging.getLogger(__name__)


def _load_settings() -> TelegramSettings:
    return cast(TelegramSettings, TelegramSettings())  # type: ignore[call-arg]


class TelegramSettings(BaseSettings):
    """Reads TELEGRAM_* settings from environment or ``.env`` in CWD."""

    model_config = SettingsConfigDict(env_prefix="TELEGRAM_", env_file=".env")

    api_id: str
    api_hash: str


async def logout_from_telegram() -> None:
    """Terminate the active Telegram session and delete the local session file."""
    client = create_maintenance_client()
    await client.connect()
    await client.log_out()
    print("You are now logged out from Telegram.")


@cache
def create_client(
    api_id: str | None = None,
    api_hash: str | None = None,
    session_name: str = "mcp_telegram_session",
    catch_up: bool = False,
    *,
    config: McpTelegramConfig,
) -> TelegramRpcGate:
    """Return a cached TelegramClient singleton for the given credentials.

    ``@cache`` means the same instance is returned for identical
    ``(api_id, api_hash, session_name, catch_up, config)`` arguments within the process lifetime.
    Callers should use ``connected_client()`` for connection lifecycle management.

    Single-session by design: all tool calls within one process share the same
    authenticated Telegram session. This is intentional for the single-user
    Docker deployment model — there is no per-request session isolation.

    ``catch_up=True`` enables Telethon's PTS-based missed-update replay on connect.
    The sync-daemon passes ``catch_up=True``; the MCP server never calls
    ``create_client()`` directly (session guard disables it), so there is no
    cache-key collision in practice.

    Warning: different argument combinations produce distinct cached instances
    that share the same session file path — avoid mixing arguments in one process.
    """
    if api_id is not None and api_hash is not None:
        settings = TelegramSettings(api_id=api_id, api_hash=api_hash)
    else:
        settings = _load_settings()
    state_home = ensure_private_state_dir(config.state.dir, mode=0o700)
    return TelegramRpcGate(
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
        flood_observer=observe_flood_wait,
    )


@cache
def create_maintenance_client(
    api_id: str | None = None,
    api_hash: str | None = None,
    session_name: str = "mcp_telegram_session",
) -> TelegramClient:
    """Return a plain Telethon client for explicit maintenance operations."""
    if api_id is not None and api_hash is not None:
        settings = TelegramSettings(api_id=api_id, api_hash=api_hash)
    else:
        settings = _load_settings()
    state_home = ensure_private_state_dir(load_config().state.dir, mode=0o700)
    return TelegramClient(
        state_home / session_name,
        cast(int, settings.api_id),
        cast(str, settings.api_hash),
        base_logger="telethon",
        auto_reconnect=True,
    )
