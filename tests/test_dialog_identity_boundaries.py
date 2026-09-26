"""Focused ownership checks for canonical dialog identity."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Protocol, cast


class _Finding(Protocol):
    message: str


class _Gate(Protocol):
    SOURCE_ROOT: Path

    def violations_for(self, path: Path, source: str) -> list[_Finding]: ...

    def boundary_violations(self, source_root: Path) -> list[_Finding]: ...


def _gate() -> _Gate:
    path = Path(__file__).parents[1] / "scripts" / "check_message_boundaries.py"
    spec = importlib.util.spec_from_file_location("check_message_boundaries", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return cast(_Gate, module)


def _identity_findings(relative: str, source: str) -> list[object]:
    gate = _gate()
    return [
        finding
        for finding in gate.violations_for(gate.SOURCE_ROOT / relative, source)
        if "dialog name/type/username" in finding.message
    ]


def test_dialog_identity_fallbacks_are_rejected_in_consumers() -> None:
    assert _identity_findings(
        "reading/sqlite_projection.py",
        'SQL = "SELECT COALESCE(d.name, e.name) FROM dialogs d LEFT JOIN entities e ON e.id=d.dialog_id"',
    )
    assert _identity_findings(
        "daemon_activity_stats.py",
        'SQL = "SELECT COALESCE(d.type, e.type) FROM dialogs d LEFT JOIN entities e ON e.id=d.dialog_id"',
    )
    assert _identity_findings(
        "daemon_api.py",
        "def _resolve_dialog_username(conn, username):\n"
        '    return conn.execute("SELECT id FROM entities WHERE username = ?")\n',
    )
    assert _identity_findings("daemon_api.py", 'SQL = "SELECT d.* FROM dialogs d"')


def test_identity_dml_aliases_and_conflict_updates_are_rejected() -> None:
    for sql in (
        "UPDATE dialogs AS d SET username = ? WHERE d.dialog_id = ?",
        "UPDATE OR IGNORE dialogs SET type=? WHERE dialog_id=?",
        "UPDATE dialogs SET (type)=(?) WHERE dialog_id=?",
        "UPDATE dialogs SET (name,type)=(?,?) WHERE dialog_id=?",
        "INSERT INTO main.dialogs(dialog_id,name) VALUES (?,?) ON CONFLICT(dialog_id) DO UPDATE SET name=excluded.name",
    ):
        assert _identity_findings("rogue.py", f'conn.execute("{sql}")')

    entity_findings = _gate().violations_for(
        _gate().SOURCE_ROOT / "rogue.py",
        'conn.execute("UPDATE entities SET type = ? WHERE id = ?")',
    )
    assert any("outside canonical entity_store.py ownership" in finding.message for finding in entity_findings)


def test_identity_owner_and_exact_schema_owner_are_allowed() -> None:
    for relative in ("dialog_identity.py", "sync_db.py"):
        assert (
            _identity_findings(
                relative,
                'conn.execute("UPDATE dialogs SET name = ? WHERE dialog_id = ?")',
            )
            == []
        )


def test_sender_and_explicit_entity_roles_remain_allowed() -> None:
    sender_sql = (
        'SQL = "SELECT COALESCE(e_raw.name, e_eff.username) FROM messages m '
        "LEFT JOIN entities e_raw ON e_raw.id = m.sender_id "
        'LEFT JOIN entities e_eff ON e_eff.id = COALESCE(m.sender_id, m.dialog_id)"'
    )
    assert _identity_findings("reading/sqlite_projection.py", sender_sql) == []
    # A sender expression fragment without its SELECT/JOIN role has no entity
    # identity table to classify and must remain outside the dialog boundary.
    assert _identity_findings("reading/sqlite_projection.py", 'SQL = "COALESCE(e_raw.name, e_eff.username)"') == []
    assert (
        _identity_findings(
            "daemon_activity_stats.py", 'SQL = "WITH candidates AS (SELECT * FROM messages) SELECT * FROM candidates"'
        )
        == []
    )
    assert (
        _identity_findings(
            "account_trace_sqlite.py",
            "def account_by_username(conn, username):\n"
            '    return conn.execute("SELECT id,name,username FROM entities WHERE username=?")\n',
        )
        == []
    )
    assert (
        _identity_findings(
            "entity_profile/repository.py",
            "def _read_entity_stub(self, entity_id):\n"
            '    return self._conn.execute("SELECT type,name,username FROM entities WHERE id=?")\n',
        )
        == []
    )


def test_profile_entity_writes_stay_behind_entity_store() -> None:
    assert (
        _gate().violations_for(
            _gate().SOURCE_ROOT / "entity_profile/repository.py",
            "from ..entity_store import upsert_entity_snapshots\n",
        )
        == []
    )


def test_operational_dialog_roles_are_read_only_and_field_limited() -> None:
    source_root = _gate().SOURCE_ROOT
    assert (
        _identity_findings(
            "activity_peer_sweep.py",
            'def _next_enrollment_dialog(conn):\n    return conn.execute("SELECT d.type FROM dialogs d")',
        )
        == []
    )
    assert _identity_findings(
        "activity_peer_sweep.py",
        'def _next_enrollment_dialog(conn):\n    return conn.execute("SELECT d.name FROM dialogs d")',
    )
    assert _identity_findings(
        "activity_peer_sweep.py",
        'def _next_enrollment_dialog(conn):\n    return conn.execute("UPDATE dialogs SET name=?")',
    )
    assert _gate().violations_for(
        source_root / "activity_peer_sweep.py",
        'def _next_enrollment_dialog(conn):\n    return conn.execute("UPDATE dialogs SET name=?")',
    )
    type_write = (
        "def _next_enrollment_dialog(conn):\n"
        '    return conn.execute("UPDATE dialogs SET type = ? WHERE dialog_id = ?")\n'
    )
    assert _identity_findings("activity_peer_sweep.py", type_write)
    for sql in (
        "UPDATE OR IGNORE dialogs SET type=? WHERE dialog_id=?",
        "UPDATE dialogs SET (type)=(?) WHERE dialog_id=?",
        "UPDATE dialogs SET (name,type)=(?,?) WHERE dialog_id=?",
    ):
        assert _identity_findings(
            "activity_peer_sweep.py",
            f'def _next_enrollment_dialog(conn):\n    return conn.execute("{sql}")\n',
        )
    assert _identity_findings(
        "sync_worker.py",
        "class FullSyncWorker:\n"
        "    def consume_canonical_dm_publication(self):\n"
        "        return self._conn.execute(\"SELECT identity_complete FROM dialogs WHERE type IN ('user','bot')\")\n",
    )


def test_dialog_entity_alias_filters_remain_owned_in_consumer_roles() -> None:
    for sql in (
        "SELECT d.name FROM scheduled_messages sm LEFT JOIN dialogs d ON d.dialog_id=sm.dialog_id",
        "SELECT m.id FROM messages m JOIN entities e ON e.id=m.dialog_id WHERE e.username=?",
        "SELECT e.type FROM messages m JOIN entities e ON e.id=m.dialog_id",
        "SELECT e.name FROM messages m JOIN entities e ON e.id=m.dialog_id",
    ):
        assert _identity_findings("reading/sqlite_projection.py", f'SQL = "{sql}"')


def test_helper_cannot_rebuild_an_identity_bundle_through_aliased_imports() -> None:
    source = (
        "from mcp_telegram.dialog_identity_contracts import DialogIdentity as IdentityBundle\n"
        "make_bundle = IdentityBundle\n"
        "def build(row):\n    return make_bundle(*row)\n"
    )
    assert any(
        "DialogIdentity bundles" in finding.message
        for finding in _gate().violations_for(_gate().SOURCE_ROOT / "resolver.py", source)
    )

    qualified = (
        "import mcp_telegram.dialog_identity_contracts as contracts\n"
        "make_bundle = contracts.DialogIdentity\n"
        "def build(row):\n    return make_bundle(*row)\n"
    )
    assert any(
        "DialogIdentity bundles" in finding.message
        for finding in _gate().violations_for(_gate().SOURCE_ROOT / "resolver.py", qualified)
    )


def test_consumers_and_producers_can_use_the_owner_contract() -> None:
    consumer = (
        "from mcp_telegram.dialog_identity import read_dialog_identities as read_ids\n"
        "def display(conn, ids):\n"
        "    bundles = read_ids(conn, ids)\n"
        "    return [(item.name, item.dialog_type) for item in bundles.values()]\n"
    )
    producer = (
        "from mcp_telegram.dialog_identity import publish_dialog_identity as publish\n"
        "from mcp_telegram.dialog_identity_contracts import DialogIdentityObservation as Observation\n"
        "def record(conn, baseline):\n"
        "    return publish(conn, 1, Observation(dialog_id=1, name='group'), baseline)\n"
    )
    assert _gate().violations_for(_gate().SOURCE_ROOT / "daemon_activity_stats.py", consumer) == []
    assert _gate().violations_for(_gate().SOURCE_ROOT / "event_handlers.py", producer) == []


def test_identity_tach_modules_have_only_downward_dependencies() -> None:
    import tomllib

    config = cast(
        dict[str, list[dict[str, object]]],
        tomllib.loads((Path(__file__).parents[1] / "tach.toml").read_text(encoding="utf-8")),
    )
    modules = {module["path"]: module for module in config["modules"]}
    assert modules["mcp_telegram.dialog_identity_contracts"]["depends_on"] == ["mcp_telegram.models"]
    assert modules["mcp_telegram.dialog_identity"]["depends_on"] == [
        "mcp_telegram.dialog_identity_contracts",
        "mcp_telegram.models",
    ]
    identity_dependencies = cast(list[str], modules["mcp_telegram.dialog_identity"]["depends_on"])
    assert "mcp_telegram.dialog_directory" not in identity_dependencies
