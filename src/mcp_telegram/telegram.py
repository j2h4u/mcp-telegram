import logging
from functools import cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from telethon import TelegramClient  # type: ignore[import-untyped]
from xdg_base_dirs import xdg_state_home  # type: ignore[import-error]

logger = logging.getLogger(__name__)


class TelegramSettings(BaseSettings):
    """Reads TELEGRAM_* settings from environment or ``.env`` in CWD."""

    model_config = SettingsConfigDict(env_prefix="TELEGRAM_", env_file=".env")

    api_id: str
    api_hash: str
    flood_sleep_threshold_seconds: int = 60
    """Telethon auto-sleeps FloodWaits whose duration is <= this value (seconds).

    Short floods (observed 22-27s in production, phase-53 finding) are handled
    transparently by Telethon: it sleeps the coroutine and pre-emptively gates
    further same-CONSTRUCTOR_ID requests via ``_flood_waited_requests``.
    Only floods *above* this threshold raise ``FloodWaitError`` to our code, where
    the durable ``*_next_retry_at`` backoff takes over.

    60 seconds: covers observed short floods with margin; a coroutine sleeping
    up to 60s is acceptable for background sync work; floods above this indicate
    a serious rate-limit that warrants a full durable defer rather than an inline
    sleep.  Override via TELEGRAM_FLOOD_SLEEP_THRESHOLD_SECONDS env var.
    """


async def logout_from_telegram() -> None:
    """Terminate the active Telegram session and delete the local session file."""
    client = create_client()
    await client.connect()
    await client.log_out()
    print("You are now logged out from Telegram.")


@cache
def create_client(
    api_id: str | None = None,
    api_hash: str | None = None,
    session_name: str = "mcp_telegram_session",
    catch_up: bool = False,
) -> TelegramClient:
    """Return a cached TelegramClient singleton for the given credentials.

    ``@cache`` means the same instance is returned for identical
    ``(api_id, api_hash, session_name, catch_up)`` arguments within the process lifetime.
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
        settings = TelegramSettings()
    state_home = xdg_state_home() / "mcp-telegram"
    state_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    return TelegramClient(
        state_home / session_name,
        settings.api_id,
        settings.api_hash,
        base_logger="telethon",
        catch_up=catch_up,
        # flood_sleep_threshold: Telethon auto-sleeps short floods (<=threshold) and
        # pre-emptively gates same-CONSTRUCTOR_ID requests, yielding the asyncio loop
        # during the sleep.  Only floods > threshold raise FloodWaitError to our durable
        # *_next_retry_at backoff handlers (delta_sync, activity_sync, sweep helpers).
        # Default 60s covers observed 22-27s production floods (phase-53 finding) with
        # margin; above this a flood is serious enough to warrant a full durable defer.
        # Tunable via TELEGRAM_FLOOD_SLEEP_THRESHOLD_SECONDS.
        flood_sleep_threshold=settings.flood_sleep_threshold_seconds,
    )
