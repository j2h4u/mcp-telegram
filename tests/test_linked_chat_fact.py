from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import cast

import pytest

from mcp_telegram.access_lifecycle import restore_access_after_revalidation, set_access_lost
from mcp_telegram.linked_chat_fact import (
    LinkedChatState,
    LinkedChatWork,
    capture_generation,
    defer,
    ensure_cold_demand,
    invalidate_from_update,
    linked_chat_fact_owner,
    next_due,
    next_release_at,
    publish,
    read_fact,
    validate_observation,
)
from mcp_telegram.sync_db import _apply_migrations


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _apply_migrations(conn)
    return conn


def test_publish_uses_fact_generation_not_unrelated_dialog_revision() -> None:
    conn = _db()
    channel_id = -1001234567890
    conn.execute("INSERT INTO dialogs(dialog_id, type) VALUES (?, 'channel')", (channel_id,))
    conn.commit()
    generation = capture_generation(conn, channel_id)

    # Normal dialog activity increments dialogs.revision but must not fence this fact.
    conn.execute("UPDATE dialogs SET needs_refresh=1 WHERE dialog_id=?", (channel_id,))
    conn.execute("INSERT INTO dialogs(dialog_id, type) VALUES (-1009876543210, 'channel')")
    assert publish(conn, channel_id, generation, 987654321, 100)

    fact = read_fact(conn, channel_id)
    assert fact.state is LinkedChatState.KNOWN_LINK
    assert fact.linked_chat_id == -1000987654321
    assert fact.resolved_at == 100
    assert not fact.refresh_pending
    conn.close()


def test_absent_dialog_row_cold_demand_is_idempotent_and_profile_can_satisfy_it() -> None:
    conn = _db()
    channel_id = -1001234567891
    old_generation = capture_generation(conn, channel_id)
    assert old_generation == 0
    assert ensure_cold_demand(conn, channel_id, 10)
    work = next_due(conn, 10)
    assert work == LinkedChatWork(channel_id, 0, 10)

    # A repeat cold consumer cannot advance the pending generation.
    assert not ensure_cold_demand(conn, channel_id, 11)
    assert publish(conn, channel_id, old_generation, 2222222222, 12)
    assert read_fact(conn, channel_id).linked_chat_id == -1002222222222
    assert not read_fact(conn, channel_id).refresh_pending
    conn.close()


def test_absent_dialog_newer_event_fences_profile_observation() -> None:
    conn = _db()
    channel_id = -1001234567895
    captured_generation = capture_generation(conn, channel_id)
    ensure_cold_demand(conn, channel_id, 10)
    invalidate_from_update(conn, channel_id, 11)

    assert not publish(conn, channel_id, captured_generation, 2222222222, 12)
    assert read_fact(conn, channel_id).state is LinkedChatState.UNKNOWN
    work = next_due(conn, 12)
    assert work is not None and work.generation == captured_generation + 1
    conn.close()


def test_new_event_invalidates_inflight_generation_and_failure_cannot_delay_it() -> None:
    conn = _db()
    channel_id = -1001234567892
    generation = capture_generation(conn, channel_id)
    invalidate_from_update(conn, channel_id, 20)
    work = next_due(conn, 20)
    assert work is not None and work.generation == generation + 1

    invalidate_from_update(conn, channel_id, 21)
    assert not defer(conn, work, 999)
    latest = next_due(conn, 21)
    assert latest is not None and latest.generation == work.generation + 1
    assert latest.retry_at == 21
    conn.close()


def test_retry_policy_uses_exponential_delay_and_caps_at_one_day() -> None:
    conn = _db()
    channel_id = -1001234567896
    ensure_cold_demand(conn, channel_id, 10)
    work = next_due(conn, 10)
    assert work is not None

    assert linked_chat_fact_owner.retry_at_for_failure(conn, work, 10) == 310
    assert defer(conn, work, 310)
    assert linked_chat_fact_owner.retry_at_for_failure(conn, work, 310) == 910

    conn.execute(
        "UPDATE linked_chat_fact_state SET failure_count=9 WHERE channel_id=?",
        (channel_id,),
    )
    assert linked_chat_fact_owner.retry_at_for_failure(conn, work, 1_000) == 87_400
    conn.close()


def test_retry_policy_clamps_flood_wait_and_falls_back_for_missing_retry_fields() -> None:
    conn = _db()
    channel_id = -1001234567897
    ensure_cold_demand(conn, channel_id, 10)
    work = next_due(conn, 10)
    assert work is not None

    assert linked_chat_fact_owner.retry_at_for_failure(conn, work, 100, flood_wait_seconds=None) == 400
    assert linked_chat_fact_owner.retry_at_for_failure(conn, work, 100, flood_wait_seconds=5) == 400
    assert linked_chat_fact_owner.retry_at_for_failure(conn, work, 100, flood_wait_seconds=100_000) == 86_500

    conn.execute(
        "UPDATE linked_chat_fact_state SET retry_at=NULL WHERE channel_id=?",
        (channel_id,),
    )
    fact = read_fact(conn, channel_id)
    assert fact.requested_at == 10
    assert fact.retry_at is None
    assert linked_chat_fact_owner.effective_retry_at(fact.retry_at, fact.requested_at, 100) == 310

    conn.execute(
        "UPDATE linked_chat_fact_state SET requested_at=NULL WHERE channel_id=?",
        (channel_id,),
    )
    assert next_release_at(conn) is None
    assert next_due(conn, 100) is None
    assert conn.execute("SELECT retry_at FROM linked_chat_fact_state WHERE channel_id=?", (channel_id,)).fetchone() == (
        400,
    )
    assert next_release_at(conn) == 400
    repaired_work = next_due(conn, 400)
    assert repaired_work == LinkedChatWork(channel_id, work.generation, 400)
    conn.close()


def test_access_lost_suspends_due_selection_and_release_until_restored() -> None:
    conn = _db()
    channel_id = -1001234567893
    ensure_cold_demand(conn, channel_id, 30)
    conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (?, 'access_lost')", (channel_id,))
    assert next_due(conn, 1000) is None
    assert next_release_at(conn) is None

    conn.execute("UPDATE synced_dialogs SET status='synced' WHERE dialog_id=?", (channel_id,))
    assert next_due(conn, 1000) is not None
    assert next_release_at(conn) == 30
    conn.close()


def test_access_lifecycle_suspends_and_restores_linked_chat_demand_without_changing_ledger() -> None:
    conn = _db()
    suspended_channel = -1001234567898
    active_channel = -1001234567899
    ensure_cold_demand(conn, suspended_channel, 100)
    ensure_cold_demand(conn, active_channel, 110)
    conn.execute(
        "UPDATE linked_chat_fact_state SET retry_at=? WHERE channel_id=?",
        (120, suspended_channel),
    )
    conn.execute(
        "UPDATE linked_chat_fact_state SET retry_at=? WHERE channel_id=?",
        (220, active_channel),
    )
    conn.commit()

    assert read_fact(conn, suspended_channel).suspended is False
    assert set_access_lost(conn, suspended_channel, 150, reason="test")
    suspended_fact = read_fact(conn, suspended_channel)
    assert suspended_fact.state is LinkedChatState.UNKNOWN
    assert suspended_fact.refresh_pending
    assert suspended_fact.suspended is True
    suspended_ledger = cast(
        tuple[int, int | None, int | None, int | None, int],
        conn.execute(
            "SELECT generation,pending_generation,requested_at,retry_at,failure_count "
            "FROM linked_chat_fact_state WHERE channel_id=?",
            (suspended_channel,),
        ).fetchone(),
    )
    assert next_due(conn, 300) == LinkedChatWork(active_channel, 0, 220)
    assert next_release_at(conn) == 220
    assert (
        conn.execute(
            "SELECT generation,pending_generation,requested_at,retry_at,failure_count "
            "FROM linked_chat_fact_state WHERE channel_id=?",
            (suspended_channel,),
        ).fetchone()
        == suspended_ledger
    )

    assert publish(conn, active_channel, 0, None, 300)
    assert next_due(conn, 300) is None

    assert restore_access_after_revalidation(conn, suspended_channel, 350)
    restored_fact = read_fact(conn, suspended_channel)
    assert restored_fact.state is LinkedChatState.UNKNOWN
    assert restored_fact.refresh_pending
    assert restored_fact.suspended is False
    assert (
        conn.execute(
            "SELECT generation,pending_generation,requested_at,retry_at,failure_count "
            "FROM linked_chat_fact_state WHERE channel_id=?",
            (suspended_channel,),
        ).fetchone()
        == suspended_ledger
    )
    assert next_due(conn, 350) == LinkedChatWork(suspended_channel, 0, 120)
    assert next_release_at(conn) == 120
    conn.close()


def test_publish_rolls_back_fact_ledger_and_sibling_detail_together() -> None:
    conn = _db()
    channel_id = -1001234567894
    generation = capture_generation(conn, channel_id)
    conn.execute("CREATE TABLE sibling_detail(channel_id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit()

    with pytest.raises(RuntimeError, match="injected failure"):
        with conn:
            assert publish(conn, channel_id, generation, 4444444444, 40)
            conn.execute("INSERT INTO sibling_detail VALUES (?, 'new')", (channel_id,))
            raise RuntimeError("injected failure")

    assert read_fact(conn, channel_id).state is LinkedChatState.UNKNOWN
    assert conn.execute(
        "SELECT generation,pending_generation FROM linked_chat_fact_state WHERE channel_id=?", (channel_id,)
    ).fetchone() == (0, None)
    assert conn.execute("SELECT * FROM sibling_detail").fetchall() == []
    conn.close()


def test_full_channel_payload_requires_matching_full_chat_id_and_valid_link_field() -> None:
    matching_chat = SimpleNamespace(id=123456789)
    wrong_full = SimpleNamespace(full_chat=SimpleNamespace(id=123456788, linked_chat_id=None), chats=[matching_chat])
    with pytest.raises(ValueError, match="different channel"):
        validate_observation(wrong_full, -1000123456789)

    valid_none = SimpleNamespace(full_chat=SimpleNamespace(id=123456789, linked_chat_id=0), chats=[matching_chat])
    assert validate_observation(valid_none, -1000123456789) == 0
    missing_link = SimpleNamespace(full_chat=SimpleNamespace(id=123456789), chats=[matching_chat])
    with pytest.raises(ValueError, match="malformed"):
        validate_observation(missing_link, -1000123456789)
    invalid_bool = SimpleNamespace(full_chat=SimpleNamespace(id=123456789, linked_chat_id=True), chats=[matching_chat])
    with pytest.raises(ValueError, match="malformed"):
        validate_observation(invalid_bool, -1000123456789)
