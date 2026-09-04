"""Focused tests for the external dependency ownership ratchet."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, cast


class _ExternalImport(Protocol):
    dependency: str
    line: int


class _ExternalImportGate(Protocol):
    SOURCE_ROOT: Path
    ALLOWED_IMPORTER_PATHS: Mapping[str, frozenset[str]]

    def find_external_imports(self, source: str) -> list[_ExternalImport]: ...

    def violations_for(
        self,
        path: Path,
        source: str,
        *,
        allowed_importer_paths: Mapping[str, frozenset[str]],
        source_root: Path,
    ) -> list[str]: ...

    def boundary_violations(
        self,
        source_root: Path,
        *,
        allowed_importer_paths: Mapping[str, frozenset[str]],
    ) -> list[str]: ...


def _load_gate() -> _ExternalImportGate:
    path = Path(__file__).parents[1] / "scripts" / "check_external_import_boundaries.py"
    spec = importlib.util.spec_from_file_location("check_external_import_boundaries", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return cast(_ExternalImportGate, module)


def test_current_source_baseline_has_no_external_import_boundary_violations() -> None:
    gate = _load_gate()

    assert gate.boundary_violations(gate.SOURCE_ROOT, allowed_importer_paths=gate.ALLOWED_IMPORTER_PATHS) == []


def test_sqlite_import_owners_name_split_persistence_modules() -> None:
    gate = _load_gate()

    assert {
        "account_trace_sqlite.py",
        "daemon_shutdown.py",
        "sqlite_checkpoint.py",
        "messages/sqlite_bundle.py",
        "messages/sqlite_hydration.py",
        "messages/sqlite_hydration_jobs.py",
    } <= gate.ALLOWED_IMPORTER_PATHS["sqlite3"]
    assert "messages/sqlite_repository.py" not in gate.ALLOWED_IMPORTER_PATHS["sqlite3"]


def test_forbidden_import_owner_is_rejected() -> None:
    gate = _load_gate()
    path = gate.SOURCE_ROOT / "not_an_owner.py"

    assert gate.violations_for(
        path,
        "from telethon.tl import types\n",
        allowed_importer_paths=gate.ALLOWED_IMPORTER_PATHS,
        source_root=gate.SOURCE_ROOT,
    ) == ["src/mcp_telegram/not_an_owner.py:1: unexpected telethon import owner"]


def test_stale_allowlist_entry_is_rejected(tmp_path: Path) -> None:
    gate = _load_gate()
    source_root = tmp_path / "mcp_telegram"
    source_root.mkdir()
    (source_root / "active.py").write_text("import telethon\n", encoding="utf-8")
    allowlists = {"telethon": frozenset({"active.py", "obsolete.py"}), "sqlite3": frozenset()}

    assert gate.boundary_violations(source_root, allowed_importer_paths=allowlists) == [
        "obsolete.py: stale telethon import allowlist entry"
    ]


def test_alias_and_dotted_import_forms_are_detected_for_all_controlled_dependencies() -> None:
    gate = _load_gate()

    imports = gate.find_external_imports(
        "import telethon\n"
        "import telethon.tl as tl\n"
        "from telethon.tl import types\n"
        "import sqlite3 as db\n"
        "from sqlite3 import connect\n"
        "import socket.socket as socket_constructor\n"
        "from socket import socket\n"
        "import subprocess.run as run_module\n"
        "from subprocess import run\n"
    )

    assert [(item.dependency, item.line) for item in imports] == [
        ("telethon", 1),
        ("telethon", 2),
        ("telethon", 3),
        ("sqlite3", 4),
        ("sqlite3", 5),
        ("socket", 6),
        ("socket", 7),
        ("subprocess", 8),
        ("subprocess", 9),
    ]


def test_nested_and_type_checking_imports_are_detected() -> None:
    gate = _load_gate()

    imports = gate.find_external_imports(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from telethon import TelegramClient\n"
        "def open_connection():\n"
        "    import sqlite3\n"
        "    from socket import create_connection\n"
        "def spawn_process():\n"
        "    import subprocess\n"
    )

    assert [(item.dependency, item.line) for item in imports] == [
        ("telethon", 3),
        ("sqlite3", 5),
        ("socket", 6),
        ("subprocess", 8),
    ]


def test_forbidden_subprocess_and_socket_imports_have_clear_diagnostics() -> None:
    gate = _load_gate()
    path = gate.SOURCE_ROOT / "not_an_owner.py"

    assert gate.violations_for(
        path,
        "import subprocess\nfrom socket import socket\n",
        allowed_importer_paths=gate.ALLOWED_IMPORTER_PATHS,
        source_root=gate.SOURCE_ROOT,
    ) == [
        "src/mcp_telegram/not_an_owner.py:1: unexpected subprocess import owner",
        "src/mcp_telegram/not_an_owner.py:2: unexpected socket import owner",
    ]


def test_comments_and_strings_do_not_trigger_detection() -> None:
    gate = _load_gate()

    assert (
        gate.find_external_imports(
            "# import telethon, socket, and subprocess\n"
            'example = "from sqlite3 import connect; import socket"\n'
            "document = '''import telethon.tl; from subprocess import run'''\n"
            "reader, writer = await asyncio.open_unix_connection('/tmp/mcp.sock')\n"
        )
        == []
    )
