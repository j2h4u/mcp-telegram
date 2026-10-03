"""Operator-visible export command options, completion and interruption behavior."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from mcp_telegram import app, chat_export_cli

type Payload = dict[str, object]
runner = CliRunner()


def install_daemon(monkeypatch: pytest.MonkeyPatch, *, failure: BaseException | None = None) -> list[Payload]:
    calls: list[Payload] = []

    class Connection:
        async def request(self, payload: Payload) -> Payload:
            calls.append(payload)
            if failure is not None:
                raise failure
            if payload["operation"] == "open":
                peer = payload["dialog_id"]
                identifier = peer if isinstance(peer, int) else -1001
                data: Payload = {
                    "group": {"dialog_id": identifier, "title": "Group"},
                    "upper_id": 0,
                    "migrated_from_dialog_id": None,
                }
            else:
                data = {"items": [], "done": True, "status": "complete"}
            return {"ok": True, "data": data}

    @asynccontextmanager
    async def connection(timeout_seconds: float) -> AsyncIterator[Connection]:
        yield Connection()

    monkeypatch.setattr(chat_export_cli, "daemon_connection", connection)
    return calls


@pytest.mark.parametrize("options", [[], ["--output", "out.json", "--output-dir", "exports"]])
def test_export_requires_exactly_one_destination(options: list[str]) -> None:
    result = runner.invoke(app, ["export-chat", "@group", *options])
    assert result.exit_code == 2
    assert "Specify exactly one of --output or --output-dir" in result.output


@pytest.mark.parametrize("identifier", ["0", "42"])
def test_export_rejects_nonnegative_group_id(tmp_path: Path, identifier: str) -> None:
    result = runner.invoke(app, ["export-chat", identifier, "--output", str(tmp_path / "out.json")])
    assert result.exit_code == 2
    assert "canonical negative Telegram group ID" in result.output


def test_export_command_writes_explicit_filename_and_uses_negative_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = install_daemon(monkeypatch)
    output = tmp_path / "chosen.json"
    result = runner.invoke(app, ["export-chat", "--output", str(output), "--", "-42"])
    assert result.exit_code == 0, result.output
    assert "Exported 0 messages and 0 admin events" in result.output
    document = cast(Payload, json.loads(output.read_text()))
    assert cast(Payload, document["group"])["dialog_id"] == "-42"
    assert calls[0]["dialog_id"] == -42
    assert not output.with_name(f".{output.name}.resume.sqlite3").exists()


def test_export_command_directory_preserves_completed_exports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_daemon(monkeypatch)
    directory = tmp_path / "exports"
    args = ["export-chat", "@group", "--output-dir", str(directory)]
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.output
    output = directory / "telegram-group--1001.json"
    original = output.read_bytes()
    second = runner.invoke(app, args)
    assert second.exit_code == 0, second.output
    assert output.read_bytes() == original
    assert (directory / "telegram-group--1001.2.json").exists()


def test_export_command_incremental_base_and_zero_refresh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = install_daemon(monkeypatch)
    base, output = tmp_path / "base.json", tmp_path / "updated.json"
    initial = runner.invoke(app, ["export-chat", "@group", "--output", str(base)])
    assert initial.exit_code == 0, initial.output
    original = base.read_bytes()
    calls.clear()
    result = runner.invoke(
        app, ["export-chat", "@group", "--update-from", str(base), "--refresh-messages", "0", "--output", str(output)]
    )
    assert result.exit_code == 0, result.output
    assert base.read_bytes() == original
    assert output.exists()
    assert next(call for call in calls if call["operation"] == "history")["min_id"] == 0


def test_export_command_operational_failure_keeps_resume_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_daemon(monkeypatch, failure=chat_export_cli.ChatExportError("Access lost"))
    output = tmp_path / "out.json"
    result = runner.invoke(app, ["export-chat", "@group", "--output", str(output)])
    assert result.exit_code == 1
    assert "Export failed: Access lost" in result.output
    assert "saved progress retained" in result.output
    assert output.with_name(f".{output.name}.resume.sqlite3").exists()


@pytest.mark.parametrize("interruption", [asyncio.CancelledError, KeyboardInterrupt])
def test_export_command_interrupt_reports_resume_and_exit_130(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, interruption: type[BaseException]
) -> None:
    install_daemon(monkeypatch, failure=interruption())
    output = tmp_path / "out.json"
    result = runner.invoke(app, ["export-chat", "@group", "--output", str(output)])
    assert result.exit_code == 130, result.output
    assert "repeat the same command to resume saved progress" in result.output
    assert output.with_name(f".{output.name}.resume.sqlite3").exists()
