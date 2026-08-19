# pyright: reportAny=false
"""Focused tests for message SQL and Telegram-content ownership gates."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _gate() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "check_message_boundaries.py"
    spec = importlib.util.spec_from_file_location("check_message_boundaries", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "source",
    [
        'conn.execute("SELECT text FROM messages WHERE dialog_id = ?")',
        'conn.execute(f"SELECT text " f"FROM messages m WHERE m.id = {message_id}")',
        'conn.execute("SELECT 1 FROM main.messages")',
        'query = "SELECT 1 FROM messages"\nconn.execute(query)',
    ],
)
def test_rogue_message_sql_is_rejected(source: str) -> None:
    gate = _gate()
    path = gate.SOURCE_ROOT / "rogue.py"
    findings = gate.violations_for(path, source)
    assert any("FROM/JOIN messages" in finding.message for finding in findings)


def test_join_messages_is_rejected_and_messages_fts_is_not_a_match() -> None:
    gate = _gate()
    path = gate.SOURCE_ROOT / "rogue.py"
    join_findings = gate.violations_for(
        path, 'conn.execute("SELECT 1 FROM messages_fts f JOIN messages m ON m.id = f.id")'
    )
    assert any("FROM/JOIN messages" in finding.message for finding in join_findings)

    fts_findings = gate.violations_for(path, 'conn.execute("SELECT 1 FROM messages_fts")')
    assert not any("FROM/JOIN messages" in finding.message for finding in fts_findings)


def test_qualified_message_join_is_found_after_another_qualified_table() -> None:
    gate = _gate()
    path = gate.SOURCE_ROOT / "rogue.py"
    source = 'conn.execute("SELECT 1 FROM main.dialogs d JOIN main.messages m ON m.dialog_id = d.id")'
    findings = gate.violations_for(path, source)
    assert any("FROM/JOIN messages" in finding.message for finding in findings)


def test_current_query_owner_is_allowed() -> None:
    gate = _gate()
    path = gate.SOURCE_ROOT / "daemon_message_queries.py"
    assert not any(
        "FROM/JOIN messages" in finding.message
        for finding in gate.violations_for(path, 'SQL = "SELECT 1 FROM messages"')
    )


def test_current_legacy_exception_is_named_but_new_path_is_rejected() -> None:
    gate = _gate()
    legacy = gate.SOURCE_ROOT / "daemon.py"
    assert not any(
        "FROM/JOIN messages" in finding.message
        for finding in gate.violations_for(legacy, 'SQL = "SELECT 1 FROM messages"')
    )

    rogue = gate.SOURCE_ROOT / "new_worker.py"
    assert any(
        "FROM/JOIN messages" in finding.message
        for finding in gate.violations_for(rogue, 'SQL = "SELECT 1 FROM messages"')
    )


def test_stale_sql_allowlist_entries_are_reported() -> None:
    gate = _gate()
    assert gate.stale_sql_allowlist_entries(
        {"current_owner.py"},
        owner_paths={"stale_owner.py"},
        legacy_paths={"legacy.py"},
    ) == ["legacy.py", "stale_owner.py"]


def test_content_marker_dict_is_rejected_outside_wrapper() -> None:
    gate = _gate()
    path = gate.SOURCE_ROOT / "rogue.py"
    findings = gate.violations_for(
        path,
        '{"text": text, "is_telegram_content": True, "content_kind": "message_text"}',
    )
    assert any("Telegram content dictionaries" in finding.message for finding in findings)


def test_manual_content_constructor_is_rejected_outside_projector() -> None:
    gate = _gate()
    rogue = gate.SOURCE_ROOT / "rogue.py"
    findings = gate.violations_for(rogue, "MessageContent(text=text, media_description=None, kind='message_text')")
    assert any("MessageContent must be produced" in finding.message for finding in findings)

    projector = gate.SOURCE_ROOT / "message_content.py"
    assert not gate.violations_for(projector, "MessageContent(text=text, media_description=None, kind='message_text')")


def test_content_wrapper_and_schema_are_allowed() -> None:
    gate = _gate()
    wrapper = gate.SOURCE_ROOT / "tools" / "structured.py"
    wrapper_source = 'return {"text": text, "is_telegram_content": True, "content_kind": content_kind}'
    assert not gate.violations_for(wrapper, wrapper_source)

    schema = gate.SOURCE_ROOT / "tools" / "discovery.py"
    schema_source = '{"is_telegram_content": {"type": "boolean"}, "content_kind": {"type": "string"}}'
    assert not gate.violations_for(schema, schema_source)


def test_projection_imports_have_single_owner() -> None:
    gate = _gate()
    rogue = gate.SOURCE_ROOT / "rogue.py"
    source = "from .text_projection import render_text_links\nfrom .telegram_message_projection import message_to_dict"
    findings = gate.violations_for(rogue, source)
    assert any("canonical projection seam" in finding.message for finding in findings)
    assert any("canonical owner" in finding.message for finding in findings)


def test_baseline_has_no_message_boundary_violations() -> None:
    gate = _gate()
    assert gate.boundary_violations(gate.SOURCE_ROOT) == []
