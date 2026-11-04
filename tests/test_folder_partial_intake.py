"""Independent rule facts survive incomplete catalogues and racing refreshes."""

import sqlite3
from pathlib import Path
from typing import cast

import pytest
from telethon import types

from mcp_telegram.folders.commands import folder_catalog_version, read_folder
from mcp_telegram.folders.contracts import FolderRule, FolderRuleObservation
from mcp_telegram.folders.sqlite_repository import SQLiteFolderSnapshotRepository, read_folder_rules
from mcp_telegram.folders.telegram_adapter import folder_update_from_native
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_savepoint
from tests.test_telegram_update_fairness_delivery import _Delivery

pytest_plugins = ["tests.test_telegram_update_fairness_delivery"]


def _update(folder_id: int, title: str | None) -> object:
    rule = None if title is None else types.DialogFilter(folder_id, types.TextWithEntities(title, []), [], [], [])
    return types.UpdateDialogFilter(folder_id, rule)


def test_singular_update_survives_without_catalogue_and_stays_partial(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        repo = SQLiteFolderSnapshotRepository(conn)
        with write_savepoint(conn):
            assert repo.apply_supplied_update(
                folder_update_from_native(_update(4, "Known")), account_id=42, order=10, now=100
            )
        value = read_folder(conn, 42, 4)
        assert value["presence"] == "present"
        assert value["catalog_version"] is None
        assert cast(dict[str, object], value["definition"])["source_position"] is None
        assert folder_catalog_version(conn, 42) is None
        assert not repo.rules_are_fresh(now=100)
    finally:
        conn.close()


def test_old_snapshot_merges_unaffected_rule_but_does_not_resurrect_deleted(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE dialog_directory_publication SET account_id=42,generation=1,observation_started_at=1 WHERE singleton=1"
        )
        conn.commit()
        repo = SQLiteFolderSnapshotRepository(conn)
        with write_savepoint(conn):
            revision = repo.read_membership_revision()
            assert repo.apply_supplied_update(
                folder_update_from_native(_update(4, None)), account_id=42, order=11, now=100
            )
            observation = FolderRuleObservation(
                (FolderRule(4, "Old", source_position=0), FolderRule(5, "Other", source_position=1)), "old", 90
            )
            assert (
                repo.project_observation(
                    observation, completed_at=101, observation_order=10, membership_revision=revision
                )
                == 1
            )
        assert [(r.folder_id, r.title) for r in read_folder_rules(conn)] == [(5, "Other")]
        assert conn.execute(
            "SELECT deleted,observation_order FROM telegram_folder_rules WHERE account_id=42 AND folder_id=4"
        ).fetchone() == (1, 11)
        assert folder_catalog_version(conn, 42) is None
    finally:
        conn.close()


async def test_refresh_racing_rule_edit_keeps_both_facts_and_no_false_completion(tmp_path: Path) -> None:
    from mcp_telegram.folders.intake import capture_folder_intake
    from mcp_telegram.folders.refresh import FolderRefresher
    from mcp_telegram.observation_order import allocate_observation_order
    from mcp_telegram.sync_transactions import enable_runtime_writes
    from tests.sqlite_writer_helpers import inline_writer

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE dialog_directory_publication SET account_id=42,generation=1,observation_started_at=1 WHERE singleton=1"
        )
        conn.commit()
        enable_runtime_writes(conn)
        repo = SQLiteFolderSnapshotRepository(conn)

        class Gateway:
            async def fetch_rules(self, *, started_at: int) -> FolderRuleObservation:
                event = _update(4, "New")
                capture_folder_intake([event])
                with write_savepoint(conn):
                    order = allocate_observation_order(conn)
                    assert repo.apply_supplied_update(
                        folder_update_from_native(event), account_id=42, order=order, now=101
                    )
                return FolderRuleObservation(
                    (FolderRule(4, "Old", source_position=0), FolderRule(5, "Other", source_position=1)),
                    "snapshot",
                    started_at,
                )

        result = await FolderRefresher(Gateway(), repo, writer=inline_writer(conn)).refresh(completed_at=100)
        assert not result.accepted
        assert {rule.folder_id: rule.title for rule in read_folder_rules(conn)} == {4: "New", 5: "Other"}
        assert folder_catalog_version(conn, 42) is None
    finally:
        conn.close()


async def test_supplied_folder_before_catalogue_reaches_local_read(delivery: _Delivery) -> None:
    gate = delivery.wire[0]
    update = _update(4, "Known")
    processed = await gate._preprocess_updates([update], [], [])
    await gate._dispatch_update(processed[0])
    payload = read_folder(delivery.reader, 42, 4)
    assert payload["presence"] == "present"
    assert cast(dict[str, object], payload["definition"])["source_position"] is None
    assert payload["catalog_version"] is None
    assert not gate._delivery_failure.is_set()


async def test_partial_folder_position_is_null_through_registered_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jsonschema import Draft202012Validator

    from mcp_telegram import server, tools
    from mcp_telegram.tools._base import ToolResult

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        with write_savepoint(conn):
            assert SQLiteFolderSnapshotRepository(conn).apply_supplied_update(
                folder_update_from_native(_update(4, "Known")), account_id=42, order=10, now=100
            )
        payload = {"ok": True, **read_folder(conn, 42, 4)}

        async def fake_runner(_args: object) -> ToolResult:
            return ToolResult(structured_content=payload)

        monkeypatch.setattr(server.tools, "tool_runner", fake_runner)
        result = await server.call_tool("get_folder", {"folder_id": 4})
        content = cast(dict[str, object], result.structured_content)
        assert cast(dict[str, object], content["definition"])["source_position"] is None
        entry = tools.TOOL_REGISTRY["get_folder"]
        assert entry.output_schema is not None
        Draft202012Validator(entry.output_schema).validate(content)
    finally:
        conn.close()


def test_verified_account_change_retains_rules_and_reprojects_only_current_account(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        with write_savepoint(conn):
            conn.execute(
                "UPDATE dialog_directory_publication SET account_id=1,generation=1,observation_started_at=1 WHERE singleton=1"
            )
            repo = SQLiteFolderSnapshotRepository(conn)
            repo.project_observation(
                FolderRuleObservation((FolderRule(4, "A", source_position=0),), "a", 1), completed_at=1
            )
            assert repo.apply_supplied_update(
                folder_update_from_native(_update(4, "B")), account_id=2, order=10, now=100
            )
            assert read_folder(conn, 1, 4)["presence"] == "unknown"
            current = read_folder(conn, 2, 4)
            assert current["presence"] == "present"
            assert cast(dict[str, object], current["directory_coverage"])["lookup_complete"] is False
            assert conn.execute(
                "SELECT account_id,title FROM telegram_folder_rules WHERE folder_id=4 ORDER BY account_id"
            ).fetchall() == [(1, "A"), (2, "B")]
            conn.execute(
                "UPDATE dialog_directory_publication SET account_id=2,generation=2,observation_started_at=2 WHERE singleton=1"
            )
            assert repo.reproject_current_rules_in_transaction(now=101) == 2
        assert [rule.title for rule in read_folder_rules(conn)] == ["B"]
        assert folder_catalog_version(conn, 2) is None
    finally:
        conn.close()


def test_completed_snapshot_fills_partial_catalog_without_losing_rule(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        with write_savepoint(conn):
            repo = SQLiteFolderSnapshotRepository(conn)
            assert repo.apply_supplied_update(
                folder_update_from_native(_update(4, "Known")), account_id=42, order=10, now=100
            )
            conn.execute(
                "UPDATE dialog_directory_publication SET account_id=42,generation=1,observation_started_at=1 WHERE singleton=1"
            )
            observation = FolderRuleObservation(
                (FolderRule(4, "Known", source_position=0), FolderRule(5, "Other", source_position=1)), "full", 101
            )
            assert (
                repo.project_observation(
                    observation,
                    completed_at=102,
                    observation_order=11,
                    membership_revision=repo.read_membership_revision(),
                )
                == 1
            )
        assert [(r.folder_id, r.source_position) for r in read_folder_rules(conn)] == [(4, 0), (5, 1)]
        assert folder_catalog_version(conn, 42) is not None
    finally:
        conn.close()


async def test_intake_before_snapshot_commit_invalidates_only_its_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_telegram.folders.intake import capture_folder_intake
    from mcp_telegram.folders.refresh import FolderRefresher
    from mcp_telegram.sync_transactions import enable_runtime_writes
    from tests.sqlite_writer_helpers import inline_writer

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE dialog_directory_publication SET account_id=42,generation=1,observation_started_at=1 WHERE singleton=1"
        )
        conn.commit()
        enable_runtime_writes(conn)
        repo = SQLiteFolderSnapshotRepository(conn)
        original = SQLiteFolderSnapshotRepository.project_observation

        def publish_then_intake(  # noqa: PLR0913 - matches publication boundary
            self: SQLiteFolderSnapshotRepository,
            observation: FolderRuleObservation,
            *,
            completed_at: int,
            observation_order: int = 0,
            membership_revision: int | None = None,
            owner_account_id: int | None = None,
        ) -> int | None:
            result = original(
                self,
                observation,
                completed_at=completed_at,
                observation_order=observation_order,
                membership_revision=membership_revision,
                owner_account_id=owner_account_id,
            )
            capture_folder_intake([_update(4, "New")])
            return result

        monkeypatch.setattr(SQLiteFolderSnapshotRepository, "project_observation", publish_then_intake)

        class Gateway:
            async def fetch_rules(self, *, started_at: int) -> FolderRuleObservation:
                return FolderRuleObservation((FolderRule(4, "Old", source_position=0),), "old", started_at)

        result = await FolderRefresher(Gateway(), repo, writer=inline_writer(conn)).refresh(completed_at=100)
        assert not result.accepted and result.committed_revision is None
        assert folder_catalog_version(conn, 42) is None
        assert read_folder_rules(conn)[0].title == "Old"
        with write_savepoint(conn):
            assert repo.apply_supplied_update(
                folder_update_from_native(_update(4, "New")), account_id=42, order=100, now=101
            )
        assert read_folder_rules(conn)[0].title == "New"
        assert folder_catalog_version(conn, 42) is None
    finally:
        conn.close()


async def test_raced_cleanup_does_not_invalidate_newer_publication(tmp_path: Path) -> None:
    from collections.abc import Callable

    from mcp_telegram.folders.intake import capture_folder_intake
    from mcp_telegram.folders.refresh import FolderRefresher
    from mcp_telegram.sqlite_writer import SQLiteWriter
    from mcp_telegram.sync_transactions import enable_runtime_writes

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE dialog_directory_publication SET account_id=42,generation=1,observation_started_at=1 WHERE singleton=1"
        )
        conn.commit()
        enable_runtime_writes(conn)
        repo = SQLiteFolderSnapshotRepository(conn)

        class Writer:
            calls = 0

            async def run[T](self, operation: Callable[[sqlite3.Connection], T]) -> T:
                self.calls += 1
                result = operation(conn)
                if self.calls == 2:
                    event = _update(4, "New")
                    capture_folder_intake([event])
                    with write_savepoint(conn):
                        assert repo.apply_supplied_update(
                            folder_update_from_native(event), account_id=42, order=100, now=101
                        )
                return result

        class Gateway:
            async def fetch_rules(self, *, started_at: int) -> FolderRuleObservation:
                return FolderRuleObservation((FolderRule(4, "Old", source_position=0),), "old", started_at)

        result = await FolderRefresher(Gateway(), repo, writer=cast(SQLiteWriter, cast(object, Writer()))).refresh(
            completed_at=100
        )
        assert not result.accepted
        assert read_folder_rules(conn)[0].title == "New"
        assert folder_catalog_version(conn, 42) is not None
    finally:
        conn.close()


def test_snapshot_keeps_newer_value_and_unaffected_rule_with_complete_order(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        with write_savepoint(conn):
            conn.execute(
                "UPDATE dialog_directory_publication SET account_id=42,generation=1,observation_started_at=1 WHERE singleton=1"
            )
            repo = SQLiteFolderSnapshotRepository(conn)
            baseline = FolderRuleObservation(
                (FolderRule(4, "Old", source_position=0), FolderRule(5, "Other", source_position=1)), "baseline", 1
            )
            repo.project_observation(baseline, completed_at=1, observation_order=5)
            revision = repo.read_membership_revision()
            assert repo.apply_supplied_update(
                folder_update_from_native(_update(4, "New")), account_id=42, order=11, now=100
            )
            assert repo.read_membership_revision() == revision
            repo.project_observation(baseline, completed_at=101, observation_order=10, membership_revision=revision)
        assert [(rule.folder_id, rule.title, rule.source_position) for rule in read_folder_rules(conn)] == [
            (4, "New", 0),
            (5, "Other", 1),
        ]
        assert folder_catalog_version(conn, 42) is not None
    finally:
        conn.close()


def test_stale_rule_delete_does_not_invalidate_newer_value(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        with write_savepoint(conn):
            conn.execute(
                "UPDATE dialog_directory_publication SET account_id=42,generation=1,observation_started_at=1 WHERE singleton=1"
            )
            repo = SQLiteFolderSnapshotRepository(conn)
            baseline = FolderRuleObservation((FolderRule(4, "Old", source_position=0),), "baseline", 1)
            repo.project_observation(baseline, completed_at=1, observation_order=5)
            assert repo.apply_supplied_update(
                folder_update_from_native(_update(4, "New")), account_id=42, order=11, now=100
            )
            before = repo.read_catalog_version()
            assert repo.apply_supplied_update(
                folder_update_from_native(_update(4, None)), account_id=42, order=10, now=101
            )
            assert repo.read_catalog_version() == before
        assert read_folder_rules(conn)[0].title == "New"
    finally:
        conn.close()


async def test_worker_reuses_full_enumeration_while_directory_missing(tmp_path: Path) -> None:
    import asyncio

    from mcp_telegram.auth_scope import TelegramAuthScope
    from mcp_telegram.folders.refresh import FolderRefresher
    from mcp_telegram.folders.worker import FolderProjectionDemandAdapter, FolderProjectionWorker
    from mcp_telegram.sqlite_writer import SQLiteWriter
    from mcp_telegram.sync_db import open_sync_db_reader
    from tests.test_folder_projection_worker import _budget, _Policy

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    reader = open_sync_db_reader(path)
    writer = await SQLiteWriter.open(path)

    class Gateway:
        calls = 0

        async def fetch_rules(self, *, started_at: int) -> FolderRuleObservation:
            self.calls += 1
            return FolderRuleObservation((FolderRule(4, "Known", source_position=0),), "token", started_at)

    try:
        gateway = Gateway()
        repo = SQLiteFolderSnapshotRepository(reader)
        scope = TelegramAuthScope(1, 42, 2, 3)
        refresher = FolderRefresher(gateway, repo, writer=writer, auth_scope=lambda: scope)
        worker = FolderProjectionWorker(refresher, repo, asyncio.Event(), _Policy(), writer=writer, clock=lambda: 1000)
        adapter = FolderProjectionDemandAdapter(worker, account_id=42)
        await adapter.run_slice(_budget(adapter, 1000))
        status = adapter.status(1001)
        assert gateway.calls == 1 and status is not None and status.release_at > 1001
        assert read_folder(reader, 42, 4)["presence"] == "present"
        assert folder_catalog_version(reader, 42) is None

        def publish_directory(conn: sqlite3.Connection) -> None:
            with write_savepoint(conn):
                conn.execute(
                    "UPDATE dialog_directory_publication SET account_id=42,generation=1,observation_started_at=1 WHERE singleton=1"
                )

        await writer.run(publish_directory)
        assert await refresher.reproject_current_rules(now=1002) == 1
        assert folder_catalog_version(reader, 42) is not None
        assert gateway.calls == 1
    finally:
        await writer.aclose()
        reader.close()


def test_partial_order_vector_survives_missing_rule_and_older_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        with write_savepoint(conn):
            repo = SQLiteFolderSnapshotRepository(conn)
            assert repo.apply_supplied_update(
                folder_update_from_native(_update(4, "Four")), account_id=42, order=1, now=1
            )
            assert repo.apply_supplied_update(
                folder_update_from_native(types.UpdateDialogFilterOrder([5, 4])), account_id=42, order=11, now=2
            )
            row = cast(
                tuple[int, str, int] | None,
                conn.execute(
                    "SELECT account_id,filter_order_json,filter_order_observation_order FROM telegram_folder_projection_state"
                ).fetchone(),
            )
            assert row == (42, "[5, 4]", 11)
            conn.execute(
                "UPDATE dialog_directory_publication SET account_id=42,generation=1,observation_started_at=1 WHERE singleton=1"
            )
            old = FolderRuleObservation(
                (FolderRule(4, "Four", source_position=0), FolderRule(5, "Five", source_position=1)), "old", 1
            )
            repo.project_observation(old, completed_at=3, observation_order=10, membership_revision=0)
        rules = read_folder_rules(conn)
        assert [rule.folder_id for rule in rules] == [5, 4]
        assert all(rule.source_position is None for rule in rules)
        assert folder_catalog_version(conn, 42) is None
    finally:
        conn.close()


def test_known_order_delta_preserves_nonfilter_slots(tmp_path: Path) -> None:
    from mcp_telegram.folders.contracts import FolderRuleKind

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        with write_savepoint(conn):
            conn.execute(
                "UPDATE dialog_directory_publication SET account_id=42,generation=1,observation_started_at=1 WHERE singleton=1"
            )
            repo = SQLiteFolderSnapshotRepository(conn)
            rules = (
                FolderRule(0, "All", namespace="default", kind=FolderRuleKind.DEFAULT),
                FolderRule(4, "Four"),
                FolderRule(5, "Five"),
            )
            repo.project_observation(FolderRuleObservation(rules, "full", 1), completed_at=1, observation_order=1)
            assert repo.apply_supplied_update(
                folder_update_from_native(types.UpdateDialogFilterOrder([5, 4])), account_id=42, order=2, now=2
            )
        assert [(rule.folder_id, rule.source_position) for rule in read_folder_rules(conn)] == [(0, 0), (5, 1), (4, 2)]
        assert folder_catalog_version(conn, 42) is None
    finally:
        conn.close()


def test_folder_projections_hide_previous_account_without_current_scope(tmp_path: Path) -> None:
    from mcp_telegram.folders.read_repository import (
        dialog_placement,
        folder_snapshot,
        folder_summaries,
        folders_by_dialog,
    )

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        with write_savepoint(conn):
            conn.execute(
                "UPDATE dialog_directory_publication SET account_id=1,generation=1,observation_started_at=1 WHERE singleton=1"
            )
            repo = SQLiteFolderSnapshotRepository(conn)
            repo.project_observation(
                FolderRuleObservation((FolderRule(4, "A", source_position=0, included_ids=(55,)),), "a", 1),
                completed_at=1,
            )
        for account in (None, 2):
            assert folder_summaries(conn, account_id=account) == []
            assert folders_by_dialog(conn, [55], account_id=account) == {}
            assert dialog_placement(conn, 55, account_id=account)["folders"] == []
            assert folder_snapshot(conn, stale_after_seconds=900, account_id=account)["status"] == "unavailable"
    finally:
        conn.close()


def test_order_with_unknown_member_does_not_invent_absolute_positions(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        with write_savepoint(conn):
            conn.execute(
                "UPDATE dialog_directory_publication SET account_id=42,generation=1,observation_started_at=1 WHERE singleton=1"
            )
            repo = SQLiteFolderSnapshotRepository(conn)
            repo.project_observation(
                FolderRuleObservation((FolderRule(4, "Four"), FolderRule(5, "Five")), "full", 1),
                completed_at=1,
                observation_order=1,
            )
            assert repo.apply_supplied_update(
                folder_update_from_native(types.UpdateDialogFilterOrder([4, 9, 5])), account_id=42, order=2, now=2
            )
        assert [rule.source_position for rule in read_folder_rules(conn)] == [None, None]
        assert (
            conn.execute("SELECT filter_order_json FROM telegram_folder_projection_state").fetchone()[0] == "[4, 9, 5]"
        )
    finally:
        conn.close()


async def test_directory_generation_change_keeps_received_rules(tmp_path: Path) -> None:
    from mcp_telegram.folders.refresh import FolderRefresher
    from mcp_telegram.sync_transactions import enable_runtime_writes
    from tests.sqlite_writer_helpers import inline_writer

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "UPDATE dialog_directory_publication SET account_id=42,generation=1,observation_started_at=1 WHERE singleton=1"
        )
        conn.commit()
        enable_runtime_writes(conn)
        repo = SQLiteFolderSnapshotRepository(conn)

        class Gateway:
            async def fetch_rules(self, *, started_at: int) -> FolderRuleObservation:
                with write_savepoint(conn):
                    conn.execute(
                        "UPDATE dialog_directory_publication SET generation=2,observation_started_at=2 WHERE singleton=1"
                    )
                return FolderRuleObservation((FolderRule(4, "Received"),), "full", started_at)

        result = await FolderRefresher(Gateway(), repo, writer=inline_writer(conn)).refresh(completed_at=100)
        assert result.accepted and result.generation == 2
        assert read_folder_rules(conn)[0].title == "Received"
    finally:
        conn.close()


def test_late_definition_does_not_undo_newer_catalogue_omission(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        repo = SQLiteFolderSnapshotRepository(conn)
        with write_savepoint(conn):
            conn.execute(
                "UPDATE dialog_directory_publication SET account_id=42,generation=1,observation_started_at=1 WHERE singleton=1"
            )
            assert repo.apply_supplied_update(
                folder_update_from_native(_update(4, "First")), account_id=42, order=10, now=100
            )
            assert (
                repo.project_observation(
                    FolderRuleObservation((), "complete", 101),
                    completed_at=102,
                    observation_order=20,
                    membership_revision=repo.read_membership_revision(),
                )
                == 1
            )
            assert repo.apply_supplied_update(
                folder_update_from_native(_update(4, "Late")), account_id=42, order=15, now=103
            )
        assert read_folder_rules(conn) == ()
        assert conn.execute(
            "SELECT title,catalog_member,membership_order FROM telegram_folder_rules WHERE account_id=42 AND folder_id=4"
        ).fetchone() == ("Late", 0, 20)
        with write_savepoint(conn):
            assert repo.apply_supplied_update(
                folder_update_from_native(_update(4, "Current")), account_id=42, order=21, now=104
            )
        assert [(rule.folder_id, rule.title) for rule in read_folder_rules(conn)] == [(4, "Current")]
    finally:
        conn.close()
