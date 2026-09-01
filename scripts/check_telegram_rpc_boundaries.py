"""Enforce the application-owned Telegram throttling boundary."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SOURCE_ROOT = ROOT / "src" / "mcp_telegram"
GATE_PATH = SOURCE_ROOT / "telegram_rpc.py"
_VENDOR_WAIT_NAMES = frozenset({"FloodWaitError", "FloodPremiumWaitError", "FloodTestPhoneWaitError"})
_REMOVED_NAMES = frozenset({"TelegramRpcCircuitOpenError", "FloodWaitErrors"})


def _violations(path: Path) -> list[str]:  # noqa: PLR0912 - AST policy branches are explicit
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    violations: list[str] = []
    if path != GATE_PATH:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in {"telethon.errors", "telethon.errors.rpcerrorlist"}:
                for imported in node.names:
                    if imported.name in _VENDOR_WAIT_NAMES:
                        violations.extend([f"{path}:{node.lineno}: vendor wait import {imported.name}"])
            if isinstance(node, ast.ImportFrom) and node.module == "telethon":
                for imported in node.names:
                    if imported.name == "errors":
                        violations.extend([f"{path}:{node.lineno}: vendor errors module import"])
            if isinstance(node, ast.Import):
                for imported in node.names:
                    if imported.name in {"telethon", "telethon.errors", "telethon.errors.rpcerrorlist"}:
                        violations.extend([f"{path}:{node.lineno}: vendor module import {imported.name}"])
            if isinstance(node, ast.Name) and node.id in _VENDOR_WAIT_NAMES:
                violations.append(f"{path}:{node.lineno}: vendor wait reference {node.id}")
            if isinstance(node, ast.Attribute) and node.attr in _VENDOR_WAIT_NAMES:
                violations.append(f"{path}:{node.lineno}: vendor wait reference {node.attr}")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for imported in node.names:
                if imported.name in _REMOVED_NAMES:
                    violations.extend([f"{path}:{node.lineno}: removed throttling import {imported.name}"])
        if isinstance(node, ast.Name) and node.id in _REMOVED_NAMES:
            violations.append(f"{path}:{node.lineno}: removed throttling symbol {node.id}")
        if isinstance(node, ast.Attribute) and node.attr in _REMOVED_NAMES:
            violations.append(f"{path}:{node.lineno}: removed throttling symbol {node.attr}")
    return violations


def check(root: Path = SOURCE_ROOT) -> list[str]:
    """Return all Telegram RPC boundary violations below *root*."""
    return [violation for path in sorted(root.rglob("*.py")) for violation in _violations(path)]


def main() -> int:
    violations = check()
    if violations:
        print("\n".join(violations), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
