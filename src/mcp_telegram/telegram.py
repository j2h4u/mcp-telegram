# ruff: noqa: T201  — print() used intentionally for interactive CLI output
from __future__ import annotations

import logging
import time
from functools import cache
from getpass import getpass

from pydantic_settings import BaseSettings, SettingsConfigDict
from telethon import TelegramClient  # type: ignore[import-untyped]
from telethon.errors.rpcerrorlist import SessionPasswordNeededError  # type: ignore[import-untyped]
from telethon.tl.types import User  # type: ignore[import-untyped]
from xdg_base_dirs import xdg_state_home  # type: ignore[import-error]

logger = logging.getLogger(__name__)


class TelegramSettings(BaseSettings):
    """Reads TELEGRAM_API_ID and TELEGRAM_API_HASH from environment or ``.env`` in CWD."""
    model_config = SettingsConfigDict(env_prefix="TELEGRAM_", env_file=".env")

    api_id: str
    api_hash: str


async def connect_to_telegram(api_id: str, api_hash: str, phone_number: str) -> None:
    user_session = create_client(api_id=api_id, api_hash=api_hash)
    await user_session.connect()

    result = await user_session.send_code_request(phone_number)
    code = input("Enter login code: ")
    try:
        await user_session.sign_in(
            phone=phone_number,
            code=code,
            phone_code_hash=result.phone_code_hash,
        )
    except SessionPasswordNeededError:
        password = getpass("Enter 2FA password: ")
        await user_session.sign_in(password=password)

    user = await user_session.get_me()
    if isinstance(user, User):
        print(f"Hey {user.username}! You are connected!")
    else:
        print("Connected!")
    print("You can now use the mcp-telegram server.")


async def logout_from_telegram() -> None:
    user_session = create_client()
    await user_session.connect()
    await user_session.log_out()
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

    ``catch_up=True`` enables Telethon's PTS-based missed-update replay on connect
    (D-05).  The sync-daemon passes ``catch_up=True``; the MCP server never calls
    ``create_client()`` directly (session guard disables it), so there is no
    cache-key collision in practice.
    """
    if api_id is not None and api_hash is not None:
        config = TelegramSettings(api_id=api_id, api_hash=api_hash)
    else:
        config = TelegramSettings()
    state_home = xdg_state_home() / "mcp-telegram"
    state_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    return TelegramClient(
        state_home / session_name,
        config.api_id,
        config.api_hash,
        base_logger="telethon",
        catch_up=catch_up,
    )
