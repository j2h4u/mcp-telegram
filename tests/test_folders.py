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


def test_account_wide_dialog_directory_has_one_production_owner() -> None:
    """Keep raw account-wide dialog requests behind the canonical owner."""
    source_root = Path("src/mcp_telegram")
    raw_request_owners: set[str] = set()
    raw_call_violations: list[str] = []
    traversal_violations: list[str] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self, path: Path) -> None:
            self.path = path
            self.class_stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.class_stack.append(node.name)
            self.generic_visit(node)
            self.class_stack.pop()

        def visit_Attribute(self, node: ast.Attribute) -> None:
            if node.attr in {"GetDialogsRequest", "GetPinnedDialogsRequest"}:
                raw_request_owners.add(self.path.name)
            if node.attr == "iter_dialogs" and isinstance(node.ctx, ast.Load):
                traversal_violations.append(f"{self.path}:{node.lineno}")
            self.generic_visit(node)

        def visit_Name(self, node: ast.Name) -> None:
            if node.id in {"GetDialogsRequest", "GetPinnedDialogsRequest"}:
                raw_request_owners.add(self.path.name)
            if (
                node.id in {"get_dialogs_request", "get_pinned_dialogs_request"}
                and isinstance(node.ctx, ast.Load)
                and "CanonicalDialogDirectory" not in self.class_stack
            ):
                raw_call_violations.append(f"{self.path}:{node.lineno}")
            self.generic_visit(node)

    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        _Visitor(path).visit(tree)

    assert raw_request_owners == {"dialog_directory_tl.py"}
    assert not raw_call_violations
    assert not traversal_violations
    for relative_path in ("folders",):
        assert not list((source_root / relative_path).rglob("*.py")) or all(
            "iter_dialogs" not in path.read_text(encoding="utf-8")
            for path in (source_root / relative_path).rglob("*.py")
        )
    for relative_path in ("sync_worker.py", "daemon_api.py"):
        assert "iter_dialogs" not in (source_root / relative_path).read_text(encoding="utf-8")
