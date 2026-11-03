"""Native folder facts through the real delivery owner and SQLite writer."""

import json
import sqlite3
import time
from typing import cast

import pytest
from telethon import types

from mcp_telegram.contact_notes.state import read_note
from mcp_telegram.folders.contracts import FolderRule, FolderRuleObservation
from mcp_telegram.folders.read_repository import folder_snapshot
from mcp_telegram.folders.sqlite_repository import SQLiteFolderSnapshotRepository, read_folder_rules
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_delivery import DeliveryEnvelope
from tests.test_telegram_update_fairness_delivery import _Delivery
from tests.test_update_delivery import _message

pytest_plugins = ["tests.test_telegram_update_fairness_delivery"]


async def test_native_update_is_persisted_through_delivery(delivery: _Delivery) -> None:
    gate = delivery.wire[0]

    def seed(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute(
                "UPDATE dialog_directory_publication SET account_id=42,generation=1,observation_started_at=1 WHERE singleton=1"
            )
            SQLiteFolderSnapshotRepository(conn).project_observation(
                FolderRuleObservation((FolderRule(4, "Before"),), "before", 1), completed_at=1
            )

    await delivery.writer.run(seed)
    native = types.DialogFilter(
        4, types.TextWithEntities("After", [types.MessageEntityBold(0, 5)]), [], [], [], color=4
    )
    update = types.UpdateDialogFilter(4, native)
    processed = await gate._preprocess_updates([update], [], [])
    await gate._dispatch_update(processed[0])
    (rule,) = read_folder_rules(delivery.reader)
    assert rule.title == "After"
    assert rule.title_entities == ({"_": "MessageEntityBold", "offset": 0, "length": 5},)
    assert rule.color == 4
    assert folder_snapshot(delivery.reader, stale_after_seconds=900)["complete"]


async def test_auth_switch_before_dispatch_keeps_old_facts(
    delivery: _Delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = delivery.wire[0]

    def seed(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute(
                "UPDATE dialog_directory_publication SET account_id=42,generation=1,observation_started_at=1 WHERE singleton=1"
            )
            SQLiteFolderSnapshotRepository(conn).project_observation(
                FolderRuleObservation((FolderRule(4, "Before"),), "before", 1), completed_at=1
            )

    await delivery.writer.run(seed)
    native = types.DialogFilter(4, types.TextWithEntities("After", []), [], [], [])
    processed = await gate._preprocess_updates([types.UpdateDialogFilter(4, native)], [], [])
    monkeypatch.setattr(gate, "retained_auth_scope", lambda: None)
    await gate._dispatch_update(processed[0])
    assert read_folder_rules(delivery.reader)[0].title == "Before"
    assert gate._delivery_failure.is_set()


@pytest.mark.parametrize("stale_scope", [False, True])
async def test_unattributed_folder_delivery_requests_refresh_and_completes(
    delivery: _Delivery, stale_scope: bool
) -> None:
    from mcp_telegram.auth_scope import TelegramAuthScope

    gate = delivery.wire[0]
    native = types.DialogFilter(4, types.TextWithEntities("Unbound", []), [], [], [])
    envelope = DeliveryEnvelope.capture(
        gate._capture_delivery_snapshot([]),
        time.time(),
        [types.UpdateDialogFilter(4, native)],
        [],
        [],
        auth_scope=TelegramAuthScope(1, 42, 2, 123) if stale_scope else None,
    )
    repository = gate._delivery_repository
    assert repository is not None
    await repository.admit(envelope.model_dump_json().encode(), 1)
    await gate.start_update_delivery(42)
    assert delivery.reader.execute("SELECT pending FROM update_delivery WHERE singleton=1").fetchone()[0] is None
    assert not folder_snapshot(delivery.reader, stale_after_seconds=900)["complete"]


async def test_unattributed_profile_delivery_preserves_note_and_requests_refresh(delivery: _Delivery) -> None:
    gate = delivery.wire[0]
    note = {"constructor": "TextWithEntities", "text": "retained", "entities": []}
    coverage = {"full_user.note": {"state": "present", "authoritative": True}}

    def seed(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute("UPDATE dialogs SET archived=1 WHERE dialog_id=55")
            conn.execute("UPDATE full_history_enrollment SET enabled=0 WHERE dialog_id=55")
            conn.execute(
                "INSERT INTO entity_details(entity_id,detail_json,fetched_at,profile_owner_account_id) VALUES (55,?,1,42)",
                (json.dumps({"schema": 1, "id": 55, "type": "user", "name": "Retained"}),),
            )
            conn.execute(
                "INSERT INTO entity_profile_semantic_snapshots(owner_account_id,entity_id,normalization_version,fields_json,coverage_json,source) VALUES (42,55,'entity-profile-full-user-v2',?,?,'rpc')",
                (json.dumps({"full_user.note": note}), json.dumps(coverage)),
            )

    await delivery.writer.run(seed)
    retained = read_note(delivery.reader, 42, 55)
    assert retained["state"] == "retained"
    envelope = DeliveryEnvelope.capture(
        gate._capture_delivery_snapshot([]),
        time.time(),
        [types.UpdateUser(55)],
        [types.User(55, first_name="Old", contact=True)],
        [],
    )
    repository = gate._delivery_repository
    assert repository is not None
    await repository.admit(envelope.model_dump_json().encode(), 1)
    await gate.start_update_delivery(42)
    assert read_note(delivery.reader, 42, 55) == retained
    assert (
        delivery.reader.execute("SELECT status FROM entity_profile_refresh_state WHERE entity_id=55").fetchone()[0]
        == "pending"
    )
    assert {
        row[0]
        for row in cast(
            list[tuple[object, ...]],
            delivery.reader.execute(
                "SELECT section FROM entity_detail_sections WHERE entity_id=55 AND status='pending'"
            ).fetchall(),
        )
    } >= {
        "full_profile",
        "personal_channel",
        "avatar_history",
        "common_chats",
        "saved_music",
        "saved_gifts",
        "current_stories",
        "pinned_stories",
    }
    assert delivery.reader.execute("SELECT enabled FROM full_history_enrollment WHERE dialog_id=55").fetchone()[0] == 0
    assert delivery.reader.execute("SELECT pending FROM update_delivery WHERE singleton=1").fetchone()[0] is None


async def test_other_account_delivery_remains_pending(delivery: _Delivery) -> None:
    from mcp_telegram.auth_scope import TelegramAuthScope

    gate = delivery.wire[0]
    envelope = DeliveryEnvelope.capture(
        gate._capture_delivery_snapshot([]),
        time.time(),
        [types.UpdateDialogFilters()],
        [],
        [],
        auth_scope=TelegramAuthScope(1, 43, 2, 123),
    )
    repository = gate._delivery_repository
    assert repository is not None
    await repository.admit(envelope.model_dump_json().encode(), 1)
    with pytest.raises(RuntimeError, match="account mismatch"):
        await gate.start_update_delivery(42)
    assert delivery.reader.execute("SELECT pending FROM update_delivery WHERE singleton=1").fetchone()[0] is not None


async def test_unattributed_bundled_user_preserves_message_and_schedules_profile(delivery: _Delivery) -> None:
    gate = delivery.wire[0]

    def seed(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute("UPDATE dialogs SET archived=1 WHERE dialog_id=55")
            conn.execute(
                "INSERT INTO entity_details(entity_id,detail_json,fetched_at,profile_owner_account_id) VALUES (55,?,1,42)",
                (json.dumps({"schema": 1, "id": 55, "type": "user", "name": "Retained"}),),
            )

    await delivery.writer.run(seed)
    message = _message(11)
    envelope = DeliveryEnvelope.capture(
        gate._capture_delivery_snapshot([]),
        time.time(),
        [types.UpdateNewMessage(message, 11, 1)],
        [types.User(55, first_name="Old", contact=True)],
        [],
    )
    repository = gate._delivery_repository
    assert repository is not None
    await repository.admit(envelope.model_dump_json().encode(), 1)
    await gate.start_update_delivery(42)
    assert delivery.reader.execute("SELECT 1 FROM messages WHERE dialog_id=55 AND message_id=11").fetchone()
    assert (
        json.loads(
            cast(
                str, delivery.reader.execute("SELECT detail_json FROM entity_details WHERE entity_id=55").fetchone()[0]
            )
        )["name"]
        == "Retained"
    )
    assert (
        delivery.reader.execute("SELECT status FROM entity_profile_refresh_state WHERE entity_id=55").fetchone()[0]
        == "pending"
    )
    assert delivery.reader.execute("SELECT pending FROM update_delivery WHERE singleton=1").fetchone()[0] is None


async def test_gap_storage_failure_retains_delivery_and_safe_checkpoint(
    delivery: _Delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_telegram.entity_profile.repository import EntityProfileRepository

    gate = delivery.wire[0]

    def seed(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute(
                "INSERT INTO entity_details(entity_id,detail_json,fetched_at,profile_owner_account_id) VALUES (55,?,1,42)",
                (json.dumps({"schema": 1, "id": 55, "type": "user", "name": "Retained"}),),
            )

    await delivery.writer.run(seed)
    envelope = DeliveryEnvelope.capture(
        gate._capture_delivery_snapshot([]), time.time(), [types.UpdateUser(55)], [], []
    )
    repository = gate._delivery_repository
    assert repository is not None
    await repository.admit(envelope.model_dump_json().encode(), 1)
    before = cast(
        tuple[object, ...], delivery.reader.execute("SELECT safe_snapshot,pending FROM update_delivery").fetchone()
    )
    tokens = cast(list[tuple[object, ...]], delivery.reader.execute("SELECT * FROM update_delivery_tokens").fetchall())
    with monkeypatch.context() as blocked:

        def fail(*_args: object, **_kwargs: object) -> bool:
            raise RuntimeError("gap storage failure")

        blocked.setattr(EntityProfileRepository, "request_section_refresh", fail)
        with pytest.raises(RuntimeError, match="replay failed"):
            await gate.start_update_delivery(42)
    assert (
        cast(
            tuple[object, ...], delivery.reader.execute("SELECT safe_snapshot,pending FROM update_delivery").fetchone()
        )
        == before
    )
    assert (
        cast(list[tuple[object, ...]], delivery.reader.execute("SELECT * FROM update_delivery_tokens").fetchall())
        == tokens
    )
    assert delivery.reader.execute("SELECT 1 FROM entity_profile_refresh_state WHERE entity_id=55").fetchone() is None
    gate._delivery_failure.clear()
    gate._active_delivery = None
    await gate.start_update_delivery(42)
    assert delivery.reader.execute("SELECT pending FROM update_delivery").fetchone()[0] is None
    assert (
        delivery.reader.execute("SELECT status FROM entity_profile_refresh_state WHERE entity_id=55").fetchone()[0]
        == "pending"
    )
