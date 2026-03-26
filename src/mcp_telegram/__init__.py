import asyncio
from typing import Annotated

from typer import Context, Option, Typer

app = Typer()


@app.callback(invoke_without_command=True)
def _run(ctx: Context) -> None:
    if ctx.invoked_subcommand is None:
        # This will run if no subcommand is specified
        run()


@app.command()
def sign_in(
    api_id: Annotated[str, Option(help="Telegram API id")],
    api_hash: Annotated[str, Option(help="Telegram API hash")],
    phone_number: Annotated[str, Option(help="Phone number with country code")],
) -> None:
    """Connect to Telegram API."""
    from .telegram import connect_to_telegram

    asyncio.run(connect_to_telegram(api_id, api_hash, phone_number))


@app.command()
def run() -> None:
    """Run the mcp-telegram server."""
    from .server import run_mcp_server

    asyncio.run(run_mcp_server())


@app.command()
def logout() -> None:
    """Logout from Telegram API."""
    from .telegram import logout_from_telegram

    asyncio.run(logout_from_telegram())


@app.command()
def sync() -> None:
    """Run the sync daemon (owns TelegramClient exclusively)."""
    import logging
    import sys

    from .daemon import sync_main

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )
    asyncio.run(sync_main())
