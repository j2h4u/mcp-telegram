"""Static ownership boundary tests for the reading service."""

from __future__ import annotations

import ast
from pathlib import Path


def test_daemon_reading_does_not_import_daemon_api() -> None:
    path = Path("src/mcp_telegram/daemon_reading.py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            violations.extend(
                f"line {node.lineno}: import {alias.name}"
                for alias in node.names
                if alias.name == "mcp_telegram.daemon_api"
            )
        elif isinstance(node, ast.ImportFrom):
            relative_daemon_api = node.level == 1 and (
                node.module == "daemon_api"
                or (node.module is None and any(alias.name == "daemon_api" for alias in node.names))
            )
            if node.module == "mcp_telegram.daemon_api" or relative_daemon_api:
                imported = node.module or "daemon_api"
                violations.append(f"line {node.lineno}: from {imported} import ...")

    assert not violations, "daemon_reading must depend on owner modules, not daemon_api: " + "; ".join(violations)
