from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from mcp_telegram import app

runner = CliRunner()


class _RecoveryConnection:
    def __init__(self, status: dict[str, object], recovery: dict[str, object] | None) -> None:
        self.status = status
        self.recovery = recovery or {}
        self.status_calls = 0
        self.recovery_calls = 0
        self.expected_generation: str | None = None
        self.acknowledged_unknown_outcome = False

    async def __aenter__(self) -> _RecoveryConnection:
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def get_account_protection(self) -> dict[str, object]:
        self.status_calls += 1
        return self.status

    async def recover_account(
        self, *, expected_generation: str, acknowledge_unknown_outcome: bool
    ) -> dict[str, object]:
        self.recovery_calls += 1
        self.expected_generation = expected_generation
        self.acknowledged_unknown_outcome = acknowledge_unknown_outcome
        return self.recovery


def _daemon(status: dict[str, object], recovery: dict[str, object] | None = None):
    conn = _RecoveryConnection(status, recovery)
    return patch("mcp_telegram.daemon_client.daemon_connection", return_value=conn), conn


def test_missing_ack_does_not_contact_daemon() -> None:
    with patch("mcp_telegram.daemon_client.daemon_connection") as connection:
        result = runner.invoke(app, ["recover-account"])
    assert result.exit_code != 0
    assert "--acknowledge-unknown-outcome" in result.output
    connection.assert_not_called()


def test_active_generation_authorizes_one_cas_recovery() -> None:
    patcher, conn = _daemon(
        {"ok": True, "data": {"account_protection": {"generation": "opaque-7", "known_retry_until_utc": None}}},
        {"ok": True, "data": {"recovery": "authorized", "restart_scheduled": True}},
    )
    with patcher:
        result = runner.invoke(app, ["recover-account", "--acknowledge-unknown-outcome"])
    assert result.exit_code == 0, result.output
    assert "authorized" in result.output and "restart scheduled" in result.output
    assert "recovered" not in result.output.lower()
    assert conn.status_calls == 1
    assert conn.recovery_calls == 1
    assert conn.expected_generation == "opaque-7"
    assert conn.acknowledged_unknown_outcome is True


def test_healthy_or_malformed_status_does_not_attempt_recovery() -> None:
    for status in (
        {"ok": True, "data": {"account_protection": None}},
        {"ok": True, "data": {"account_protection": {"generation": ""}}},
        {"ok": True, "data": None},
        {"ok": False, "error": "status_unavailable"},
    ):
        patcher, conn = _daemon(status)
        with patcher:
            result = runner.invoke(app, ["recover-account", "--acknowledge-unknown-outcome"])
        assert result.exit_code != 0
        assert conn.recovery_calls == 0


def test_recovery_error_is_reported_without_retry() -> None:
    patcher, conn = _daemon(
        {"ok": True, "data": {"account_protection": {"generation": "g-1"}}},
        {"ok": False, "error": "stale_generation", "message": "generation changed"},
    )
    with patcher:
        result = runner.invoke(app, ["recover-account", "--acknowledge-unknown-outcome"])
    assert result.exit_code != 0
    assert "generation changed" in result.output
    assert conn.recovery_calls == 1
