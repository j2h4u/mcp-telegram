"""Narrow AST gate for access-lifecycle ownership."""

# ruff: noqa: PLR1702
from __future__ import annotations

import ast
import re
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "mcp_telegram"
MOVED = {"set_access_lost", "restore_access_after_revalidation"}
FIELDS = ("access_lost_at", "access_last_revalidated_at", "access_next_revalidate_at")
SPACE = re.compile(r"\s+")


def _text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _text(node.left), _text(node.right)
        return a + b if a is not None and b is not None else None
    if isinstance(node, ast.JoinedStr):
        parts = [
            value.value for value in node.values if isinstance(value, ast.Constant) and isinstance(value.value, str)
        ]
        return "".join(parts)
    return None


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _mutation(value: str) -> bool:
    sql = SPACE.sub(" ", value).strip().lower()
    if not any(word in sql.split() for word in ("insert", "update", "delete", "replace")):
        return False
    return (
        any(field in sql for field in FIELDS)
        or bool(re.search(r"status\s*=\s*['\"]access_lost['\"]", sql))
        or ("synced_dialogs" in sql and "access_lost" in sql)
    )


def boundary_violations(source_root: Path = SOURCE_ROOT) -> list[str]:  # noqa: PLR0912
    findings: list[str] = []
    for path in source_root.rglob("*.py"):
        rel = path.relative_to(source_root)
        if rel.parts[:1] == ("access_lifecycle",) or rel == Path("sync_db.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        sync_aliases: set[str] = set()
        allowed_calls: set[str] = set()
        lifecycle_aliases: set[str] = set()
        static_sql: dict[str, str] = {}
        for assignment in ast.walk(tree):
            if isinstance(assignment, (ast.Assign, ast.AnnAssign)) and assignment.value is not None:
                text = _text(assignment.value)
                if text is not None:
                    targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            static_sql[target.id] = text
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("sync_db"):
                for alias in node.names:
                    if alias.name in MOVED:
                        findings.append(f"{rel}:{node.lineno}: moved import {alias.name}")
                    if alias.name == "*":
                        sync_aliases.add(alias.asname or "*")
            elif isinstance(node, ast.ImportFrom) and (node.module or "").endswith("access_lifecycle"):
                allowed_calls.update(alias.asname or alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.endswith("sync_db"):
                        sync_aliases.add(alias.asname or alias.name.rsplit(".", 1)[-1])
                    if alias.name.endswith("access_lifecycle"):
                        lifecycle_aliases.add(alias.asname or alias.name.rsplit(".", 1)[-1])
            if isinstance(node, ast.Call):
                qualified = _name(node.func)
                prefix = qualified.split(".", 1)[0] if qualified else ""
                if (
                    qualified
                    and qualified.rsplit(".", 1)[-1] in MOVED
                    and prefix not in lifecycle_aliases
                    and qualified not in allowed_calls
                ):
                    findings.append(f"{rel}:{node.lineno}: moved call")
                if (
                    isinstance(node.func, ast.Attribute)
                    and _name(node.func.value) in sync_aliases
                    and node.func.attr in MOVED
                ):
                    findings.append(f"{rel}:{node.lineno}: qualified moved call")
                first_arg = node.args[0] if node.args else None
                text = _text(first_arg) if first_arg is not None else None
                if isinstance(first_arg, ast.Name):
                    text = static_sql.get(first_arg.id)
                if text is not None and _mutation(text):
                    findings.append(f"{rel}:{node.lineno}: access lifecycle SQL")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _mutation(node.value):
                findings.append(f"{rel}:{node.lineno}: access lifecycle SQL")
    return sorted(set(findings))


if __name__ == "__main__":
    violations = boundary_violations()
    if violations:
        raise SystemExit("Access lifecycle boundary violations:\n" + "\n".join(violations))
    print("Access lifecycle boundary check passed.")
