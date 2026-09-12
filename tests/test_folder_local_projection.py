"""Focused contract tests for the local canonical folder projection."""

from __future__ import annotations

import sqlite3
from pathlib import Path

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
from mcp_telegram.folders.sqlite_repository import SQLiteFolderSnapshotRepository
from mcp_telegram.folders.telegram_adapter import TelethonTelegramFolderGateway
from mcp_telegram.sync_db import ensure_sync_schema


def _conn(path: Path) -> sqlite3.Connection:
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE dialog_directory_publication SET account_id=1,generation=7,observation_started_at=10,observation_completed_at=11 WHERE singleton=1"
    )
    return conn


def _observation(*rules: FolderRule, started_at: int = 100) -> FolderRuleObservation:
    return FolderRuleObservation(tuple(rules), "rule-token", started_at)


def test_three_valued_precedence_and_missing_explicit_peer() -> None:
    rule = FolderRule(4, "Work", included_ids=(99,), excluded_ids=(7,), categories=frozenset({DialogCategory.CONTACT}), exclude_archived=True)
    assert evaluate(rule, DialogFacts(7, DialogCategory.CONTACT), now=10) is MembershipState.ABSENT
    assert evaluate(rule, DialogFacts(99), now=10) is MembershipState.PRESENT
    assert evaluate(rule, DialogFacts(2), now=10) is MembershipState.UNKNOWN
    assert evaluate(FolderRule(5, "None"), DialogFacts(3, DialogCategory.CONTACT), now=10) is MembershipState.ABSENT


def test_projection_retains_unknown_and_custom_pin_order(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "sync.db")
    try:
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


def test_pending_rule_observation_is_preserved_without_catalog(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "sync.db")
    try:
        conn.execute("UPDATE dialog_directory_publication SET account_id=NULL,generation=NULL,observation_started_at=NULL WHERE singleton=1")
        repo = SQLiteFolderSnapshotRepository(conn)
        assert repo.project_observation(_observation(FolderRule(1, "A"), started_at=55), completed_at=56) is None
        assert conn.execute("SELECT token,started_at FROM telegram_folder_pending_observation").fetchone() == ("rule-token", 55)
        assert repo.rules_are_fresh(now=954)
        assert not repo.rules_are_fresh(now=955)
    finally:
        conn.close()


def test_default_uses_catalog_and_main_pin_order(tmp_path: Path) -> None:
    conn = _conn(tmp_path / "sync.db")
    try:
        conn.executemany(
            "INSERT INTO dialog_directory_facts(dialog_id,category,archived,unread,mute_until,observed_at) VALUES (?,?,?,?,?,?)",
            [(40, "group", 0, 0, 0, 10), (10, "bot", 0, 1, 0, 10)],
        )
        conn.executemany(
            "INSERT INTO dialog_directory_published_pins(folder_id,dialog_id,position) VALUES (0,?,?)", [(40, 1), (10, 0)]
        )
        repo = SQLiteFolderSnapshotRepository(conn)
        rule = FolderRule(0, "All chats", DEFAULT_FOLDER_NAMESPACE, FolderRuleKind.DEFAULT)
        repo.project_observation(_observation(rule), completed_at=100)
        assert conn.execute("SELECT dialog_id,pin_position FROM telegram_folder_local_members ORDER BY pin_position").fetchall() == [(10, 0), (40, 1)]
    finally:
        conn.close()


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
        conn.execute("INSERT INTO dialog_directory_facts VALUES (1,'contact',0,1,200,10)")
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
