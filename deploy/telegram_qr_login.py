#!/usr/bin/env python3
import argparse
import asyncio
import datetime
import getpass
import os
import shutil
import sqlite3
import sys
from collections.abc import Awaitable
from io import StringIO
from pathlib import Path
from typing import Protocol, cast

import qrcode as qrcode_module
from dotenv import load_dotenv
from telethon.errors import PasswordHashInvalidError, RPCError, SessionPasswordNeededError

from mcp_telegram.config import load_config
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.telegram_auth import authenticate
from mcp_telegram.telegram_rpc import TelegramRpcGate

PRIVATE_STATE_DIR_MODE = 0o700
PRIVATE_SESSION_FILE_MODE = 0o600
QR_LEFT_PADDING = " " * 6
QR_BORDER = 4
QR_REFRESH_MARGIN_SECONDS = 20
QR_PROGRESS_BAR_WIDTH = 28
REAUTHENTICATION_NOTICE = (
    "Local credential replacement does not remotely log out the old authorization, which may remain listed in Telegram."
)


class LoginInputError(RuntimeError):
    """An operator input failure whose message contains no account or credential data."""


class _QrCodeConstants(Protocol):
    ERROR_CORRECT_L: object


class _QrCode(Protocol):
    def add_data(self, data: str) -> None: ...

    def make(self, *, fit: bool = ...) -> None: ...

    def print_ascii(self, *, invert: bool = ..., out: StringIO) -> None: ...


class _QrCodeFactory(Protocol):
    def __call__(
        self,
        *,
        version: int | None,
        error_correction: object,
        box_size: int,
        border: int,
    ) -> _QrCode: ...


class _QrCodeModule(Protocol):
    QRCode: _QrCodeFactory
    constants: _QrCodeConstants


class _QrLogin(Protocol):
    @property
    def expires(self) -> datetime.datetime: ...

    @property
    def url(self) -> str: ...

    def wait(self, timeout: float) -> Awaitable[None]: ...


class _TelegramUser(Protocol):
    phone: str | None
    first_name: str | None


qrcode = cast(_QrCodeModule, qrcode_module)


def _config_path() -> Path:
    return Path.cwd() / "config.toml"


def _ensure_private_state_dir(path: Path) -> Path:
    """Create or tighten the deploy state directory before writing session state."""
    path.mkdir(mode=PRIVATE_STATE_DIR_MODE, parents=True, exist_ok=True)
    path.chmod(PRIVATE_STATE_DIR_MODE)
    if not path.is_dir():
        raise NotADirectoryError(path)
    return path


def _protect_session_file(session_file: Path) -> None:
    """Restrict the Telethon SQLite session file when it exists."""
    sqlite_session_file = session_file.with_suffix(".session")
    if sqlite_session_file.exists():
        sqlite_session_file.chmod(PRIVATE_SESSION_FILE_MODE)


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


def _qr_lifetime(qr_login: _QrLogin) -> int:
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


def _show_account_summary(me: _TelegramUser, session_file: Path | None = None) -> None:
    """Print account details only after the shared gate verifies the account."""
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


async def complete_2fa_login(client: TelegramRpcGate, two_fa_password: str) -> None:
    """Complete login with a 2FA password."""
    if two_fa_password:
        show_2fa_screen(using_env_password=True)
        await client.sign_in(password=two_fa_password)
        return

    show_2fa_screen(using_env_password=False)

    if not sys.stdin.isatty():
        raise LoginInputError(
            "Entering a 2FA password requires an interactive terminal. "
            "Run this script in a normal TTY or set TELEGRAM_2FA_PASSWORD in .env."
        )

    for attempt in range(3):
        try:
            password = getpass.getpass("Enter Telegram cloud 2FA password: ")
        except EOFError as e:
            raise LoginInputError(
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
    raise LoginInputError("No 2FA password was supplied.")


async def _run_qr_login(client: TelegramRpcGate, two_fa_password: str) -> None:
    """Run the QR login loop until authorization completes."""
    qr_login = cast(_QrLogin, await client.qr_login())
    qr_total_seconds = _qr_lifetime(qr_login)

    while True:
        expires_at = qr_login.expires.astimezone(datetime.UTC)
        remaining_seconds = max(
            0,
            int((expires_at - datetime.datetime.now(tz=datetime.UTC)).total_seconds()),
        )

        if remaining_seconds <= QR_REFRESH_MARGIN_SECONDS:
            qr_login = cast(_QrLogin, await client.qr_login())
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
                qr_login = cast(_QrLogin, await client.qr_login())
                qr_total_seconds = _qr_lifetime(qr_login)
            continue
        except SessionPasswordNeededError:
            await complete_2fa_login(client, two_fa_password)

        return


async def main(*, acknowledge_unknown_outcome: bool = False, replace_credentials: bool = False) -> int:
    """Authenticate while the daemon is stopped, without bypassing its RPC gate."""
    try:
        load_dotenv(Path.cwd() / ".env")
        config = load_config(_config_path())
        if replace_credentials:
            print(REAUTHENTICATION_NOTICE)

        async def login(client: TelegramRpcGate) -> None:
            print("Requesting a Telegram QR login code...\n")
            await _run_qr_login(client, os.getenv("TELEGRAM_2FA_PASSWORD", ""))

        user, already_authorized = await authenticate(
            config,
            login,
            acknowledge_unknown_outcome=acknowledge_unknown_outcome,
            replace_credentials=replace_credentials,
        )
        print("Already authorized." if already_authorized else "\nAuthorization successful.")
        session_file = None if already_authorized else config.state.dir / "mcp_telegram_session.session"
        _show_account_summary(cast(_TelegramUser, user), session_file)
        return 0
    except asyncio.CancelledError:
        print("\nCancelled by user.", file=sys.stderr)
        return 130
    except PasswordHashInvalidError:
        print("\nInvalid 2FA password. Authorization was not completed.", file=sys.stderr)
        return 1
    except LoginInputError as error:
        print(f"\n{error}", file=sys.stderr)
        return 1
    except TelegramRpcThrottled as error:
        if error.latched:
            print("\nAccount protection is active. Authorization remains stopped.", file=sys.stderr)
        else:
            print(f"\nAuthorization was deferred for at least {error.retry_after_seconds} seconds.", file=sys.stderr)
        return 1
    except (AttributeError, OSError, RPCError, RuntimeError, TypeError, ValueError, sqlite3.Error) as error:
        # Authentication errors can include credentials or details of an unverified account.
        print(f"\nAuthorization did not complete ({type(error).__name__}).", file=sys.stderr)
        return 1


def cli() -> int:
    """Parse explicit authentication options and report a process status."""
    parser = argparse.ArgumentParser(description="Authenticate with Telegram while the daemon is stopped.")
    parser.add_argument(
        "--reauthenticate",
        action="store_true",
        help=REAUTHENTICATION_NOTICE,
    )
    parser.add_argument(
        "--acknowledge-unknown-outcome",
        action="store_true",
        help="Acknowledge an unresolved prior operation for authentication only; does not resume synchronization.",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(
            main(
                acknowledge_unknown_outcome=cast(bool, args.acknowledge_unknown_outcome),
                replace_credentials=cast(bool, args.reauthenticate),
            )
        )
    except KeyboardInterrupt:
        print("\n\nCancelled by user.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(cli())
