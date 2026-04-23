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
from io import StringIO
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError
import qrcode

# Загружаем .env
load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
PHONE = os.getenv("TELEGRAM_PHONE", "")
TWO_FA_PASSWORD = os.getenv("TELEGRAM_2FA_PASSWORD", "")
QR_LEFT_PADDING = " " * 6
QR_BORDER = 4
QR_REFRESH_MARGIN_SECONDS = 20
QR_PROGRESS_BAR_WIDTH = 28

def qr_to_terminal(data: str) -> str:
    """Конвертирует QR данные в терминальный QR код."""
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

def clear_screen():
    """Очищает экран терминала"""
    os.system('clear' if os.name == 'posix' else 'cls')


def _qr_lifetime(qr_login) -> int:
    """Return seconds until qr_login expires (minimum 1)."""
    return max(
        1,
        int(
            (
                qr_login.expires.astimezone(datetime.timezone.utc)
                - datetime.datetime.now(tz=datetime.timezone.utc)
            ).total_seconds()
        ),
    )


def build_countdown_bar(remaining_seconds: int, total_seconds: int) -> str:
    """Строит текстовый progress bar обратного отсчета."""
    if total_seconds <= 0:
        total_seconds = 1

    filled_width = int((remaining_seconds / total_seconds) * QR_PROGRESS_BAR_WIDTH)
    filled_width = max(0, min(QR_PROGRESS_BAR_WIDTH, filled_width))
    empty_width = QR_PROGRESS_BAR_WIDTH - filled_width
    return f"[{'#' * filled_width}{'-' * empty_width}]"


def show_2fa_screen(using_env_password: bool) -> None:
    """Показывает отдельный экран для шага 2FA."""
    clear_screen()
    print("=" * 50)
    print("🔐 TELEGRAM 2FA PASSWORD".center(50))
    print("=" * 50)
    print("\n✅ QR-код принят Telegram.\n")

    if using_env_password:
        print("🔒 Используем пароль из TELEGRAM_2FA_PASSWORD.")
    else:
        print("🔒 Для завершения входа нужен облачный пароль Telegram 2FA.")
        print("   Это не SMS-код и не код из приложения.")

    print("\n" + "=" * 50)
    sys.stdout.flush()


async def complete_2fa_login(client: TelegramClient) -> None:
    """Завершает авторизацию через пароль 2FA."""
    if TWO_FA_PASSWORD:
        show_2fa_screen(using_env_password=True)
        await client.sign_in(password=TWO_FA_PASSWORD)
        return

    show_2fa_screen(using_env_password=False)

    if not sys.stdin.isatty():
        raise RuntimeError(
            "Для ввода 2FA-пароля нужен интерактивный терминал. "
            "Запусти скрипт в обычном TTY или задай TELEGRAM_2FA_PASSWORD в .env."
        )

    for attempt in range(3):
        try:
            password = getpass.getpass("Введите облачный пароль Telegram 2FA: ")
        except EOFError as e:
            raise RuntimeError(
                "Не удалось прочитать 2FA-пароль из терминала. "
                "Запусти скрипт в интерактивной консоли."
            ) from e

        if not password:
            print("⚠️  Пароль не должен быть пустым.")
            continue

        try:
            await client.sign_in(password=password)
            return
        except PasswordHashInvalidError:
            remaining_attempts = 2 - attempt
            if remaining_attempts == 0:
                raise
            print(f"⚠️  Неверный пароль 2FA. Осталось попыток: {remaining_attempts}")


async def main():
    if not API_ID or not API_HASH:
        print("❌ TELEGRAM_API_ID или TELEGRAM_API_HASH не установлены в .env")
        sys.exit(1)

    if not PHONE:
        phone = input("📱 Введите номер телефона Telegram (с кодом страны, например +79...): ")
    else:
        phone = PHONE

    # Используем session в текущей папке
    session_file = Path("telegram_session")

    client = TelegramClient(str(session_file), API_ID, API_HASH)
    await client.connect()

    try:
        # Проверяем, уже ли авторизованы
        if await client.is_user_authorized():
            print("✅ Уже авторизован!")
            me = await client.get_me()
            print(f"📞 Номер: {me.phone}")
            print(f"👤 Имя: {me.first_name}")
            await client.disconnect()
            return

        print("🔐 Запрашиваем QR код для авторизации...\n")

        # Инициируем QR логин
        qr_login = await client.qr_login()
        qr_total_seconds = _qr_lifetime(qr_login)

        while True:
            expires_at = qr_login.expires.astimezone(datetime.timezone.utc)
            now = datetime.datetime.now(tz=datetime.timezone.utc)
            remaining_seconds = max(0, int((expires_at - now).total_seconds()))

            if remaining_seconds <= QR_REFRESH_MARGIN_SECONDS:
                qr_login = await client.qr_login()
                qr_total_seconds = _qr_lifetime(qr_login)
                continue

            # Генерируем QR с URL
            qr_ascii = qr_to_terminal(qr_login.url)
            countdown_bar = build_countdown_bar(remaining_seconds, qr_total_seconds)

            clear_screen()
            print("=" * 50)
            print("🔐 TELEGRAM QR LOGIN".center(50))
            print("=" * 50)
            print(f"\n⏱️  Время истечения QR: {remaining_seconds}с\n")
            print(f"{countdown_bar}\n")
            print(qr_ascii)
            print("\n📱 Отсканируй QR код телефоном Telegram")
            print("⏳ Ожидаем подтверждения...\n")
            print("=" * 50)

            try:
                wait_timeout = min(10, max(1, remaining_seconds))
                await qr_login.wait(timeout=wait_timeout)
            except asyncio.TimeoutError:
                if datetime.datetime.now(tz=datetime.timezone.utc) >= expires_at:
                    qr_login = await client.qr_login()
                    qr_total_seconds = _qr_lifetime(qr_login)
                continue
            except SessionPasswordNeededError:
                await complete_2fa_login(client)

            print("\n✅ Успешно авторизован!")
            me = await client.get_me()
            print(f"📞 Номер: {me.phone}")
            print(f"👤 Имя: {me.first_name}")
            print(f"💾 Сессия сохранена в: {session_file}")
            await client.disconnect()
            return

    except asyncio.CancelledError:
        print("\n⛔ Отменено пользователем")
    except TimeoutError as e:
        print(f"\n❌ {e}")
    except PasswordHashInvalidError:
        print("\n❌ Пароль 2FA неверный. Авторизация не завершена.")
    except Exception as e:
        print(f"\n❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await client.disconnect()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⛔ Отменено пользователем")
        sys.exit(0)
