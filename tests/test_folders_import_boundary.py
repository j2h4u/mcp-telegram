"""Static ownership boundary tests for Telegram folders."""

from __future__ import annotations

import ast
from pathlib import Path


def test_daemon_api_uses_folder_read_model_not_sqlite_repository() -> None:
    path = Path("src/mcp_telegram/daemon_api.py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            violations.extend(
                f"line {node.lineno}: import {alias.name}"
                for alias in node.names
                if alias.name == "mcp_telegram.folders.sqlite_repository"
            )
        elif isinstance(node, ast.ImportFrom):
            relative_sqlite_repository = node.level == 1 and node.module == "folders.sqlite_repository"
            if node.module == "mcp_telegram.folders.sqlite_repository" or relative_sqlite_repository:
                violations.append(f"line {node.lineno}: from {node.module} import ...")

    assert not violations, "daemon_api must use folders.read_model, not folders.sqlite_repository: " + "; ".join(
        violations
    )
