"""The QR operator UI cannot bypass the shared authentication owner or account fence."""

import asyncio
import datetime
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
from deploy import telegram_qr_login
from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError
from telethon.tl.types import User

from mcp_telegram import telegram_auth
from mcp_telegram.config import McpTelegramConfig, StateConfig
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.telegram_rpc import TelegramRpcGate


@dataclass
class _AuthGate:
    authorized: bool
    events: list[str] = field(default_factory=list)
    failure: Exception | None = None

    async def is_user_authorized(self) -> bool:
        self.events.append("check_authorized")
        return self.authorized

    async def finish_authentication(self) -> User:
        self.events.append("verify")
        if self.failure is not None:
            raise self.failure
        return User(id=42, first_name="Verified account", phone="12345")


def _install_owner(
    monkeypatch: pytest.MonkeyPatch,
    config: McpTelegramConfig,
    gate: _AuthGate,
) -> list[tuple[bool, bool]]:
    options: list[tuple[bool, bool]] = []

    @asynccontextmanager
    async def owner(
        actual_config: McpTelegramConfig,
        *,
        acknowledge_unknown_outcome: bool = False,
        replace_credentials: bool = False,
    ) -> AsyncIterator[TelegramRpcGate]:
        assert actual_config is config
        options.append((acknowledge_unknown_outcome, replace_credentials))
        gate.events.append("acquire")
        try:
            yield cast(TelegramRpcGate, gate)
        finally:
            gate.events.append("release")

    monkeypatch.setattr(telegram_auth, "maintenance_client", owner)
    return options


@pytest.mark.parametrize("authorized", [False, True])
async def test_auth_verifies_before_releasing_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, authorized: bool
) -> None:
    config = McpTelegramConfig(state=StateConfig(dir=tmp_path))
    gate = _AuthGate(authorized)
    acknowledgements = _install_owner(monkeypatch, config, gate)

    async def login(client: TelegramRpcGate) -> None:
        assert client is gate
        gate.events.append("login")

    user, already_authorized = await telegram_auth.authenticate(config, login)

    assert user.id == 42
    assert already_authorized is authorized
    assert acknowledgements == [(False, False)]
    assert gate.events == ["acquire", "check_authorized", *([] if authorized else ["login"]), "verify", "release"]


@pytest.mark.parametrize("acknowledge", [False, True])
@pytest.mark.parametrize("replace_credentials", [False, True])
async def test_auth_options_are_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, acknowledge: bool, replace_credentials: bool
) -> None:
    config = McpTelegramConfig(state=StateConfig(dir=tmp_path))
    gate = _AuthGate(True)
    acknowledgements = _install_owner(monkeypatch, config, gate)
    login = AsyncMock()

    await telegram_auth.authenticate(
        config, login, acknowledge_unknown_outcome=acknowledge, replace_credentials=replace_credentials
    )

    assert acknowledgements == [(acknowledge, replace_credentials)]
    login.assert_not_awaited()
    assert gate.events == ["acquire", "check_authorized", "verify", "release"]


@pytest.mark.parametrize("authorized", [False, True])
async def test_unverified_account_never_returns_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, authorized: bool
) -> None:
    config = McpTelegramConfig(state=StateConfig(dir=tmp_path))
    gate = _AuthGate(authorized, failure=RuntimeError("wrong account"))
    _install_owner(monkeypatch, config, gate)

    with pytest.raises(RuntimeError, match="wrong account"):
        await telegram_auth.authenticate(config, AsyncMock())

    assert gate.events[-2:] == ["verify", "release"]


@pytest.mark.parametrize("failure", [RuntimeError("held"), asyncio.CancelledError()])
async def test_qr_failure_releases_owner_without_verifying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    config = McpTelegramConfig(state=StateConfig(dir=tmp_path))
    gate = _AuthGate(False)
    _install_owner(monkeypatch, config, gate)

    with pytest.raises(type(failure)):
        await telegram_auth.authenticate(config, AsyncMock(side_effect=failure))

    assert gate.events == ["acquire", "check_authorized", "release"]


async def test_owner_refusal_never_starts_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = McpTelegramConfig(state=StateConfig(dir=tmp_path))
    login = AsyncMock()

    @asynccontextmanager
    async def busy_owner(*_args: object, **_kwargs: object) -> AsyncIterator[TelegramRpcGate]:
        raise RuntimeError("Session already owned")
        yield  # pragma: no cover

    monkeypatch.setattr(telegram_auth, "maintenance_client", busy_owner)

    with pytest.raises(RuntimeError, match="already owned"):
        await telegram_auth.authenticate(config, login)

    login.assert_not_awaited()


@pytest.fixture
def deploy_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text(
        f'[state]\ndir = "{tmp_path / "state"}"\n[telegram_rpc]\ntransport_contract_version = 1\n', encoding="utf-8"
    )
    monkeypatch.setattr(telegram_qr_login, "load_dotenv", lambda _path: None)
    monkeypatch.delenv("TELEGRAM_2FA_PASSWORD", raising=False)
    return tmp_path


@pytest.mark.parametrize("authorized", [False, True])
async def test_deploy_prints_account_only_after_verified_owner_exit(
    deploy_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], authorized: bool
) -> None:
    config = telegram_qr_login.load_config(deploy_dir / "config.toml")
    gate = _AuthGate(authorized)
    _install_owner(monkeypatch, config, gate)
    monkeypatch.setattr(telegram_qr_login, "load_config", lambda _path: config)

    async def login(_client: TelegramRpcGate, _password: str) -> None:
        gate.events.append("login")
        assert "Authorization successful" not in capsys.readouterr().out

    monkeypatch.setattr(telegram_qr_login, "_run_qr_login", login)
    assert await telegram_qr_login.main() == 0

    assert gate.events[-2:] == ["verify", "release"]
    output = capsys.readouterr().out
    assert "Phone: 12345" in output
    assert ("Already authorized." if authorized else "Authorization successful.") in output
    assert not (deploy_dir / "state").exists()


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("unverified-phone secret"),
        sqlite3.DatabaseError("unverified-phone secret"),
        asyncio.CancelledError(),
    ],
)
async def test_deploy_failure_is_nonzero_and_redacted(
    deploy_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: BaseException
) -> None:
    monkeypatch.setattr(telegram_qr_login, "authenticate", AsyncMock(side_effect=failure))

    assert await telegram_qr_login.main() != 0

    output = capsys.readouterr()
    assert "unverified-phone" not in output.err
    assert "secret" not in output.err
    assert output.err
    assert not output.out
    assert not (deploy_dir / "state").exists()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (TelegramRpcThrottled(latched=True, detail="sensitive detail"), "Account protection is active"),
        (TelegramRpcThrottled(retry_after_seconds=17, detail="sensitive detail"), "at least 17 seconds"),
    ],
)
async def test_deploy_reports_safe_protection_state(
    deploy_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: TelegramRpcThrottled,
    expected: str,
) -> None:
    monkeypatch.setattr(telegram_qr_login, "authenticate", AsyncMock(side_effect=failure))

    assert await telegram_qr_login.main() == 1

    output = capsys.readouterr()
    assert expected in output.err
    assert "sensitive" not in output.err
    assert not output.out
    assert not (deploy_dir / "state").exists()


async def test_deploy_retains_safe_input_guidance(
    deploy_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        telegram_qr_login,
        "authenticate",
        AsyncMock(side_effect=telegram_qr_login.LoginInputError("Run this script in an interactive terminal.")),
    )

    assert await telegram_qr_login.main() == 1

    assert "interactive terminal" in capsys.readouterr().err
    assert not (deploy_dir / "state").exists()


@pytest.mark.parametrize("acknowledge", [False, True])
@pytest.mark.parametrize("replace_credentials", [False, True])
async def test_deploy_loads_full_policy_and_forwards_independent_options(
    deploy_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    acknowledge: bool,
    replace_credentials: bool,
) -> None:
    (deploy_dir / "config.toml").write_text(
        f'[state]\ndir = "{deploy_dir / "state"}"\n'
        "[telegram_rpc]\ntransport_contract_version = 1\nmax_calls_per_period = 7\n",
        encoding="utf-8",
    )
    authenticate = AsyncMock(return_value=(User(id=42), True))
    monkeypatch.setattr(telegram_qr_login, "authenticate", authenticate)

    assert (
        await telegram_qr_login.main(acknowledge_unknown_outcome=acknowledge, replace_credentials=replace_credentials)
        == 0
    )

    arguments = authenticate.await_args
    assert arguments is not None
    assert arguments.kwargs == {
        "acknowledge_unknown_outcome": acknowledge,
        "replace_credentials": replace_credentials,
    }
    config = cast(McpTelegramConfig, arguments.args[0])
    assert config.telegram_rpc.max_calls_per_period == 7
    assert (telegram_qr_login.REAUTHENTICATION_NOTICE in capsys.readouterr().out) is replace_credentials


async def test_invalid_full_config_stops_before_auth(deploy_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (deploy_dir / "config.toml").write_text(
        f'[state]\ndir = "{deploy_dir / "state"}"\n'
        "[telegram_rpc]\ntransport_contract_version = 1\nmax_calls_per_period = 0\n",
        encoding="utf-8",
    )
    authenticate = AsyncMock()
    monkeypatch.setattr(telegram_qr_login, "authenticate", authenticate)

    assert await telegram_qr_login.main() == 1

    authenticate.assert_not_awaited()
    assert not (deploy_dir / "state").exists()


@pytest.mark.parametrize("acknowledge", [False, True])
@pytest.mark.parametrize("replace_credentials", [False, True])
def test_cli_options_are_opt_in_and_preserve_failure_status(
    monkeypatch: pytest.MonkeyPatch, acknowledge: bool, replace_credentials: bool
) -> None:
    main = AsyncMock(return_value=1)
    monkeypatch.setattr(telegram_qr_login, "main", main)
    monkeypatch.setattr(
        telegram_qr_login.sys,
        "argv",
        [
            "telegram_qr_login.py",
            *(["--acknowledge-unknown-outcome"] if acknowledge else []),
            *(["--reauthenticate"] if replace_credentials else []),
        ],
    )

    assert telegram_qr_login.cli() == 1

    main.assert_awaited_once_with(acknowledge_unknown_outcome=acknowledge, replace_credentials=replace_credentials)


def test_cli_help_explains_local_replacement(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    main = AsyncMock()
    monkeypatch.setattr(telegram_qr_login, "main", main)
    monkeypatch.setattr(telegram_qr_login.sys, "argv", ["telegram_qr_login.py", "--help"])

    with pytest.raises(SystemExit) as result:
        telegram_qr_login.cli()

    assert result.value.code == 0
    output = " ".join(capsys.readouterr().out.split())
    assert "--reauthenticate" in output
    assert telegram_qr_login.REAUTHENTICATION_NOTICE in output
    assert "does not resume synchronization" in output
    main.assert_not_awaited()


@dataclass
class _QrLogin:
    wait: Callable[..., Awaitable[None]]
    expires: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=60)
    )
    url: str = "test-qr"


@dataclass
class _QrClient:
    qr: _QrLogin | None = None
    passwords: list[str] = field(default_factory=list)
    password_error: Exception | None = None
    qr_requests: int = 0

    async def qr_login(self) -> _QrLogin:
        assert self.qr is not None
        self.qr_requests += 1
        return self.qr

    async def sign_in(self, *, password: str) -> None:
        self.passwords.append(password)
        if self.password_error is not None:
            raise self.password_error


async def test_qr_2fa_defers_success_until_final_verification(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    qr = _QrLogin(AsyncMock(side_effect=SessionPasswordNeededError(request=None)))
    client = _QrClient(qr=qr)
    monkeypatch.setattr(telegram_qr_login, "clear_screen", lambda: None)
    monkeypatch.setattr(telegram_qr_login, "qr_to_terminal", lambda _url: "QR")

    await telegram_qr_login._run_qr_login(cast(TelegramRpcGate, client), "2fa-password")

    assert client.passwords == ["2fa-password"]
    assert "Authorization successful" not in capsys.readouterr().out


async def test_qr_gate_refusal_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _QrClient(qr=_QrLogin(AsyncMock(side_effect=TelegramRpcThrottled(latched=True))))
    monkeypatch.setattr(telegram_qr_login, "clear_screen", lambda: None)
    monkeypatch.setattr(telegram_qr_login, "qr_to_terminal", lambda _url: "QR")

    with pytest.raises(TelegramRpcThrottled):
        await telegram_qr_login._run_qr_login(cast(TelegramRpcGate, client), "")

    assert client.qr_requests == 1
    assert client.passwords == []


async def test_2fa_empty_inputs_do_not_fall_through(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _QrClient()
    monkeypatch.setattr(telegram_qr_login, "show_2fa_screen", lambda **_kwargs: None)
    monkeypatch.setattr(telegram_qr_login.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(telegram_qr_login.getpass, "getpass", lambda _prompt: "")

    with pytest.raises(RuntimeError, match="No 2FA password"):
        await telegram_qr_login.complete_2fa_login(cast(TelegramRpcGate, client), "")

    assert client.passwords == []


async def test_2fa_wrong_password_stops_after_three_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _QrClient(password_error=PasswordHashInvalidError(request=None))
    monkeypatch.setattr(telegram_qr_login, "show_2fa_screen", lambda **_kwargs: None)
    monkeypatch.setattr(telegram_qr_login.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(telegram_qr_login.getpass, "getpass", lambda _prompt: "wrong-password")

    with pytest.raises(PasswordHashInvalidError):
        await telegram_qr_login.complete_2fa_login(cast(TelegramRpcGate, client), "")

    assert client.passwords == ["wrong-password"] * 3
