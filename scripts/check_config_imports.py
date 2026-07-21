#!/usr/bin/env python3
"""Ratchet direct config imports to explicit composition roots.

This is deliberately an AST check, not a complete Python import resolver.
Import-linter owns transitive checks for the policy-facing modules; this gate
keeps new direct ``config`` imports out of state and capability code.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src" / "mcp_telegram"
ALLOWED_CONFIG_IMPORTERS = frozenset(
    {
        "__init__.py",
        "config.py",
        "daemon.py",
        "daemon_client.py",
        "telegram.py",
    }
)


def find_config_imports(source: str) -> list[int]:
    """Return lines that directly import the package-local config module.

    This normalizes the relative and absolute spellings that Python accepts;
    aliases do not change what is being imported.
    """
    tree = ast.parse(source)
    return [
        node.lineno
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.ImportFrom)
            and (
                (node.module == "config" and node.level > 0)
                or (node.module is None and node.level > 0 and any(alias.name == "config" for alias in node.names))
                or node.module == "mcp_telegram.config"
                or (node.module == "mcp_telegram" and any(alias.name == "config" for alias in node.names))
            )
        )
        or (isinstance(node, ast.Import) and any(alias.name == "mcp_telegram.config" for alias in node.names))
    ]


def violations_for(path: Path, source: str) -> list[str]:
    lines = find_config_imports(source)
    if path.name in ALLOWED_CONFIG_IMPORTERS:
        return []
    return [
        f"{path.relative_to(ROOT)}:{line}: direct config import is allowed only in composition roots" for line in lines
    ]


def main() -> int:
    violations = [
        violation
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        for violation in violations_for(path, path.read_text(encoding="utf-8"))
    ]
    if violations:
        print("Config-import violations:", *[f"- {item}" for item in violations], sep="\n", file=sys.stderr)
        return 1
    print("Config import boundary check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
