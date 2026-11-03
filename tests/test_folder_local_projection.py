"""Focused contract tests for the local canonical folder projection."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.folders.contracts import (
    DEFAULT_FOLDER_NAMESPACE,
    DialogCategory,
    DialogFacts,
    FolderRule,
    FolderRuleKind,
    FolderRuleObservation,
    MembershipState,
)
from mcp_telegram.folders.membership import evaluate
from mcp_telegram.folders.read_repository import folder_snapshot
from mcp_telegram.folders.sqlite_repository import SQLiteFolderSnapshotRepository, read_folder_rules
from mcp_telegram.folders.telegram_adapter import (
    TelethonTelegramFolderGateway,
    _observation_token,
    folder_update_from_native,
)
from mcp_telegram.sync_db import _apply_migration_97, ensure_sync_schema
from mcp_telegram.sync_transactions import write_savepoint


def _conn(path: Path) -> sqlite3.Connection:
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    with write_savepoint(conn):
        conn.execute(
            "UPDATE dialog_directory_publication SET account_id=1,generation=7,observation_started_at=10,observation_completed_at=11 WHERE singleton=1"
        )
    return conn


def _observation(*rules: FolderRule, started_at: int = 100) -> FolderRuleObservation:
    return FolderRuleObservation(tuple(rules), "rule-token", started_at)


def test_migration_marks_preexisting_folder_metadata_unknown(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "sync.db")
    try:
        SQLiteFolderSnapshotRepository(conn).project_observation(
            _observation(FolderRule(4, "Retained")), completed_at=100
        )
        conn.execute("DELETE FROM schema_version WHERE version=97")
        conn.execute("ALTER TABLE telegram_folder_projection_state DROP COLUMN catalog_revision")
        conn.execute(
            "CREATE TABLE telegram_folder_pending_observation (singleton INTEGER PRIMARY KEY, token TEXT, started_at INTEGER, rules_json TEXT)"
        )
        conn.execute("INSERT INTO telegram_folder_pending_observation VALUES (1,'unbound',2,'{}')")
        conn.execute("UPDATE telegram_folder_projection_state SET next_retry_at=105,consecutive_failures=2")
        conn.execute("INSERT INTO update_delivery VALUES (1,1,1,'checkpoint',?)", (b'{"version":1,"tl":"x"}',))
        conn.execute("INSERT INTO update_delivery_tokens VALUES (0,3,7)")
        conn.commit()
        assert _apply_migration_97(conn, 96) == 97
        assert conn.execute(
            "SELECT safe_snapshot,json_type(CAST(pending AS TEXT),'$.auth_scope') FROM update_delivery"
        ).fetchone() == ("checkpoint", "null")
        assert conn.execute("SELECT * FROM update_delivery_tokens").fetchall() == [(0, 3, 7)]
        assert (
            conn.execute("SELECT 1 FROM sqlite_master WHERE name='telegram_folder_pending_observation'").fetchone()
            is None
        )
        (rule,) = read_folder_rules(conn)
        assert rule.title == "Retained"
        assert not rule.complete
        repo = SQLiteFolderSnapshotRepository(conn)
        assert not repo.rules_are_fresh(now=101)
        assert repo.read_next_retry_at() == 105 and repo.read_consecutive_failures() == 2
        assert repo.read_snapshot_revision() == (1, 7, "rule-token", 100)
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('folder_command_receipts','contact_note_operation_receipts') ORDER BY name"
        ).fetchall() == [("contact_note_operation_receipts",), ("folder_command_receipts",)]
    finally:
        conn.close()


def test_migration_rolls_back_schema_and_retained_data_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mcp_telegram.folders.command_schema as receipts

    conn = _conn(tmp_path / "sync.db")
    try:
        SQLiteFolderSnapshotRepository(conn).project_observation(_observation(FolderRule(4, "Kept")), completed_at=100)
        conn.execute("DELETE FROM schema_version WHERE version=97")
        conn.execute("ALTER TABLE telegram_folder_projection_state DROP COLUMN catalog_revision")
        conn.execute(
            "CREATE TABLE telegram_folder_pending_observation (singleton INTEGER PRIMARY KEY, token TEXT, started_at INTEGER, rules_json TEXT)"
        )
        conn.commit()
        monkeypatch.setattr(receipts, "FOLDER_COMMAND_SCHEMA", "invalid SQL")
        with pytest.raises(sqlite3.OperationalError):
            _apply_migration_97(conn, 96)
        assert conn.execute("SELECT title FROM telegram_folder_rules").fetchone() == ("Kept",)
        assert conn.execute("SELECT 1 FROM schema_version WHERE version=97").fetchone() is None
        assert "catalog_revision" not in {
            row[1]
            for row in cast(
                list[tuple[object, ...]], conn.execute("PRAGMA table_info(telegram_folder_projection_state)").fetchall()
            )
        }
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='telegram_folder_pending_observation'"
        ).fetchone()
    finally:
        conn.close()


async def test_native_folder_metadata_survives_storage(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from telethon.tl.types import DialogFilter, MessageEntityCustomEmoji, TextWithEntities

    folder = DialogFilter(
        id=4,
        title=TextWithEntities("Work", [MessageEntityCustomEmoji(0, 2, 123)]),
        pinned_peers=[],
        include_peers=[],
        exclude_peers=[],
        contacts=True,
        title_noanimate=True,
        emoticon="📁",
        color=3,
    )

    class Client:
        async def __call__(self, request: object) -> object:
            return SimpleNamespace(filters=[folder])

    observed = await TelethonTelegramFolderGateway(Client()).fetch_rules(started_at=20)
    conn = _conn(tmp_path / "sync.db")
    try:
        SQLiteFolderSnapshotRepository(conn).project_observation(observed, completed_at=21)
        (stored,) = read_folder_rules(conn)
        assert stored.title_entities == (
            {"_": "MessageEntityCustomEmoji", "offset": 0, "length": 2, "document_id": 123},
        )
        assert (stored.title_noanimate, stored.emoticon, stored.color, stored.complete) == (True, "📁", 3, True)
    finally:
        conn.close()


async def test_unknown_folder_preserves_previous_snapshot(tmp_path: Path) -> None:
    from types import SimpleNamespace

    class Client:
        async def __call__(self, request: object) -> object:
            return SimpleNamespace(filters=[SimpleNamespace()])

    conn = _conn(tmp_path / "sync.db")
    try:
        repo = SQLiteFolderSnapshotRepository(conn)
        repo.project_observation(_observation(FolderRule(4, "Retained")), completed_at=100)
        unknown = await TelethonTelegramFolderGateway(Client()).fetch_rules(started_at=101)
        assert not unknown.complete
        assert repo.project_observation(unknown, completed_at=101) is None
        assert read_folder_rules(conn)[0].title == "Retained"
        assert folder_snapshot(conn, stale_after_seconds=900, now=101)["complete"] is False
    finally:
        conn.close()


def test_supplied_folder_update_is_ordered_and_local(tmp_path: Path) -> None:
    from telethon.tl.types import DialogFilter, TextWithEntities, UpdateDialogFilter, UpdateDialogFilterOrder

    conn = _conn(tmp_path / "sync.db")
    try:
        repo = SQLiteFolderSnapshotRepository(conn)
        repo.project_observation(_observation(FolderRule(4, "Old")), completed_at=100)
        folder = DialogFilter(4, TextWithEntities("New", []), [], [], [])
        with write_savepoint(conn):
            assert repo.apply_supplied_update(
                folder_update_from_native(UpdateDialogFilter(4, folder)), account_id=1, order=2, now=101
            )
            assert not repo.apply_supplied_update(
                folder_update_from_native(UpdateDialogFilter(4)), account_id=1, order=1, now=102
            )
            assert not repo.apply_supplied_update(
                folder_update_from_native(UpdateDialogFilter(4)), account_id=2, order=3, now=102
            )
            assert not repo.apply_supplied_update(
                folder_update_from_native(UpdateDialogFilterOrder([5])), account_id=1, order=3, now=102
            )
            assert repo.apply_supplied_update(
                folder_update_from_native(UpdateDialogFilterOrder([4])), account_id=1, order=3, now=102
            )
            assert not repo.apply_supplied_update(folder_update_from_native(object()), account_id=1, order=4, now=102)
        assert read_folder_rules(conn)[0].title == "New"
    finally:
        conn.close()


def test_supplied_delta_refines_only_its_matching_receipt(tmp_path: Path) -> None:
    from telethon.tl.types import DialogFilter, TextWithEntities, UpdateDialogFilter

    from mcp_telegram.folders.sqlite_repository import encode_folder_definition

    conn = _conn(tmp_path / "sync.db")
    try:
        repo = SQLiteFolderSnapshotRepository(conn)
        repo.project_observation(_observation(FolderRule(4, "Old"), FolderRule(5, "Other")), completed_at=100)
        with write_savepoint(conn):
            conn.executemany(
                "INSERT INTO folder_command_receipts(account_id,operation_key,dc_id,key_id,action,target,semantic_fingerprint,binding_revision,outcome,verification,dispatch_order,intended_definition,created_at,updated_at) VALUES (1,?,2,'3','update',?,'hash',1,'unknown','exhausted',1,?,1,1)",
                [
                    ("first", 4, encode_folder_definition(FolderRule(4, "New"))),
                    ("second", 5, encode_folder_definition(FolderRule(5, "Other"))),
                ],
            )
            native = DialogFilter(4, TextWithEntities("New", []), [], [], [])
            assert repo.apply_supplied_update(
                folder_update_from_native(UpdateDialogFilter(4, native)), account_id=1, order=2, now=101
            )
        assert conn.execute(
            "SELECT operation_key,outcome,verification FROM folder_command_receipts ORDER BY operation_key"
        ).fetchall() == [("first", "unknown", "matched"), ("second", "unknown", "exhausted")]
    finally:
        conn.close()


def test_invalidated_facts_survive_local_reprojection(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "sync.db")
    try:
        repo = SQLiteFolderSnapshotRepository(conn)
        repo.project_observation(_observation(FolderRule(4, "Retained")), completed_at=100)
        with write_savepoint(conn):
            repo.invalidate()
        assert not repo.rules_are_fresh(now=101)
        repo.reproject_current_rules(now=102)
        assert read_folder_rules(conn)[0].title == "Retained"
        assert not folder_snapshot(conn, stale_after_seconds=900, now=102)["complete"]
    finally:
        conn.close()


def test_catalog_version_changes_for_semantic_aba_but_not_reobservation(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "sync.db")
    try:
        repo = SQLiteFolderSnapshotRepository(conn)
        a = FolderRuleObservation((FolderRule(4, "A"),), "token-a", 100)
        b = FolderRuleObservation((FolderRule(4, "B"),), "token-b", 101)
        repo.project_observation(a, completed_at=100)
        first = repo.read_catalog_version()
        repo.project_observation(a, completed_at=101)
        assert repo.read_catalog_version() == first
        repo.project_observation(b, completed_at=102)
        repo.project_observation(a, completed_at=103)
        assert repo.read_catalog_version()[1] == first[1] + 2
        with write_savepoint(conn):
            repo.invalidate()
        invalidated = repo.read_catalog_version()
        assert not invalidated[3] and invalidated[1] == first[1] + 3
        repo.reproject_current_rules(now=104)
        assert repo.read_catalog_version() == invalidated
    finally:
        conn.close()


def test_native_filter_and_chatlist_definitions_preserve_supported_fields() -> None:
    from telethon.tl import types

    from mcp_telegram.folders.sqlite_repository import decode_folder_definition, encode_folder_definition

    peers = [types.InputPeerUser(23, 987654), types.InputPeerChannel(12, 876543), types.InputPeerChat(42)]
    title = types.TextWithEntities("Hi🙂", [types.MessageEntityBold(0, 2), types.MessageEntityCustomEmoji(2, 2, 987)])
    variants = [
        types.DialogFilter(
            4,
            title,
            peers[:1],
            peers[1:],
            peers[:1],
            contacts=True,
            non_contacts=True,
            groups=True,
            broadcasts=True,
            bots=True,
            exclude_muted=True,
            exclude_read=True,
            exclude_archived=True,
            title_noanimate=True,
            emoticon="📁",
            color=4,
        ),
        types.DialogFilterChatlist(
            5, title, peers[:1], peers[1:], has_my_invites=True, title_noanimate=True, emoticon="📁", color=3
        ),
    ]
    for native in variants:
        rule = folder_update_from_native(types.UpdateDialogFilter(native.id, native)).rule
        assert rule is not None and rule.complete
        definition = encode_folder_definition(rule)
        assert decode_folder_definition(definition) == rule
        assert "987654" not in definition and "876543" not in definition
        assert rule.pinned_ids == (23,)
        assert rule.included_ids == (-1000000000012, -42)
        assert rule.title_noanimate and len(rule.title_entities) == 2
    first = folder_update_from_native(types.UpdateDialogFilter(variants[0].id, variants[0])).rule
    second = folder_update_from_native(types.UpdateDialogFilter(variants[1].id, variants[1])).rule
    assert first is not None and first.excluded_ids == (23,)
    assert second is not None and second.has_my_invites


def test_three_valued_precedence_and_missing_explicit_peer() -> None:
    rule = FolderRule(
        4,
        "Work",
        included_ids=(99,),
        excluded_ids=(7,),
        categories=frozenset({DialogCategory.CONTACT}),
        exclude_archived=True,
    )
    assert evaluate(rule, DialogFacts(7, DialogCategory.CONTACT), now=10) is MembershipState.ABSENT
    assert evaluate(rule, DialogFacts(99), now=10) is MembershipState.PRESENT
    assert evaluate(rule, DialogFacts(2), now=10) is MembershipState.UNKNOWN
    assert evaluate(FolderRule(5, "None"), DialogFacts(3, DialogCategory.CONTACT), now=10) is MembershipState.ABSENT


def test_projection_retains_unknown_and_custom_pin_order(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "sync.db")
    try:
        with write_savepoint(conn):
            conn.executemany(
                "INSERT INTO dialog_directory_facts(dialog_id,category,archived,unread,mute_until,observed_at) VALUES (?,?,?,?,?,?)",
                [(3, "contact", 0, 1, 0, 10), (8, None, 0, 1, 0, 10)],
            )
        repo = SQLiteFolderSnapshotRepository(conn)
        rule = FolderRule(4, "Work", pinned_ids=(8, 3), categories=frozenset({DialogCategory.CONTACT}))
        assert repo.project_observation(_observation(rule), completed_at=100) == 7
        rows = conn.execute(
            "SELECT dialog_id,state,pin_position FROM telegram_folder_local_members WHERE namespace='filter' AND folder_id=4 ORDER BY pin_position"
        ).fetchall()
        assert rows == [(8, "present", 0), (3, "present", 1)]
    finally:
        conn.close()


def test_unbound_observation_is_not_replayed_into_another_account(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "sync.db")
    try:
        with write_savepoint(conn):
            conn.execute(
                "UPDATE dialog_directory_publication SET account_id=NULL,generation=NULL,observation_started_at=NULL WHERE singleton=1"
            )
        repo = SQLiteFolderSnapshotRepository(conn)
        assert repo.project_observation(_observation(FolderRule(2, "Unbound")), completed_at=100) is None
        with write_savepoint(conn):
            conn.execute(
                "UPDATE dialog_directory_publication SET account_id=2,generation=8,observation_started_at=101 WHERE singleton=1"
            )
        assert repo.reproject_current_rules(now=102) is None
        assert read_folder_rules(conn) == ()
        assert repo.read_last_success_at() is None
    finally:
        conn.close()


def test_default_uses_catalog_and_main_pin_order(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "sync.db")
    try:
        with write_savepoint(conn):
            conn.executemany(
                "INSERT INTO dialog_directory_facts(dialog_id,category,archived,unread,mute_until,observed_at) VALUES (?,?,?,?,?,?)",
                [(40, "group", 0, 0, 0, 10), (10, "bot", 0, 1, 0, 10)],
            )
        with write_savepoint(conn):
            conn.executemany("INSERT INTO dialogs(dialog_id,type,hidden) VALUES (?, 'group', 0)", [(40,), (10,)])
        with write_savepoint(conn):
            conn.executemany(
                "INSERT INTO dialog_directory_published_pins(folder_id,dialog_id,position) VALUES (0,?,?)",
                [(40, 1), (10, 0)],
            )
        repo = SQLiteFolderSnapshotRepository(conn)
        rule = FolderRule(0, "All chats", DEFAULT_FOLDER_NAMESPACE, FolderRuleKind.DEFAULT)
        repo.project_observation(_observation(rule), completed_at=100)
        assert conn.execute(
            "SELECT dialog_id,pin_position FROM telegram_folder_local_members ORDER BY pin_position"
        ).fetchall() == [(10, 0), (40, 1)]
    finally:
        conn.close()


def test_visible_dialog_without_eligibility_facts_is_not_omitted(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "sync.db")
    try:
        with write_savepoint(conn):
            conn.execute("INSERT INTO dialogs(dialog_id,type,hidden) VALUES (77,'user',0)")
        repo = SQLiteFolderSnapshotRepository(conn)
        default = FolderRule(0, "All chats", DEFAULT_FOLDER_NAMESPACE, FolderRuleKind.DEFAULT)
        filtered = FolderRule(2, "Contacts", categories=frozenset({DialogCategory.CONTACT}))
        repo.project_observation(_observation(default, filtered), completed_at=100)
        assert conn.execute(
            "SELECT namespace,folder_id,state FROM telegram_folder_local_members WHERE dialog_id=77 ORDER BY folder_id"
        ).fetchall() == [("default", 0, "present"), ("filter", 2, "unknown")]
    finally:
        conn.close()


def test_default_excludes_archived_dialogs_and_marks_unknown_archive_unknown(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "sync.db")
    try:
        with write_savepoint(conn):
            conn.executemany(
                "INSERT INTO dialogs(dialog_id,type,hidden,archived) VALUES (?,'user',0,?)",
                [(1, 0), (2, 1), (3, 0)],
            )
        with write_savepoint(conn):
            conn.execute("INSERT INTO dialog_directory_facts VALUES (3,NULL,NULL,NULL,NULL,NULL)")
        repo = SQLiteFolderSnapshotRepository(conn)
        default = FolderRule(0, "All chats", DEFAULT_FOLDER_NAMESPACE, FolderRuleKind.DEFAULT)
        repo.project_observation(_observation(default), completed_at=100)
        assert conn.execute(
            "SELECT dialog_id,state FROM telegram_folder_local_members ORDER BY dialog_id"
        ).fetchall() == [(1, "present"), (3, "present")]
        assert evaluate(default, DialogFacts(9, archived=None), now=100) is MembershipState.UNKNOWN
    finally:
        conn.close()


def test_folder_observation_token_changes_when_only_title_changes() -> None:
    assert _observation_token((FolderRule(1, "Old"),)) != _observation_token((FolderRule(1, "New"),))


def test_canonical_and_rule_receipts_turn_stale_at_exactly_900_seconds(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "sync.db")
    try:
        repo = SQLiteFolderSnapshotRepository(conn)
        repo.project_observation(_observation(FolderRule(1, "A"), started_at=100), completed_at=100)
        assert folder_snapshot(conn, stale_after_seconds=3_600, now=909)["status"] == "fresh"
        assert folder_snapshot(conn, stale_after_seconds=3_600, now=910)["status"] == "stale"
    finally:
        conn.close()


def test_mute_expiry_reprojects_without_rule_rpc(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "sync.db")
    try:
        with write_savepoint(conn):
            conn.execute("INSERT INTO dialog_directory_facts VALUES (1,'contact',0,1,200,10)")
        with write_savepoint(conn):
            conn.execute("INSERT INTO dialogs(dialog_id,type,hidden) VALUES (1,'user',0)")
        repo = SQLiteFolderSnapshotRepository(conn)
        rule = FolderRule(1, "Unmuted", categories=frozenset({DialogCategory.CONTACT}), exclude_muted=True)
        repo.project_observation(_observation(rule), completed_at=100)
        assert conn.execute("SELECT COUNT(*) FROM telegram_folder_local_members").fetchone()[0] == 0
        repo.ensure_mute_projection(now=200)
        assert conn.execute("SELECT dialog_id,state FROM telegram_folder_local_members").fetchall() == [(1, "present")]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_telethon_adapter_uses_exactly_one_rule_rpc() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[object] = []

        async def __call__(self, request: object) -> object:
            self.calls.append(request)
            return type("Response", (), {"filters": ()})()

    client = Client()
    observation = await TelethonTelegramFolderGateway(client).fetch_rules(started_at=7)
    assert len(client.calls) == 1
    assert observation.rules == ()
    assert observation.started_at == 7


@pytest.mark.asyncio
async def test_telethon_adapter_rejects_custom_filter_id_reserved_for_archive() -> None:
    class Client:
        async def __call__(self, request: object) -> object:
            del request
            folder = type("DialogFilter", (), {"id": 1, "title": "Collision"})()
            return type("Response", (), {"filters": (folder,)})()

    with pytest.raises(ValueError, match="reserved for the Telegram archive"):
        await TelethonTelegramFolderGateway(Client()).fetch_rules(started_at=7)
