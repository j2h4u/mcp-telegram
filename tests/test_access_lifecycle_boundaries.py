from __future__ import annotations

from pathlib import Path

from scripts import check_access_lifecycle_boundaries as gate


def test_current_source_has_no_access_lifecycle_boundary_violations() -> None:
    assert gate.boundary_violations() == []


def test_gate_catches_runtime_mutation_and_moved_import(tmp_path: Path) -> None:
    root = tmp_path / "mcp_telegram"
    root.mkdir()
    (root / "bad.py").write_text(
        "from mcp_telegram.sync_db import set_access_lost\n"
        "SQL = 'UPDATE synced_dialogs SET access_lost_at = ? WHERE dialog_id = ?'\n",
        encoding="utf-8",
    )
    assert len(gate.boundary_violations(root)) == 2


def test_gate_allows_schema_owner(tmp_path: Path) -> None:
    root = tmp_path / "mcp_telegram"
    root.mkdir()
    (root / "sync_db.py").write_text(
        "MIGRATION = 'ALTER TABLE synced_dialogs ADD COLUMN access_lost_at INTEGER'\n",
        encoding="utf-8",
    )
    assert gate.boundary_violations(root) == []


def test_gate_catches_aliases_case_spacing_and_static_sql_forms(tmp_path: Path) -> None:
    root = tmp_path / "mcp_telegram"
    root.mkdir()
    (root / "bad.py").write_text(
        "import mcp_telegram.sync_db as legacy\n"
        "import mcp_telegram.access_lifecycle as lifecycle\n"
        "legacy.set_access_lost(conn, 1, 2)\n"
        "lifecycle.set_access_lost(conn, 1, 2)\n"
        "conn.execute('UpDaTe synced_dialogs SET ' + 'ACCESS_LOST_AT = ?')\n"
        'conn.executemany(f"UPDATE synced_dialogs SET access_last_revalidated_at = 1", ())\n'
        "conn.executescript(\"UPDATE synced_dialogs SET status = 'ACCESS_LOST'\")\n",
        encoding="utf-8",
    )
    violations = gate.boundary_violations(root)
    assert len(violations) == 5


def test_gate_resolves_sql_names_and_dynamic_f_strings(tmp_path: Path) -> None:
    root = tmp_path / "mcp_telegram"
    root.mkdir()
    (root / "bad.py").write_text(
        "prefix = 'UPDATE ' + 'synced_dialogs SET access_next_revalidate_at = 1'\n"
        "conn.execute(prefix)\n"
        "table = get_table()\n"
        "conn.execute(f'UPDATE {table} SET ACCESS_LOST_AT = 1')\n"
        "conn.execute(\"INSERT INTO synced_dialogs(status) VALUES ('access_lost')\")\n",
        encoding="utf-8",
    )
    assert len(gate.boundary_violations(root)) == 3


def test_gate_allows_ordinary_hidden_reconciliation(tmp_path: Path) -> None:
    root = tmp_path / "mcp_telegram"
    root.mkdir()
    (root / "catalog.py").write_text(
        "conn.execute('UPDATE dialogs SET hidden = 1, snapshot_at = ? WHERE dialog_id = ?', (now, dialog_id))\n",
        encoding="utf-8",
    )
    assert gate.boundary_violations(root) == []
