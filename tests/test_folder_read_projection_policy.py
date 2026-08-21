from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
_WRITE_SQL = re.compile(r"\b(?:INSERT|UPDATE|DELETE|REPLACE|COMMIT)\b", re.IGNORECASE)
_TELEGRAM_CALLS = {
    "fetch_snapshot",
    "get_entity",
    "get_input_entity",
    "get_messages",
    "iter_dialogs",
    "iter_messages",
    "iter_participants",
    "refresh",
}
_FORBIDDEN_READ_CALLS = _TELEGRAM_CALLS | {
    "_client",
    "client",
    "_gateway",
    "gateway",
    "_refresher",
    "refresher",
    "commit",
    "rollback",
    "executemany",
    "executescript",
}


def _function_source(path: Path, names: set[str]) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    source = path.read_text(encoding="utf-8")
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) or node.name not in names:
            continue
        segment = ast.get_source_segment(source, node)
        assert segment is not None
        found.append(segment)
    assert len(found) == len(names), f"missing read projection functions in {path}: {names}"
    return found


def test_folder_read_handlers_have_no_telegram_refresh_or_dml_calls() -> None:
    functions = _function_source(
        ROOT / "src/mcp_telegram/daemon_api.py",
        {"_list_dialogs", "_list_folders", "_list_folder_messages"},
    )
    functions += _function_source(
        ROOT / "src/mcp_telegram/reading/service.py",
        {"_list_dialogs", "_list_dialogs_sync", "_list_dialogs_from_reader"},
    )
    functions += _function_source(
        ROOT / "src/mcp_telegram/folders/read_model.py",
        {"list_folders", "list_folder_messages", "folder_snapshot", "folders_by_dialog", "dialog_placement"},
    )

    for source in functions:
        assert not _WRITE_SQL.search(source), source
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            assert node.func.attr not in _FORBIDDEN_READ_CALLS, f"forbidden read call {node.func.attr}: {source}"


def test_folder_read_model_has_no_acquisition_imports() -> None:
    path = ROOT / "src/mcp_telegram/folders/read_model.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            rendered = ast.unparse(node).lower()
            assert "sqlite_repository" not in rendered
            assert "gateway" not in rendered
            assert "refresher" not in rendered
            assert "telethon" not in rendered


def test_folder_read_repository_is_select_only_and_has_no_write_or_telegram_dependencies() -> None:
    path = ROOT / "src/mcp_telegram/folders/read_repository.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            rendered = ast.unparse(node).lower()
            assert not any(
                forbidden in rendered
                for forbidden in ("sqlite_repository", "ports", "contracts", "refresh", "gateway", "telethon")
            )
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        assert node.func.attr not in _FORBIDDEN_READ_CALLS, ast.get_source_segment(source, node)
        if node.func.attr != "execute":
            continue
        assert node.args and isinstance(node.args[0], ast.Constant), ast.get_source_segment(source, node)
        sql = node.args[0].value
        assert isinstance(sql, str), ast.get_source_segment(source, node)
        assert sql.lstrip().upper().startswith("SELECT"), ast.get_source_segment(source, node)
