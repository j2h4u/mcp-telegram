from __future__ import annotations

from pathlib import Path

import pytest

from scripts import check_transaction_boundaries as gate


def _source_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "mcp_telegram"
    root.mkdir()
    for name, source in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    return root


def test_current_source_has_no_transaction_boundary_violations() -> None:
    assert gate.boundary_violations() == []


def test_gate_rejects_raw_transactions_and_context_managers(tmp_path: Path) -> None:
    root = _source_tree(
        tmp_path,
        {
            "bad.py": """
from contextlib import ExitStack
from mcp_telegram.sync_transactions import write_transaction as wt
SQL = ' BEGIN ' + 'IMMEDIATE'

def bad(conn):
    alias = conn
    conn.commit()
    conn.execute(SQL)
    conn.executescript('SELECT 1')
    conn.execute('PRAGMA query_only=OFF')
    with alias:
        pass
    conn.__enter__()
    with ExitStack() as stack:
        stack.enter_context(conn)
"""
        },
    )
    violations = gate.boundary_violations(root)
    assert any("raw SQLite commit" in item for item in violations)
    assert any("raw transaction SQL" in item for item in violations)
    assert any("raw SQLite executescript" in item for item in violations)
    assert any("raw SQLite connection context" in item for item in violations)
    assert any("manual context entry/exit" in item for item in violations)
    assert any("ExitStack" in item for item in violations)


def test_gate_rejects_helper_aliases_and_assigned_contexts(tmp_path: Path) -> None:
    root = _source_tree(
        tmp_path,
        {
            "bad.py": """
from mcp_telegram.sync_transactions import write_transaction as tx

async def bad(conn):
    scope = tx
    manager = scope(conn)
    with manager:
        await remote()
"""
        },
    )
    violations = gate.boundary_violations(root)
    assert any("write helper reference escapes" in item for item in violations)
    assert any("must be a direct with-context" in item for item in violations)


@pytest.mark.parametrize("connection", ["conn", "self._conn"])
def test_gate_rejects_suspension_and_nested_raw_context_in_owned_unit(tmp_path: Path, connection: str) -> None:
    root = _source_tree(
        tmp_path,
        {
            "bad.py": f"""
from mcp_telegram.sync_transactions import write_transaction

async def bad(conn):
    with write_transaction(conn):
        with {connection}:
            conn.execute('INSERT INTO t VALUES (1)')
        await remote()
"""
        },
    )
    violations = gate.boundary_violations(root)
    assert any("raw SQLite connection context" in item for item in violations)
    assert any("suspension inside synchronous write context" in item for item in violations)


def test_deferred_async_function_inside_write_unit_is_not_a_suspension(tmp_path: Path) -> None:
    root = _source_tree(
        tmp_path,
        {
            "ok.py": """
from mcp_telegram.sync_transactions import write_transaction as owned

def persist(conn):
    with owned(conn):
        conn.execute('INSERT INTO t VALUES (1)')
        async def later():
            await remote()
"""
        },
    )
    assert gate.boundary_violations(root) == []


def test_gate_limits_schema_and_connection_exceptions_to_exact_functions(tmp_path: Path) -> None:
    root = _source_tree(
        tmp_path,
        {
            "sync_db.py": """
import sqlite3

def _open_sync_db(path):
    return sqlite3.connect(path)

def _apply_migration(conn):
    conn.execute('BEGIN IMMEDIATE')
    conn.commit()

def future_runtime_writer(conn):
    conn.execute('BEGIN IMMEDIATE')
""",
            "new_writer.py": """
import sqlite3

def open_connection(path):
    return sqlite3.connect(path)
""",
        },
    )
    violations = gate.boundary_violations(root)
    assert any("raw transaction SQL" in item for item in violations)
    assert any("SQLite connection outside approved factory" in item for item in violations)


def test_read_only_connection_exceptions_and_separate_stores(tmp_path: Path) -> None:
    root = _source_tree(
        tmp_path,
        {
            "__init__.py": """
import sqlite3

def feedback_list(path):
    return sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)
""",
            "event_recovery.py": """
import sqlite3

def recover_events(source, target_path):
    src = sqlite3.connect(f'file:{source}?mode=ro', uri=True)
    dst = sqlite3.connect(target_path)
    return src, dst
""",
            "feedback_db.py": """
import sqlite3

def ensure_feedback_schema(path):
    conn = sqlite3.connect(path)
    conn.commit()
""",
        },
    )
    assert gate.boundary_violations(root) == []


def test_gate_rejects_query_only_toggles_and_wrong_legacy_factory_callsite(tmp_path: Path) -> None:
    root = _source_tree(
        tmp_path,
        {
            "sync_db.py": """
def _open_sync_db(path):
    return path

def unrelated(conn):
    conn.execute('PRAGMA query_only=ON')
    _open_sync_db('db')
    conn.isolation_level = None
""",
        },
    )
    violations = gate.boundary_violations(root)
    assert any("raw transaction SQL" in item for item in violations)
    assert any("legacy _open_sync_db call" in item for item in violations)
    assert any("isolation_level toggle" in item for item in violations)


@pytest.mark.parametrize("helper", ["write_transaction", "write_savepoint"])
def test_gate_rejects_async_write_context_even_without_await(tmp_path: Path, helper: str) -> None:
    root = _source_tree(
        tmp_path,
        {
            "bad.py": f"""
from mcp_telegram.sync_transactions import {helper} as owned

async def persist(conn):
    with owned(conn):
        conn.execute('INSERT INTO t VALUES (1)')
"""
        },
    )
    assert any("synchronous write context in async function" in item for item in gate.boundary_violations(root))


def test_gate_allows_nested_sync_callback_with_enclosing_import_alias(tmp_path: Path) -> None:
    root = _source_tree(
        tmp_path,
        {
            "ok.py": """
async def persist(writer):
    from mcp_telegram.sync_transactions import write_transaction as owned

    def apply(conn):
        with owned(conn):
            conn.execute('INSERT INTO t VALUES (1)')

    await writer.run(apply)
"""
        },
    )
    assert gate.boundary_violations(root) == []


@pytest.mark.parametrize(
    ("imports", "factory"),
    [
        ("from mcp_telegram.sync_db import open_runtime_sync_db as acquire", "acquire"),
        ("import mcp_telegram.sync_db as db", "db.open_runtime_sync_db"),
        ("from mcp_telegram import sync_db as db", "db.open_runtime_sync_db"),
    ],
)
def test_gate_rejects_runtime_factory_aliases(tmp_path: Path, imports: str, factory: str) -> None:
    root = _source_tree(
        tmp_path,
        {"bad.py": f"{imports}\n\ndef acquire_writer(path):\n    factory = {factory}\n    return factory(path)\n"},
    )
    violations = gate.boundary_violations(root)
    assert any("runtime writer connection outside owner" in item for item in violations)
    assert any("connection setup reference escapes direct call" in item for item in violations)


@pytest.mark.parametrize(
    ("imports", "factory"),
    [
        ("from mcp_telegram.sync_db import _open_sync_db as acquire", "acquire"),
        ("from mcp_telegram.sync_transactions import enable_runtime_writes as acquire", "acquire"),
        ("from sqlite3 import connect as acquire", "acquire"),
        ("import sqlite3 as sql", "sql.connect"),
    ],
)
def test_gate_rejects_setup_aliases_in_nested_callbacks(tmp_path: Path, imports: str, factory: str) -> None:
    root = _source_tree(
        tmp_path,
        {
            "bad.py": f"""
async def persist(writer):
    {imports}

    def apply(conn):
        return {factory}(conn)

    await writer.run(apply)
"""
        },
    )
    assert gate.boundary_violations(root)


def test_gate_checks_conditionally_defined_callback_with_inherited_alias(tmp_path: Path) -> None:
    root = _source_tree(
        tmp_path,
        {
            "bad.py": """
async def persist(writer, enabled):
    from mcp_telegram.sync_db import open_runtime_sync_db as acquire
    if enabled:
        def apply(path):
            return acquire(path)
        await writer.run(apply)
"""
        },
    )
    assert any(
        "runtime writer connection outside owner in persist.apply" in item for item in gate.boundary_violations(root)
    )


def test_gate_limits_writer_factory_and_recovery_to_exact_owner_methods(tmp_path: Path) -> None:
    root = _source_tree(
        tmp_path,
        {
            "sqlite_writer.py": """
from mcp_telegram.sync_db import open_runtime_sync_db

class SQLiteWriter:
    def _work(self):
        return open_runtime_sync_db(self.path)

    def _recover(self, conn):
        conn.rollback()
        conn.execute('PRAGMA query_only=ON')

class AnotherWriter:
    def _work(self):
        return open_runtime_sync_db(self.path)

    def _recover(self, conn):
        conn.rollback()
"""
        },
    )
    violations = gate.boundary_violations(root)
    assert len(violations) == 2
    assert all("AnotherWriter." in item for item in violations)


def test_gate_allows_bootstrap_but_rejects_retired_sink_factory(tmp_path: Path) -> None:
    root = _source_tree(
        tmp_path,
        {
            "daemon.py": """
from mcp_telegram.sync_db import _open_sync_db
from mcp_telegram.sync_transactions import enable_runtime_writes

def _bootstrap_sync_database(path):
    conn = _open_sync_db(path)
    enable_runtime_writes(conn)

async def _build_sync_main_context(path):
    return _open_sync_db(path)
""",
            "runtime_observations.py": """
import sqlite3
from mcp_telegram.sync_transactions import enable_runtime_writes

class RuntimeObservationSink:
    def _open_writer_connection(self):
        conn = sqlite3.connect(self.path)
        enable_runtime_writes(conn)
        return conn
""",
        },
    )
    violations = gate.boundary_violations(root)
    assert len(violations) == 3
    assert not any("in _bootstrap_sync_database" in item for item in violations)
