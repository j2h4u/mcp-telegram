"""Folder cutover regression tests live beside the focused local projection suite."""

from __future__ import annotations

import ast
from pathlib import Path


def test_folder_package_has_no_directory_traversal_dependency() -> None:
    banned = {"iter_dialogs", "GetDialogsRequest", "GetPinnedDialogsRequest"}
    violations: list[str] = []
    for path in Path("src/mcp_telegram/folders").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations.extend(
            f"{path}:{node.lineno}:{node.id}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id in banned
        )
    assert not violations
