#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "qrcode",
#   "python-dotenv",
#   "telethon",
# ]
# ///
import asyncio
import datetime
import getpass
import os
import shutil
import sys
import traceback
from io import StringIO
from pathlib import Path
from typing import Any

import qrcode
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import PasswordHashInvalidError, RPCError, SessionPasswordNeededError

load_dotenv()

QR_LEFT_PADDING = " " * 6
QR_BORDER = 4
QR_REFRESH_MARGIN_SECONDS = 20
QR_PROGRESS_BAR_WIDTH = 28


def _load_telegram_credentials() -> tuple[int, str, str]:
    """Load and validate Telegram auth settings from the environment."""
    api_id_raw = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    two_fa_password = os.getenv("TELEGRAM_2FA_PASSWORD", "")

    if not api_id_raw or not api_hash:
        print("TELEGRAM_API_ID or TELEGRAM_API_HASH is not set in .env")
        sys.exit(1)
    if not api_id_raw.isdecimal():
        print("TELEGRAM_API_ID must be an integer")
        sys.exit(1)

    return int(api_id_raw), api_hash, two_fa_password


def qr_to_terminal(data: str) -> str:
    """Render QR data as terminal text."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=1,
        border=QR_BORDER,
    )
    qr.add_data(data)
    qr.make(fit=True)

    f = StringIO()
    qr.print_ascii(invert=True, out=f)
    qr_ascii = f.getvalue().splitlines()
    terminal_width = shutil.get_terminal_size(fallback=(100, 40)).columns
    qr_visible_width = max((len(line) for line in qr_ascii), default=0)
    left_padding = " " * max(len(QR_LEFT_PADDING), (terminal_width - qr_visible_width) // 2)
    return "\n".join(f"{left_padding}{line}" for line in qr_ascii)


def clear_screen() -> None:
    """Clear the terminal screen."""
    os.system("clear" if os.name == "posix" else "cls")


def _qr_lifetime(qr_login: Any) -> int:
    """Return seconds until qr_login expires (minimum 1)."""
    return max(
        1,
        int((qr_login.expires.astimezone(datetime.UTC) - datetime.datetime.now(tz=datetime.UTC)).total_seconds()),
    )


def build_countdown_bar(remaining_seconds: int, total_seconds: int) -> str:
    """Build a text countdown progress bar."""
    if total_seconds <= 0:
        total_seconds = 1

    filled_width = int((remaining_seconds / total_seconds) * QR_PROGRESS_BAR_WIDTH)
    filled_width = max(0, min(QR_PROGRESS_BAR_WIDTH, filled_width))
    empty_width = QR_PROGRESS_BAR_WIDTH - filled_width
    return f"[{'#' * filled_width}{'-' * empty_width}]"


async def _show_account_summary(client: TelegramClient, session_file: Path | None = None) -> None:
    """Print account details for the current authenticated session."""
    me = await client.get_me()
    print(f"Phone: {me.phone}")
    print(f"Name: {me.first_name}")
    if session_file is not None:
        print(f"Session saved to: {session_file}")


def show_2fa_screen(using_env_password: bool) -> None:
    """Show a dedicated screen for the 2FA step."""
    clear_screen()
    print("=" * 50)
    print("TELEGRAM 2FA PASSWORD".center(50))
    print("=" * 50)
    print("\nTelegram accepted the QR code.\n")

    if using_env_password:
        print("Using the password from TELEGRAM_2FA_PASSWORD.")
    else:
        print("Telegram needs the cloud 2FA password to finish login.")
        print("This is not an SMS code or an in-app login code.")

    print("\n" + "=" * 50)
    sys.stdout.flush()


async def complete_2fa_login(client: TelegramClient, two_fa_password: str) -> None:
    """Complete login with a 2FA password."""
    if two_fa_password:
        show_2fa_screen(using_env_password=True)
        await client.sign_in(password=two_fa_password)
        return

    show_2fa_screen(using_env_password=False)

    if not sys.stdin.isatty():
        raise RuntimeError(
            "Entering a 2FA password requires an interactive terminal. "
            "Run this script in a normal TTY or set TELEGRAM_2FA_PASSWORD in .env."
        )

    for attempt in range(3):
        try:
            password = getpass.getpass("Enter Telegram cloud 2FA password: ")
        except EOFError as e:
            raise RuntimeError(
                "Could not read the 2FA password from the terminal. Run this script in an interactive console."
            ) from e

        if not password:
            print("Password must not be empty.")
            continue

        try:
            await client.sign_in(password=password)
            return
        except PasswordHashInvalidError:
            remaining_attempts = 2 - attempt
            if remaining_attempts == 0:
                raise
            print(f"Invalid 2FA password. Attempts remaining: {remaining_attempts}")


async def _run_qr_login(client: TelegramClient, two_fa_password: str, session_file: Path) -> None:
    """Run the QR login loop until authorization completes."""
    qr_login = await client.qr_login()
    qr_total_seconds = _qr_lifetime(qr_login)

    while True:
        expires_at = qr_login.expires.astimezone(datetime.UTC)
        remaining_seconds = max(
            0,
            int((expires_at - datetime.datetime.now(tz=datetime.UTC)).total_seconds()),
        )

        if remaining_seconds <= QR_REFRESH_MARGIN_SECONDS:
            qr_login = await client.qr_login()
            qr_total_seconds = _qr_lifetime(qr_login)
            continue

        qr_ascii = qr_to_terminal(qr_login.url)
        countdown_bar = build_countdown_bar(remaining_seconds, qr_total_seconds)

        clear_screen()
        print("=" * 50)
        print("TELEGRAM QR LOGIN".center(50))
        print("=" * 50)
        print(f"\nQR expires in: {remaining_seconds}s\n")
        print(f"{countdown_bar}\n")
        print(qr_ascii)
        print("\nScan this QR code with Telegram on your phone.")
        print("Waiting for confirmation...\n")
        print("=" * 50)

        try:
            wait_timeout = min(10, max(1, remaining_seconds))
            await qr_login.wait(timeout=wait_timeout)
        except TimeoutError:
            if datetime.datetime.now(tz=datetime.UTC) >= expires_at:
                qr_login = await client.qr_login()
                qr_total_seconds = _qr_lifetime(qr_login)
            continue
        except SessionPasswordNeededError:
            await complete_2fa_login(client, two_fa_password)

        print("\nAuthorization successful.")
        await _show_account_summary(client, session_file)
        return


async def main() -> None:
    api_id, api_hash, two_fa_password = _load_telegram_credentials()

    session_file = Path("database") / "mcp_telegram_session"
    session_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    client = TelegramClient(str(session_file), api_id, api_hash)
    await client.connect()

    try:
        if await client.is_user_authorized():
            print("Already authorized.")
            await _show_account_summary(client)
            return

        print("Requesting a Telegram QR login code...\n")
        await _run_qr_login(client, two_fa_password, session_file)
    except asyncio.CancelledError:
        print("\nCancelled by user.")
    except TimeoutError as e:
        print(f"\n{e}")
    except PasswordHashInvalidError:
        print("\nInvalid 2FA password. Authorization was not completed.")
    except (AttributeError, OSError, RPCError, RuntimeError, TypeError, ValueError) as e:
        print(f"\nError: {e}")
        traceback.print_exc()
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nCancelled by user.")
        sys.exit(0)
