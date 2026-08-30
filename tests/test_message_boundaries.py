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


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE messages SET text = ? WHERE dialog_id = ?",
        "INSERT INTO message_versions (dialog_id, message_id) VALUES (?, ?)",
        "DELETE FROM messages WHERE dialog_id = ?",
        "SELECT old_text FROM message_versions WHERE dialog_id = ?",
    ],
)
def test_message_table_dml_is_rejected_outside_owner(sql: str) -> None:
    gate = _gate()
    findings = gate.violations_for(gate.SOURCE_ROOT / "rogue.py", f'conn.execute("{sql}")')
    assert any("messages/message_versions SQL" in finding.message for finding in findings)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE OR IGNORE messages SET text = ?",
        "UPDATE OR REPLACE main.message_versions SET old_text = ?",
    ],
)
def test_sqlite_update_conflict_forms_are_rejected_for_message_tables(sql: str) -> None:
    gate = _gate()
    findings = gate.violations_for(gate.SOURCE_ROOT / "rogue.py", f'conn.execute("{sql}")')
    assert any("messages/message_versions SQL" in finding.message for finding in findings)


def test_sqlite_update_conflict_form_for_other_table_is_not_a_message_match() -> None:
    gate = _gate()
    findings = gate.violations_for(
        gate.SOURCE_ROOT / "rogue.py",
        'conn.execute("UPDATE OR REPLACE main.dialogs SET pinned = ?")',
    )
    assert not any("messages/message_versions SQL" in finding.message for finding in findings)


def test_current_query_owner_is_allowed() -> None:
    gate = _gate()
    path = gate.SOURCE_ROOT / "reading/sqlite_projection.py"
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

    event_handlers = gate.SOURCE_ROOT / "event_handlers.py"
    assert any(
        "messages/message_versions SQL" in finding.message
        for finding in gate.violations_for(event_handlers, 'SQL = "UPDATE messages SET text = ?"')
    )


def test_schema_owner_is_separate_from_runtime_legacy_exceptions() -> None:
    gate = _gate()
    assert "event_handlers.py" not in gate.MESSAGE_SQL_LEGACY_EXCEPTION_PATHS
    assert "sync_db.py" not in gate.MESSAGE_SQL_LEGACY_EXCEPTION_PATHS
    assert "sync_db.py" in gate.MESSAGE_SQL_SCHEMA_OWNER_PATHS
    assert gate.violations_for(gate.SOURCE_ROOT / "sync_db.py", 'SQL = "UPDATE messages SET text = ?"') == []


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


def test_raw_message_body_wrapper_is_rejected_in_delivery_tools() -> None:
    gate = _gate()
    path = gate.SOURCE_ROOT / "tools" / "folders.py"
    findings = gate.violations_for(path, 'telegram_content(message.text, "message_text")')
    assert any("message bodies must use serialize_message_content" in finding.message for finding in findings)


@pytest.mark.parametrize(
    "source",
    [
        (
            "from .structured import telegram_content as tc\n"
            "def _structured_messages(message):\n"
            "    return tc(message.text, 'message_text')\n"
        ),
        (
            "from .structured import telegram_content\n"
            "def _structured_messages(message):\n"
            "    body = message.media_description\n"
            "    return telegram_content(body, 'media_description')\n"
        ),
        (
            "from .structured import telegram_content\n"
            "def _structured_messages(message):\n"
            "    def wrap(value):\n"
            "        return telegram_content(value, 'message_text')\n"
            "    return wrap(message.text)\n"
        ),
        (
            "from .structured import telegram_content\n"
            "def _structured_messages(message):\n"
            "    make = telegram_content\n"
            "    return make(message.text, 'message_text')\n"
        ),
    ],
)
def test_message_body_wrapper_aliases_and_helpers_are_rejected(source: str) -> None:
    gate = _gate()
    findings = gate.violations_for(gate.SOURCE_ROOT / "tools" / "unread.py", source)
    assert any("message bodies must use serialize_message_content" in finding.message for finding in findings)


def test_message_entrypoint_must_call_shared_serializer() -> None:
    gate = _gate()
    source = "def _structured_messages(message):\n    return {'text': message.text}\n"
    findings = gate.violations_for(gate.SOURCE_ROOT / "tools" / "unread.py", source)
    assert any("must call project_message_view" in finding.message for finding in findings)


def test_metadata_wrapper_allowlist_does_not_hide_message_body_bypass() -> None:
    gate = _gate()
    source = (
        "from .structured import telegram_content\n"
        "def _structured_reactions(message):\n"
        "    return telegram_content(message.text, 'reaction')\n"
    )
    findings = gate.violations_for(gate.SOURCE_ROOT / "tools" / "unread.py", source)
    assert any("message bodies must use serialize_message_content" in finding.message for finding in findings)


def test_legitimate_metadata_call_remains_allowed_with_serializer_entrypoint() -> None:
    gate = _gate()
    source = (
        "from .message_view import project_message_view\n"
        "def _structured_messages(message):\n"
        "    return project_message_view(message)\n"
        "def _structured_reactions(display):\n"
        "    return display\n"
    )
    assert gate.violations_for(gate.SOURCE_ROOT / "tools" / "unread.py", source) == []


@pytest.mark.parametrize(
    "write",
    [
        "item = {'sender': 'parallel label'}",
        "item['sent_at'] = 1",
        "item.update({'read_markers': []})",
        "item.update(content={'text': 'raw'})",
        "item.setdefault('sender', 'parallel label')",
        "item.update(dict(sent_at=1))",
        "item = dict(dialog_id=1, sent_at=1)",
        "item.pop('sender')",
        "del item['read_at']",
    ],
)
def test_canonical_message_entrypoint_cannot_overwrite_presenter_fields(write: str) -> None:
    gate = _gate()
    source = (
        "from .message_view import project_message_view\n"
        "def _structured_messages(message):\n"
        "    item = project_message_view(message)\n"
        f"    {write}\n"
        "    return item\n"
    )
    findings = gate.violations_for(gate.SOURCE_ROOT / "tools" / "unread.py", source)
    assert any("must be owned by project_message_view" in finding.message for finding in findings)


def test_list_message_page_composer_cannot_overwrite_presenter_fields() -> None:
    gate = _gate()
    source = (
        "from .message_view import project_message_view\n"
        "def _list_message_structured_item(message):\n"
        "    return project_message_view(message)\n"
        "def _list_messages_structured_messages(message):\n"
        "    item = _list_message_structured_item(message)\n"
        "    item['sender'] = 'parallel label'\n"
        "    return [item]\n"
    )
    findings = gate.violations_for(gate.SOURCE_ROOT / "tools" / "reading.py", source)
    assert any("must be owned by project_message_view" in finding.message for finding in findings)


def test_list_message_presenter_extension_allows_only_lifecycle_fields() -> None:
    gate = _gate()
    source = (
        "from .message_view import project_message_view\n"
        "def _list_message_structured_item(message):\n"
        "    item = project_message_view(message)\n"
        "    item.update({'visibility': 'chat_visible'})\n"
        "    item.update({'unexpected': True})\n"
        "    return item\n"
    )
    findings = gate.violations_for(gate.SOURCE_ROOT / "tools" / "reading.py", source)
    assert any(
        "extension field 'unexpected' is not an allowed lifecycle field" in finding.message for finding in findings
    )
    assert not any("extension field 'visibility'" in finding.message for finding in findings)


@pytest.mark.parametrize(
    "merge",
    [
        "item.update(patch)",
        "item.update(build_patch())",
        "item.update(dict(patch))",
        "item.update({**patch})",
        "item.update(**patch)",
    ],
)
def test_message_view_update_fails_closed_for_unknown_mapping_keys(merge: str) -> None:
    """Catch ordinary accidental bypasses without pretending to prove provenance."""
    gate = _gate()
    source = (
        "from .message_view import project_message_view\n"
        "def _structured_messages(message, patch):\n"
        "    item = project_message_view(message)\n"
        f"    {merge}\n"
        "    return item\n"
    )
    findings = gate.violations_for(gate.SOURCE_ROOT / "tools" / "unread.py", source)
    assert any("update keys must be a statically known literal mapping" in finding.message for finding in findings)


def test_filter_shaped_literal_cannot_be_merged_into_message_view() -> None:
    gate = _gate()
    source = (
        "from .message_view import project_message_view\n"
        "def _structured_messages(message):\n"
        "    item = project_message_view(message)\n"
        "    item.update({'exact_dialog_id': 1, 'sender': 'parallel', 'sender_id': 2, "
        "'exact_topic_id': 3, 'topic': 'parallel'})\n"
        "    return item\n"
    )
    findings = gate.violations_for(gate.SOURCE_ROOT / "tools" / "unread.py", source)
    assert any("canonical field 'sender'" in finding.message for finding in findings)
    assert any("canonical field 'topic'" in finding.message for finding in findings)
    assert not any("update keys must be a statically known literal mapping" in finding.message for finding in findings)


@pytest.mark.parametrize(
    "merge",
    [
        "item.update(patch, sender='parallel')",
        "item.update({**patch, 'sender': 'parallel'})",
    ],
)
def test_opaque_update_preserves_known_presenter_overwrite_keys(merge: str) -> None:
    gate = _gate()
    source = (
        "from .message_view import project_message_view\n"
        "def _structured_messages(message, patch):\n"
        "    item = project_message_view(message)\n"
        f"    {merge}\n"
        "    return item\n"
    )
    findings = gate.violations_for(gate.SOURCE_ROOT / "tools" / "unread.py", source)
    assert any("canonical field 'sender'" in finding.message for finding in findings)
    assert any("update keys must be a statically known literal mapping" in finding.message for finding in findings)


@pytest.mark.parametrize(
    "construction",
    [
        "{**project_message_view(message), 'sender': 'parallel'}",
        "dict(project_message_view(message), sender='parallel')",
    ],
)
def test_opaque_construction_preserves_known_presenter_overwrite_keys(construction: str) -> None:
    gate = _gate()
    source = (
        "from .message_view import project_message_view\n"
        "def _structured_messages(message):\n"
        f"    return {construction}\n"
    )
    findings = gate.violations_for(gate.SOURCE_ROOT / "tools" / "unread.py", source)
    assert any("canonical field 'sender'" in finding.message for finding in findings)


def test_message_view_ior_fails_closed_for_unknown_mapping_keys() -> None:
    gate = _gate()
    source = (
        "from .message_view import project_message_view\n"
        "def _structured_messages(message, patch):\n"
        "    item = project_message_view(message)\n"
        "    item |= patch\n"
        "    return item\n"
    )
    findings = gate.violations_for(gate.SOURCE_ROOT / "tools" / "unread.py", source)
    assert any("|= keys must be a statically known literal mapping" in finding.message for finding in findings)


def test_list_message_ior_allows_known_lifecycle_literal() -> None:
    gate = _gate()
    source = (
        "from .message_view import project_message_view\n"
        "from .search_hit import SEARCH_HIT_SCHEMA, project_search_hit\n"
        "SEARCH_MESSAGES_OUTPUT_SCHEMA = {'properties': {'results': {'items': SEARCH_HIT_SCHEMA}}}\n"
        "def _list_message_structured_item(message):\n"
        "    item = project_message_view(message)\n"
        "    item |= {'visibility': 'chat_visible'}\n"
        "    return item\n"
        "def _search_result_structured_rows(rows, query):\n"
        "    return [project_search_hit(row, query, lifecycle={}) for row in rows]\n"
    )
    assert gate.violations_for(gate.SOURCE_ROOT / "tools" / "reading.py", source) == []


def test_noop_presenter_call_does_not_excuse_manually_returned_envelope() -> None:
    gate = _gate()
    source = (
        "from .message_view import project_message_view\n"
        "def _structured_messages(message):\n"
        "    project_message_view(message)\n"
        "    return dict(dialog_id=message.dialog_id, sent_at=message.sent_at)\n"
    )
    findings = gate.violations_for(gate.SOURCE_ROOT / "tools" / "unread.py", source)
    assert any("canonical field 'sent_at'" in finding.message for finding in findings)


def test_local_helper_cannot_hide_presenter_field_mutation() -> None:
    gate = _gate()
    source = (
        "from .message_view import project_message_view\n"
        "def _manual_sender(item):\n"
        "    item.setdefault('sender', 'parallel label')\n"
        "    return item\n"
        "def _structured_messages(message):\n"
        "    return _manual_sender(project_message_view(message))\n"
    )
    findings = gate.violations_for(gate.SOURCE_ROOT / "tools" / "unread.py", source)
    assert any("canonical field 'sender'" in finding.message for finding in findings)


@pytest.mark.parametrize(
    "source",
    [
        (
            "from .message_view import project_message_view as present\n"
            "def _structured_messages(message):\n"
            "    return present(message)\n"
        ),
        (
            "import mcp_telegram.tools.message_view as message_view\n"
            "def _structured_messages(message):\n"
            "    return message_view.project_message_view(message)\n"
        ),
    ],
)
def test_presenter_alias_and_module_qualified_calls_are_noncanonical(source: str) -> None:
    gate = _gate()
    findings = gate.violations_for(gate.SOURCE_ROOT / "tools" / "unread.py", source)
    assert any("canonical direct import and name" in finding.message for finding in findings)
    assert any("must call project_message_view" in finding.message for finding in findings)


def test_boundary_gate_fields_match_canonical_message_view_schema() -> None:
    from mcp_telegram.tools.message_view import MESSAGE_VIEW_SCHEMA

    gate = _gate()
    properties = MESSAGE_VIEW_SCHEMA["properties"]
    assert isinstance(properties, dict)
    assert frozenset(properties) == gate.CANONICAL_MESSAGE_VIEW_FIELDS


def _search_hit_fixture(
    mapper_body: str = "    return [project_search_hit(row, query, lifecycle={}) for row in rows]\n",
    *,
    search_import: str = "from .search_hit import SEARCH_HIT_SCHEMA, project_search_hit\n",
    schema_items: str = "SEARCH_HIT_SCHEMA",
    helpers: str = "",
) -> str:
    return (
        "from .message_view import project_message_view\n"
        f"{search_import}"
        f"SEARCH_MESSAGES_OUTPUT_SCHEMA = {{'properties': {{'results': {{'items': {schema_items}}}}}}}\n"
        "def _list_message_structured_item(message):\n"
        "    return project_message_view(message)\n"
        f"{helpers}"
        "def _search_result_structured_rows(rows, query):\n"
        f"{mapper_body}"
    )


def test_search_hit_boundary_accepts_canonical_seams_with_benign_helpers() -> None:
    gate = _gate()
    source = _search_hit_fixture(
        helpers=(
            "def _unrelated_metadata():\n"
            "    return {'dialog_name': 'benign', 'anchor_call': {}}\n"
        )
    )

    assert gate.violations_for(gate.SOURCE_ROOT / "tools" / "reading.py", source) == []


@pytest.mark.parametrize(
    "search_import",
    [
        "from .search_hit import project_search_hit\n",
        "from .search_hit import SEARCH_HIT_SCHEMA\n",
        "from .search_hit import SEARCH_HIT_SCHEMA as HIT_SCHEMA, project_search_hit as project\n",
    ],
)
def test_search_hit_boundary_requires_both_canonical_direct_imports(search_import: str) -> None:
    gate = _gate()
    source = _search_hit_fixture(search_import=search_import)
    findings = gate.violations_for(gate.SOURCE_ROOT / "tools" / "reading.py", source)
    assert any("directly import canonical" in finding.message for finding in findings)


def test_search_hit_boundary_rejects_copied_result_item_schema() -> None:
    gate = _gate()
    source = _search_hit_fixture(schema_items="{'type': 'object', 'properties': {}}")

    findings = gate.violations_for(gate.SOURCE_ROOT / "tools" / "reading.py", source)

    assert any("results.items must reference SEARCH_HIT_SCHEMA" in finding.message for finding in findings)


@pytest.mark.parametrize(
    "mapper_body",
    [
        "    return rows\n",
        "    return [project_search_hit(rows[0], query, lifecycle={})]\n",
        "    return [row for row in rows]\n",
        (
            "    return [\n"
            "        project_search_hit(row, query, lifecycle={}) if row.get('canonical') else row\n"
            "        for row in rows\n"
            "    ]\n"
        ),
        "    return [_manual(project_search_hit(row, query, lifecycle={})) for row in rows]\n",
    ],
)
def test_search_hit_boundary_requires_direct_projector_list_comprehension(mapper_body: str) -> None:
    gate = _gate()
    source = _search_hit_fixture(mapper_body)

    findings = gate.violations_for(gate.SOURCE_ROOT / "tools" / "reading.py", source)

    assert any("directly project every list-comprehension item" in finding.message for finding in findings)


def test_serializer_call_does_not_excuse_second_raw_wrapper() -> None:
    gate = _gate()
    source = (
        "from .structured import serialize_message_content, telegram_content\n"
        "def _structured_messages(row):\n"
        "    serialize_message_content(row.get('text'), row.get('media_description'))\n"
        "    return telegram_content(str(row.get('text')), 'message_text')\n"
    )
    findings = gate.violations_for(gate.SOURCE_ROOT / "tools" / "unread.py", source)
    assert any("message bodies must use serialize_message_content" in finding.message for finding in findings)


def test_module_alias_and_row_subscript_body_bypass_is_rejected() -> None:
    gate = _gate()
    source = (
        "import mcp_telegram.tools.structured as structured\n"
        "def _structured_messages(row):\n"
        "    body = str(row['text'])\n"
        "    return structured.telegram_content(body, 'message_text')\n"
    )
    findings = gate.violations_for(gate.SOURCE_ROOT / "tools" / "unread.py", source)
    assert any("message bodies must use serialize_message_content" in finding.message for finding in findings)


def test_new_surface_entrypoint_cannot_bypass_owner() -> None:
    gate = _gate()
    source = (
        "from .structured import telegram_content\n"
        "def get_inbox(row):\n"
        "    return telegram_content(row.get('text'), 'message_text')\n"
    )
    findings = gate.violations_for(gate.SOURCE_ROOT / "tools" / "new_surface.py", source)
    assert any("message bodies must use serialize_message_content" in finding.message for finding in findings)


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


def test_scheduled_mapper_must_use_canonical_read_projector() -> None:
    gate = _gate()
    path = gate.SOURCE_ROOT / "reading/scheduled_projection.py"
    source = "def scheduled_row_to_wire(row):\n    return dict(row)\n"
    findings = gate.violations_for(path, source)
    assert any("must call project_read_message_content directly" in finding.message for finding in findings)


def test_scheduled_mapper_rejects_a_private_content_renderer_or_serializer() -> None:
    gate = _gate()
    path = gate.SOURCE_ROOT / "reading/scheduled_projection.py"
    source = (
        "from .tools.structured import serialize_message_content\n"
        "def scheduled_row_to_wire(row):\n"
        "    project_read_message_content(row)\n"
        "    return serialize_message_content(row.text, row.media_description, 'message_text')\n"
    )
    findings = gate.violations_for(path, source)
    assert any("must not import private renderer/serializer symbols" in finding.message for finding in findings)


def test_scheduled_mapper_rejects_aliased_private_serializer() -> None:
    gate = _gate()
    path = gate.SOURCE_ROOT / "reading/scheduled_projection.py"
    source = (
        "from .tools.structured import serialize_message_content as encode_content\n"
        "def scheduled_row_to_wire(row):\n"
        "    project_read_message_content(row)\n"
        "    return encode_content(row.text, row.media_description, 'message_text')\n"
    )
    findings = gate.violations_for(path, source)
    assert any("must not import private renderer/serializer symbols" in finding.message for finding in findings)


def test_scheduled_mapper_requires_exact_projector_provenance_and_rejects_shadowing() -> None:
    gate = _gate()
    path = gate.SOURCE_ROOT / "reading/scheduled_projection.py"
    wrong_import = (
        "from .message_content import project_read_message_content\n"
        "def scheduled_row_to_wire(row):\n"
        "    message = project_read_message_content(row)\n"
        "    return dataclasses.asdict(message)\n"
    )
    findings = gate.violations_for(path, wrong_import)
    assert any("imported exactly from ..daemon_message" in finding.message for finding in findings)

    shadowed = (
        "from .daemon_message import project_read_message_content\n"
        "project_read_message_content = object()\n"
        "def scheduled_row_to_wire(row):\n"
        "    message = project_read_message_content(row)\n"
        "    return dataclasses.asdict(message)\n"
    )
    findings = gate.violations_for(path, shadowed)
    assert any("binding is shadowed locally" in finding.message for finding in findings)

    shadowed_definition = (
        "from .daemon_message import project_read_message_content\n"
        "def project_read_message_content(row):\n"
        "    return row\n"
        "def scheduled_row_to_wire(row):\n"
        "    message = project_read_message_content(row)\n"
        "    return dataclasses.asdict(message)\n"
    )
    findings = gate.violations_for(path, shadowed_definition)
    assert any("binding is shadowed locally" in finding.message for finding in findings)


def test_scheduled_mapper_requires_exactly_one_module_entrypoint() -> None:
    gate = _gate()
    path = gate.SOURCE_ROOT / "reading/scheduled_projection.py"
    missing = "from .daemon_message import project_read_message_content\n"
    findings = gate.violations_for(path, missing)
    assert any("exactly one module-level scheduled_row_to_wire" in finding.message for finding in findings)
    duplicate = (
        "from .daemon_message import project_read_message_content\n"
        "def scheduled_row_to_wire(row):\n"
        "    return project_read_message_content(row)\n"
        "def scheduled_row_to_wire(row):\n"
        "    return project_read_message_content(row)\n"
    )
    findings = gate.violations_for(path, duplicate)
    assert any("exactly one module-level scheduled_row_to_wire" in finding.message for finding in findings)


@pytest.mark.parametrize(
    "source",
    [
        (
            "import mcp_telegram.message_content as content\n"
            "def rogue(value):\n"
            "    return content.MessageContent(text=value, media_description=None, kind='message_text')\n"
        ),
        (
            "import mcp_telegram.message_content\n"
            "def rogue(value):\n"
            "    return mcp_telegram.message_content.MessageContent(text=value, media_description=None, kind='message_text')\n"
        ),
        (
            "from .message_content import TelegramContent as Ctor\n"
            "Alias = Ctor\n"
            "def rogue(value):\n"
            "    return Alias(text=value, media_description=None, kind='message_text')\n"
        ),
    ],
)
def test_manual_content_constructors_reject_module_and_assignment_aliases(source: str) -> None:
    gate = _gate()
    findings = gate.violations_for(gate.SOURCE_ROOT / "rogue.py", source)
    assert any("MessageContent must be produced" in finding.message for finding in findings)


def test_baseline_has_no_message_boundary_violations() -> None:
    gate = _gate()
    assert gate.boundary_violations(gate.SOURCE_ROOT) == []


@pytest.mark.parametrize(
    ("relative_path", "entrypoint"),
    [
        ("tools/activity.py", "_structured_comment"),
        ("tools/account_trace.py", "_attach_trace_content_metadata"),
    ],
)
def test_activity_trace_delivery_entrypoints_require_one_canonical_serializer(
    relative_path: str, entrypoint: str
) -> None:
    gate = _gate()
    path = gate.SOURCE_ROOT / relative_path
    missing = f"def {entrypoint}(value):\n    return value\n"
    assert any(
        "must call serialize_message_content" in finding.message for finding in gate.violations_for(path, missing)
    )

    duplicate = (
        f"def {entrypoint}(value):\n"
        "    serialize_message_content(None, None, 'none')\n"
        "    return serialize_message_content(None, None, 'none')\n"
    )
    assert any("exactly once" in finding.message for finding in gate.violations_for(path, duplicate))


@pytest.mark.parametrize("relative_path", ["tools/activity.py", "tools/account_trace.py"])
def test_activity_trace_delivery_forbids_projector_imports_and_calls(relative_path: str) -> None:
    gate = _gate()
    path = gate.SOURCE_ROOT / relative_path
    imported = "from ..message_content import MessageSnapshot, project_message_content\n"
    called = "def _structured_comment(value):\n    return project_message_content(value)\n"
    findings = gate.violations_for(path, imported + called)
    assert any("must not import message_content projectors" in finding.message for finding in findings)
    assert any("must not call message_content projectors" in finding.message for finding in findings)

    module_alias = (
        "import mcp_telegram.message_content as mc\n"
        "def _structured_comment(value):\n"
        "    return mc.project_message_content(value)\n"
    )
    alias_findings = gate.violations_for(path, module_alias)
    assert any("must not import message_content projectors" in finding.message for finding in alias_findings)
    assert any("must not call message_content projectors" in finding.message for finding in alias_findings)
