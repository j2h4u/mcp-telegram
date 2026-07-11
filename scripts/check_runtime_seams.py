"""Static guard for the public runtime seams introduced by Slice 1B."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "mcp_telegram"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".", 1)[0])
    return roots


def _check_correlation_import_closure() -> list[str]:
    roots = _imported_roots(_tree(PACKAGE / "correlation.py"))
    allowed = set(sys.stdlib_module_names) | {"__future__"}
    return [f"correlation.py imports non-stdlib module {name!r}" for name in sorted(roots - allowed)]


def _check_server_import_closure() -> list[str]:
    """Import the MCP server in a clean process and reject Telethon leakage."""
    probe = (
        "import sys; import mcp_telegram.server; "
        "assert not [name for name in sys.modules if name == 'telethon' or name.startswith('telethon.')]"
    )
    env = os.environ.copy()
    source_root = str(ROOT / "src")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [source_root, env.get("PYTHONPATH", "")]))
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return []
    detail = result.stderr.strip() or result.stdout.strip() or "server import probe failed"
    return [f"server import closure is not Telethon-free: {detail}"]


def _check_public_consumption() -> list[str]:
    errors: list[str] = []
    daemon_client = _tree(PACKAGE / "daemon_client.py")
    server = _tree(PACKAGE / "server.py")
    for label, tree in (("daemon_client.py", daemon_client), ("server.py", server)):
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "_request_ids":
                errors.append(f"{label} references private _request_ids")
            if isinstance(node, ast.alias) and node.name == "_request_ids":
                errors.append(f"{label} imports private _request_ids")

    daemon_imports = {
        alias.name
        for node in ast.walk(daemon_client)
        if isinstance(node, ast.ImportFrom) and node.module == "correlation"
        for alias in node.names
    }
    if daemon_imports != {"record_correlation_id"}:
        errors.append("daemon_client.py must consume correlation.record_correlation_id")

    server_imports = {
        alias.name
        for node in ast.walk(server)
        if isinstance(node, ast.ImportFrom) and node.module == "correlation"
        for alias in node.names
    }
    if server_imports != {"correlation_context", "current_correlation_ids"}:
        errors.append("server.py must consume the public correlation context API")

    bootstrap = [node for node in server.body if isinstance(node, ast.FunctionDef) and node.name == "bootstrap_server"]
    if len(bootstrap) != 1:
        errors.append("server.py must expose exactly one bootstrap_server function")
    elif not any(isinstance(node, ast.Name) and node.id == "app" for node in ast.walk(bootstrap[0])):
        errors.append("bootstrap_server must return the canonical app")
    return errors


def main() -> int:
    errors = [
        *_check_correlation_import_closure(),
        *_check_server_import_closure(),
        *_check_public_consumption(),
    ]
    if errors:
        print("Runtime seam checks failed:")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print("Runtime seam checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
