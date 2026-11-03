# pyright: reportAny=false

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_telegram.entity_profile.membership import MembershipFact, changed_members, membership_signal


@pytest.mark.parametrize(
    ("action", "sender", "expected"),
    [
        ('{"_":"MessageActionChatAddUser","users":[12,13,12]}', 99, {("joined", 12), ("joined", 13)}),
        ('{"users":[13,12],"_":"MessageActionChatAddUser"}', 99, {("joined", 12), ("joined", 13)}),
        ('{"_":"MessageActionChatDeleteUser","user_id":12}', 99, {("left", 12)}),
        ('{"_":"MessageActionChatJoinedByLink","inviter_id":99}', 12, {("joined", 12)}),
        ('{"_":"MessageActionChatJoinedByRequest"}', 12, {("joined", 12)}),
        ('{"_":"MessageActionChatJoinedByLink"}', None, set()),
        ('{"_":"MessageActionChatAddUser","users":[true,-1,"12"]}', 99, set()),
        ('{"_":"MessageActionPinMessage"}', 12, set()),
        ("bad json", 12, set()),
    ],
)
def test_membership_signal(action: str, sender: int | None, expected: set[tuple[str, int]]) -> None:
    assert membership_signal(1, action, sender) == expected
    assert membership_signal(0, action, sender) == set()


def test_semantic_changes_include_old_and_new_subjects() -> None:
    before = MembershipFact(1, '{"_":"MessageActionChatAddUser","users":[12]}', 99)
    replay = MembershipFact(1, '{"users":[12],"_":"MessageActionChatAddUser"}', 99)
    correction = MembershipFact(1, '{"_":"MessageActionChatDeleteUser","user_id":13}', 99)
    assert changed_members(None, before) == {12}
    assert changed_members(before, replay) == set()
    assert changed_members(before, correction) == {12, 13}


@pytest.mark.asyncio
async def test_repair_is_one_bounded_durable_owner_turn(tmp_path: Path) -> None:
    import json
    import sqlite3

    from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter, RefreshLimits
    from mcp_telegram.sync_db import ensure_sync_schema
    from mcp_telegram.sync_transactions import write_transaction
    from mcp_telegram.telegram_demand_progress import SliceOutcomeKind
    from tests.demand_adapter_helpers import run_ready_slice
    from tests.test_entity_profile_progressive import _test_service

    path = tmp_path / "repair.sqlite"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    service = _test_service(conn, limits=RefreshLimits(), account_id=77)
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=77 WHERE singleton=1")
        conn.executemany(
            "INSERT INTO messages(dialog_id,message_id,sent_at) VALUES (-5,?,1)", ((i,) for i in range(1, 66))
        )
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    adapter = EntityProfileDemandAdapter(coordinator)
    result = await run_ready_slice(adapter, now=100)
    assert result is not None and result.kind == SliceOutcomeKind.PROGRESSED
    state = json.loads(conn.execute("SELECT value FROM daemon_state WHERE key='profile_admission_v2'").fetchone()[0])
    assert state["membership_repair_v1"]["cursor"] == [-5, 32]
    assert state["membership_repair_v1"]["terminal"] == [-5, 65]
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 65
    resumed = _test_service(conn, limits=RefreshLimits(), account_id=77)
    resumed_coordinator = resumed.refresh_coordinator
    assert resumed_coordinator is not None
    resumed_adapter = EntityProfileDemandAdapter(resumed_coordinator)
    resumed_result = await run_ready_slice(resumed_adapter, now=100)
    assert resumed_result is not None and resumed_result.kind == SliceOutcomeKind.PROGRESSED
    state = json.loads(conn.execute("SELECT value FROM daemon_state WHERE key='profile_admission_v2'").fetchone()[0])
    assert state["membership_repair_v1"]["cursor"] == [-5, 64]
    assert state["membership_repair_v1"]["terminal"] == [-5, 65]
    terminal = await run_ready_slice(resumed_adapter, now=100)
    assert terminal is not None and terminal.kind == SliceOutcomeKind.TERMINAL
    assert resumed_adapter.status(100) is None
    conn.close()


def test_resumed_repair_seeks_to_cursor_without_scanning_archive(tmp_path: Path) -> None:
    import json
    import sqlite3

    from mcp_telegram.entity_profile.contracts import NORMALIZATION_VERSION
    from mcp_telegram.entity_profile.repository import _repair_retained_memberships
    from mcp_telegram.sync_db import ensure_sync_schema
    from mcp_telegram.sync_transactions import enable_runtime_writes, write_transaction

    path = tmp_path / "large-repair.sqlite"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    enable_runtime_writes(conn)
    with write_transaction(conn):
        conn.executemany(
            "INSERT INTO messages(dialog_id,message_id,sent_at) VALUES (-5,?,1)",
            ((i,) for i in range(1, 20_001)),
        )
        state = {
            "account": 77,
            "version": NORMALIZATION_VERSION,
            "membership_repair_v1": {
                "terminal": [-5, 20_000],
                "cursor": [-5, 19_968],
                "dialog_terminal": None,
                "dialog_cursor": None,
                "phase": "messages",
                "completed": False,
            },
        }
        conn.execute("INSERT INTO daemon_state(key,value) VALUES ('profile_admission_v2',?)", (json.dumps(state),))

    ticks = 0

    def count_instructions() -> int:
        nonlocal ticks
        ticks += 1
        return 0

    conn.set_progress_handler(count_instructions, 100)
    try:
        with write_transaction(conn):
            examined, _ = _repair_retained_memberships(conn, 77, now=100, limit=32)
    finally:
        conn.set_progress_handler(None, 0)
    assert examined == 32
    assert ticks < 150
    state = json.loads(conn.execute("SELECT value FROM daemon_state WHERE key='profile_admission_v2'").fetchone()[0])
    assert state["membership_repair_v1"]["cursor"] == [-5, 20_000]
    assert state["membership_repair_v1"]["completed"] is True
    conn.close()


@pytest.mark.asyncio
async def test_membership_job_captures_disabled_mode_and_survives_deleted_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3
    import time
    from dataclasses import replace
    from unittest.mock import AsyncMock

    from mcp_telegram.entity_profile.refresh import DurableRefreshSliceResult, RefreshLimits
    from mcp_telegram.entity_profile.repository import _repair_retained_memberships
    from mcp_telegram.messages.sqlite_bundle import insert_messages_with_fts, mark_message_deleted
    from mcp_telegram.sync_db import ensure_sync_schema
    from mcp_telegram.sync_transactions import write_transaction
    from tests.test_entity_profile_progressive import _test_service
    from tests.test_message_sqlite_persistence import _message

    path = tmp_path / "disabled-membership.sqlite"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    service = _test_service(conn, limits=RefreshLimits(), account_id=77)
    assert service._deps.enable_full_user_pair is False
    original = _message(1, text=None)
    join = replace(
        original,
        message=replace(
            original.message,
            dialog_id=-5,
            is_service=1,
            service_action='{"_":"MessageActionChatAddUser","users":[12]}',
        ),
    )
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=77 WHERE singleton=1")
        conn.execute("INSERT INTO entities(id,type,updated_at) VALUES (12,'user',1)")
        conn.execute("INSERT INTO dialogs(dialog_id,type) VALUES (12,'user')")
        conn.execute(
            "INSERT INTO entity_details(entity_id,detail_json,fetched_at,profile_owner_account_id) VALUES (12,'{}',1,77)"
        )
        conn.execute(
            "INSERT INTO entity_detail_sections(entity_id,section,status,payload_json) VALUES (12,'common_chats','fresh','{}')"
        )
        assert insert_messages_with_fts(conn, [join], profile_owner_account_id=77).membership_counts.queued == 1
        before = conn.execute(
            "SELECT generation,status,pair_mode FROM entity_profile_refresh_state WHERE entity_id=12"
        ).fetchone()
        assert before is not None and before[2] is None
        assert mark_message_deleted(conn, -5, 1, 300)
        examined, counts = _repair_retained_memberships(conn, 77, now=300, limit=32)
        assert examined == 1 and counts.received == 0
        assert (
            conn.execute(
                "SELECT generation,status,pair_mode FROM entity_profile_refresh_state WHERE entity_id=12"
            ).fetchone()
            == before
        )

    cursor = service._profiles.next_due_refresh(now=int(time.time()) + 1, account_id=77, entity_id=12)
    assert cursor is not None and cursor.pair_mode is None
    run_known = AsyncMock(return_value=DurableRefreshSliceResult(12))
    monkeypatch.setattr(service, "_run_known_entity_refresh", run_known)
    await service._run_selected_profile_owner(cursor, now=300)
    call = run_known.await_args
    assert call is not None and call.kwargs["pair_mode"] == "disabled"
    assert conn.execute("SELECT pair_mode FROM entity_profile_refresh_state WHERE entity_id=12").fetchone() == (
        "disabled",
    )
    conn.close()


def test_repair_access_loss_is_bounded_resumable_and_atomic(tmp_path: Path) -> None:  # noqa: PLR0915
    import json
    import sqlite3

    from mcp_telegram.entity_profile.repository import EntityProfileRepository
    from mcp_telegram.sync_db import ensure_sync_schema
    from mcp_telegram.sync_transactions import write_transaction

    path = tmp_path / "lost-repair.sqlite"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=77 WHERE singleton=1")
        conn.executemany(
            "INSERT INTO synced_dialogs(dialog_id,status,access_lost_at) VALUES (?,?,?)",
            ((i, "access_lost" if i in {-65, -1} else "synced", None if i == -1 else 300) for i in range(-65, 0)),
        )
        for peer, key in ((12, "chat:65"), (13, "chat:1"), (14, "chat:64")):
            conn.execute("INSERT INTO entities(id,type,updated_at) VALUES (?,'user',1)", (peer,))
            conn.execute("INSERT INTO dialogs(dialog_id,type) VALUES (?,'user')", (peer,))
            conn.execute(
                "INSERT INTO entity_details(entity_id,detail_json,fetched_at,profile_owner_account_id) "
                "VALUES (?,'{}',1,77)",
                (peer,),
            )
            conn.execute(
                "INSERT INTO entity_detail_sections(entity_id,section,status,payload_json) "
                "VALUES (?,'common_chats','fresh',?)",
                (peer, '{"published":{"owner_account_id":77,"generation":1,"scan":0,"observation_started_at":200}}'),
            )
            conn.execute(
                "INSERT INTO entity_profile_collection_facts(owner_account_id,entity_id,section,item_key,fields_json,coverage_json,observation_order,observed_at) "
                "VALUES (77,?,'common_chats',?,'{}','{}',1,100)",
                (peer, key),
            )
            conn.execute(
                "INSERT INTO entity_profile_collection_membership(owner_account_id,entity_id,section,generation,scan,item_key,identity_kind,page_key,ordinal,detail_complete) "
                "VALUES (77,?,'common_chats',1,0,?,'remote','first',0,1)",
                (peer, key),
            )
    repo = EntityProfileRepository(conn, section_ttl_seconds=300)
    initial = repo.membership_repair_revision(77)
    assert initial == "uninitialized"
    with pytest.raises(RuntimeError, match="rollback"):
        with write_transaction(conn):
            _, counts = repo.advance_membership_repair(77, initial, now=400, limit=100)
            assert counts.received == 1 and counts.queued == 1
            raise RuntimeError("rollback")
    assert repo.membership_repair_revision(77) == initial
    assert conn.execute("SELECT COUNT(*) FROM entity_profile_refresh_state").fetchone() == (0,)

    with write_transaction(conn):
        revision, counts = repo.advance_membership_repair(77, initial, now=400, limit=100)
        assert counts.received == 1 and counts.queued == 1
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status,access_lost_at) VALUES (100,'access_lost',400)")
    assert revision is not None
    state = json.loads(revision)
    assert state["dialog_cursor"] == -34 and state["dialog_terminal"] == -1
    conn.close()

    conn = sqlite3.connect(path)
    repo = EntityProfileRepository(conn, section_ttl_seconds=300)
    assert repo.membership_repair_revision(77) == revision
    with write_transaction(conn):
        revision, counts = repo.advance_membership_repair(77, revision, now=400, limit=32)
        assert counts.received == 0
    assert revision is not None
    assert json.loads(revision)["dialog_cursor"] == -2
    with write_transaction(conn):
        revision, counts = repo.advance_membership_repair(77, revision, now=400, limit=32)
        assert counts.received == 1 and counts.queued == 1
    assert revision == "complete"
    assert repo.membership_repair_revision(77) is None
    assert conn.execute("SELECT entity_id FROM entity_profile_refresh_state ORDER BY entity_id").fetchall() == [
        (12,),
        (13,),
    ]
    conn.close()


def test_message_horizon_is_captured_after_dialog_repair(tmp_path: Path) -> None:
    import json
    import sqlite3

    from mcp_telegram.entity_profile.repository import EntityProfileRepository
    from mcp_telegram.sync_db import ensure_sync_schema
    from mcp_telegram.sync_transactions import write_transaction

    path = tmp_path / "phase-order.sqlite"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=77 WHERE singleton=1")
        conn.executemany(
            "INSERT INTO synced_dialogs(dialog_id,status) VALUES (?,'synced')", ((i,) for i in range(-33, 0))
        )
        conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at) VALUES (-5,1,100)")
    repo = EntityProfileRepository(conn, section_ttl_seconds=300)
    with write_transaction(conn):
        revision, _ = repo.advance_membership_repair(77, "uninitialized", now=400, limit=32)
        assert revision is not None
        assert json.loads(revision)["phase"] == "dialogs"
        conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at) VALUES (-5,2,100)")
    with write_transaction(conn):
        revision, _ = repo.advance_membership_repair(77, revision, now=400, limit=32)
        assert revision is not None
        assert json.loads(revision)["terminal"] == [-5, 2]
        conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at) VALUES (-5,3,100)")
    with write_transaction(conn):
        revision, _ = repo.advance_membership_repair(77, revision, now=400, limit=32)
    assert revision == "complete"
    state = json.loads(conn.execute("SELECT value FROM daemon_state WHERE key='profile_admission_v2'").fetchone()[0])
    assert state["membership_repair_v1"]["cursor"] == [-5, 2]
    conn.close()


def test_empty_repair_finishes_once(tmp_path: Path) -> None:
    import sqlite3

    from mcp_telegram.entity_profile.repository import EntityProfileRepository
    from mcp_telegram.sync_db import ensure_sync_schema
    from mcp_telegram.sync_transactions import write_transaction

    path = tmp_path / "empty-repair.sqlite"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=77 WHERE singleton=1")
    repo = EntityProfileRepository(conn, section_ttl_seconds=300)
    with write_transaction(conn):
        revision, counts = repo.advance_membership_repair(77, "uninitialized", now=400, limit=32)
    assert revision == "complete" and counts.received == 0
    assert repo.membership_repair_revision(77) is None
    with write_transaction(conn):
        revision, _ = repo.advance_membership_repair(77, "uninitialized", now=400, limit=32)
    assert revision is None
    conn.close()
