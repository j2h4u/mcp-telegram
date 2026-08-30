"""Regression guards for the compact runtime logging policy."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _literal_log_levels(path: Path) -> dict[str, list[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    levels: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or not node.args:
            continue
        if node.func.attr not in {"debug", "info", "warning", "error", "exception"}:
            continue
        message = node.args[0]
        if not isinstance(message, ast.Constant) or not isinstance(message.value, str):
            continue
        key = message.value.split(maxsplit=1)[0]
        levels.setdefault(key, []).append(node.func.attr)
    return levels


def _levels_for_message_prefix(path: Path, prefix: str) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    levels: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or not node.args:
            continue
        if node.func.attr not in {"debug", "info", "warning", "error", "exception"}:
            continue
        message = node.args[0]
        if isinstance(message, ast.Constant) and isinstance(message.value, str) and message.value.startswith(prefix):
            levels.append(node.func.attr)
    return levels


def test_compact_runtime_success_records_and_important_paths_have_expected_levels() -> None:
    """Keep routine success quiet while retaining operational signals."""
    event_levels = _literal_log_levels(ROOT / "src/mcp_telegram/event_handlers.py")
    assert {
        name: event_levels[name]
        for name in (
            "event_new",
            "event_read",
            "event_outbox_read",
            "event_raw_inbox_read",
            "event_edit",
            "event_edit_reactions",
            "event_raw_reaction",
            "event_raw_transcribed_audio",
        )
    } == {
        "event_new": ["debug"],
        "event_read": ["debug"],
        "event_outbox_read": ["debug"],
        "event_raw_inbox_read": ["debug"],
        "event_edit": ["debug"],
        "event_edit_reactions": ["debug"],
        "event_raw_reaction": ["debug"],
        "event_raw_transcribed_audio": ["debug"],
    }
    assert event_levels["event_edit_new"] == ["info"]
    assert event_levels["event_read_no_row"] == ["warning"]
    assert event_levels["event_outbox_read_no_row"] == ["warning"]

    assert _literal_log_levels(ROOT / "src/mcp_telegram/dialog_sync.py")["recon_topics_complete"] == ["debug"]

    daemon_levels = _literal_log_levels(ROOT / "src/mcp_telegram/daemon.py")
    assert sorted(daemon_levels["initialize_read_positions"]) == ["debug", "info"]
    daemon_path = ROOT / "src/mcp_telegram/daemon.py"
    assert _levels_for_message_prefix(daemon_path, "initialize_read_positions — no NULL rows") == ["debug"]
    assert _levels_for_message_prefix(daemon_path, "initialize_read_positions filled=") == ["info"]

    reading_levels = _literal_log_levels(ROOT / "src/mcp_telegram/reading/service.py")
    assert reading_levels["list_dialogs_sql_reader"] == ["debug"]
