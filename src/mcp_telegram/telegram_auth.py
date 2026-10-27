"""Stopped-service authentication through the shared Telegram ownership boundary."""

from collections.abc import Awaitable, Callable

from telethon.tl.types import User

from .config import McpTelegramConfig
from .telegram import maintenance_client
from .telegram_rpc import TelegramRpcGate


async def authenticate(
    config: McpTelegramConfig,
    login: Callable[[TelegramRpcGate], Awaitable[None]],
    *,
    acknowledge_unknown_outcome: bool = False,
    replace_credentials: bool = False,
) -> tuple[User, bool]:
    """Return the freshly verified account and whether login was already complete.

    The shared owner controls the Session, authentication intent, account fence and
    RPC admission. Authentication alone never resumes ordinary acquisition.
    """
    async with maintenance_client(
        config,
        acknowledge_unknown_outcome=acknowledge_unknown_outcome,
        replace_credentials=replace_credentials,
    ) as client:
        already_authorized = await client.is_user_authorized()
        if not already_authorized:
            await login(client)
        user = await client.finish_authentication()
    return user, already_authorized
