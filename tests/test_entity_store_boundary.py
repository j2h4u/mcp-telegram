# pyright: reportAny=false
"""Focused tests for exact-table entity DML ownership."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_ENTITY_FINDING = "direct INSERT/UPDATE/DELETE/REPLACE entities SQL"


def _gate() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "check_message_boundaries.py"
    spec = importlib.util.spec_from_file_location("check_message_boundaries_for_entity_store", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _entity_findings(gate: ModuleType, path: Path, source: str) -> list[object]:
    return [finding for finding in gate.violations_for(path, source) if _ENTITY_FINDING in finding.message]


def test_live_runtime_entity_dml_has_only_canonical_owners() -> None:
    gate = _gate()

    findings = [finding for finding in gate.boundary_violations() if _ENTITY_FINDING in finding.message]

    assert findings == []


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO entities (id, type, updated_at) VALUES (?, ?, ?)",
        "INSERT OR IGNORE INTO main.entities (id, type, updated_at) VALUES (?, ?, ?)",
        "REPLACE INTO entities (id, type, updated_at) VALUES (?, ?, ?)",
        "UPDATE entities SET name = ? WHERE id = ?",
        "UPDATE OR REPLACE main.entities SET username = ? WHERE id = ?",
        'DELETE FROM "entities" WHERE id = ?',
    ],
)
def test_runtime_entity_dml_is_rejected_outside_canonical_owner(sql: str) -> None:
    gate = _gate()

    findings = _entity_findings(gate, gate.SOURCE_ROOT / "rogue.py", f"SQL = {sql!r}")

    assert len(findings) == 1


@pytest.mark.parametrize(
    "sql",
    [
        (
            "WITH incoming(id, type, updated_at) AS (VALUES (1, 'User', 1)) "
            "INSERT INTO entities (id, type, updated_at) SELECT id, type, updated_at FROM incoming"
        ),
        (
            "WITH incoming(name) AS (VALUES ('Alice')) "
            "UPDATE entities SET name = (SELECT name FROM incoming) WHERE id = 1"
        ),
        ("WITH doomed(id) AS (VALUES (1)) DELETE FROM entities WHERE id IN (SELECT id FROM doomed)"),
    ],
)
def test_cte_prefixed_entity_dml_is_rejected_outside_canonical_owner(sql: str) -> None:
    gate = _gate()

    findings = _entity_findings(gate, gate.SOURCE_ROOT / "rogue.py", f"SQL = {sql!r}")

    assert len(findings) == 1


def test_cte_literal_parentheses_and_doubled_quote_do_not_hide_entity_insert() -> None:
    gate = _gate()
    sql = (
        "WITH incoming(name) AS (VALUES (')) O''Brien INSERT INTO decoy ((')) "
        "INSERT INTO entities (id, type, name, updated_at) SELECT 1, 'User', name, 1 FROM incoming"
    )

    findings = _entity_findings(gate, gate.SOURCE_ROOT / "rogue.py", f"SQL = {sql!r}")

    assert len(findings) == 1


@pytest.mark.parametrize(
    "sql",
    [
        "WITH selected AS (SELECT * FROM entities) SELECT * FROM selected",
        (
            "WITH selected(entity_id) AS (VALUES (1)) "
            "UPDATE entity_details SET fetched_at = 2 WHERE entity_id IN (SELECT entity_id FROM selected)"
        ),
    ],
)
def test_cte_without_entity_dml_remains_allowed(sql: str) -> None:
    gate = _gate()

    assert _entity_findings(gate, gate.SOURCE_ROOT / "reader.py", f"SQL = {sql!r}") == []


@pytest.mark.parametrize(
    "relative_path",
    [
        "entity_store.py",
        "sync_db.py",
    ],
)
def test_entity_store_and_schema_migration_owner_are_allowed(relative_path: str) -> None:
    gate = _gate()
    source = 'SQL = "DELETE FROM main.entities WHERE id = ?"'

    assert _entity_findings(gate, gate.SOURCE_ROOT / relative_path, source) == []


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM entities WHERE id = ?",
        "INSERT INTO message_entities (message_id, entity_id) VALUES (?, ?)",
        "UPDATE entity_details SET fetched_at = ? WHERE entity_id = ?",
        "DELETE FROM unrelated_entities WHERE id = ?",
    ],
)
def test_read_only_and_similarly_named_tables_are_not_entity_dml(sql: str) -> None:
    gate = _gate()

    assert gate._has_entity_table_dml(sql) is False
    assert _entity_findings(gate, gate.SOURCE_ROOT / "reader.py", f"SQL = {sql!r}") == []


def test_boundary_scan_is_limited_to_production_source_not_docs_or_tests() -> None:
    gate = _gate()
    repository_root = Path(__file__).parents[1].resolve()

    assert gate.SOURCE_ROOT.resolve() == repository_root / "src" / "mcp_telegram"
    assert not (repository_root / "docs").is_relative_to(gate.SOURCE_ROOT.resolve())
    assert not (repository_root / "tests").is_relative_to(gate.SOURCE_ROOT.resolve())
