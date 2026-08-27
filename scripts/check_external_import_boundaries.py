#!/usr/bin/env python3
"""Ratchet direct external imports to their current explicit owners.

Tach owns the package import graph.  This small AST check complements it where
third-party and stdlib dependencies need a deliberately narrow ownership
boundary.  It is intentionally an ownership ratchet, not a claim that the
currently allowed modules are otherwise clean.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src" / "mcp_telegram"

# These exact package-relative paths are the import owners at the time this
# gate was introduced.  No parent-directory or glob matching is intentional.
ALLOWED_IMPORTER_PATHS: Mapping[str, frozenset[str]] = {
    # Process spawning and raw socket access have no production owners.  Keep
    # them explicit so any future use requires an architecture decision.
    "socket": frozenset(),
    "subprocess": frozenset(),
    "sqlite3": frozenset(
        {
            "__init__.py",
            "access_lifecycle/__init__.py",
            "activity_cold_backfill.py",
            "activity_hot_sweep.py",
            "activity_peer_resolve.py",
            "activity_peer_sweep.py",
            "activity_sync.py",
            "daemon.py",
            "daemon_account_trace.py",
            "daemon_activity_stats.py",
            "daemon_api.py",
            "daemon_entity_info.py",
            "daemon_log_context.py",
            "daemon_message.py",
            "reading/service.py",
            "reading/sqlite_projection.py",
            "reading/scheduled_projection.py",
            "delta_sync.py",
            "dialog_sync.py",
            "event_handlers.py",
            "feedback_db.py",
            "folders/sqlite_repository.py",
            "folders/read_repository.py",
            "fts.py",
            "history_enrollment.py",
            "hydration_queue.py",
            "messages/sqlite_repository.py",
            "message_fact_refresh.py",
            "media_hydration.py",
            "own_only.py",
            "reactions/persistence.py",
            "reactions/sqlite_repository.py",
            "read_state.py",
            "resolver.py",
            "scheduled_messages.py",
            "sync_db.py",
            "sync_worker.py",
            "telegram_fact_queries.py",
            "telegram_fragments.py",
            "topics/sqlite_repository.py",
            "unread_state.py",
        }
    ),
    "telethon": frozenset(
        {
            "activity_peer_resolve.py",
            "activity_peer_sweep.py",
            "activity_sync.py",
            "daemon.py",
            "daemon_account_trace.py",
            "daemon_api.py",
            "daemon_entity_info.py",
            "delta_sync.py",
            "dialog_sync.py",
            "event_handlers.py",
            "folders/telegram_adapter.py",
            "messages/telegram_adapter.py",
            "reactions/telegram_adapter.py",
            "scheduled_messages.py",
            "sync_worker.py",
            "telegram.py",
            "telegram_access.py",
            "telegram_gateway.py",
            "telegram_read_receipts.py",
            "media_hydration.py",
            "telethon_dialog.py",
            "telethon_media.py",
            "telethon_message.py",
            "topics/telegram_adapter.py",
        }
    ),
}


@dataclass(frozen=True)
class ExternalImport:
    """A direct import of one of the dependencies this gate owns."""

    dependency: str
    line: int


def find_external_imports(source: str) -> list[ExternalImport]:
    """Return direct policy-controlled imports, including nested imports."""
    imports: list[ExternalImport] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported_modules = (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules = (node.module,)
        else:
            continue

        for module_name in imported_modules:
            dependency = module_name.split(".", 1)[0]
            if dependency in ALLOWED_IMPORTER_PATHS:
                imports.append(ExternalImport(dependency=dependency, line=node.lineno))
    return sorted(imports, key=lambda item: (item.line, item.dependency))


def _package_relative_path(path: Path, source_root: Path) -> str:
    return path.resolve().relative_to(source_root.resolve()).as_posix()


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def violations_for(
    path: Path,
    source: str,
    *,
    allowed_importer_paths: Mapping[str, frozenset[str]] = ALLOWED_IMPORTER_PATHS,
    source_root: Path = SOURCE_ROOT,
) -> list[str]:
    """Return direct-import owner violations for one package source file."""
    package_path = _package_relative_path(path, source_root)
    return [
        f"{_display_path(path)}:{item.line}: unexpected {item.dependency} import owner"
        for item in find_external_imports(source)
        if package_path not in allowed_importer_paths[item.dependency]
    ]


def boundary_violations(
    source_root: Path = SOURCE_ROOT,
    *,
    allowed_importer_paths: Mapping[str, frozenset[str]] = ALLOWED_IMPORTER_PATHS,
) -> list[str]:
    """Return unexpected owners and stale explicit owner allowlist entries."""
    import_owners = {dependency: set() for dependency in allowed_importer_paths}
    violations: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        package_path = _package_relative_path(path, source_root)
        for item in find_external_imports(source):
            import_owners[item.dependency].add(package_path)
        violations.extend(
            violations_for(
                path,
                source,
                allowed_importer_paths=allowed_importer_paths,
                source_root=source_root,
            )
        )

    for dependency in sorted(allowed_importer_paths):
        violations.extend(
            f"{package_path}: stale {dependency} import allowlist entry"
            for package_path in sorted(allowed_importer_paths[dependency] - import_owners[dependency])
        )
    return violations


def main() -> int:
    violations = boundary_violations()
    if violations:
        print("External import boundary violations:", *[f"- {item}" for item in violations], sep="\n", file=sys.stderr)
        return 1
    print("External import boundary check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
