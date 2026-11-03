import sqlite3
from dataclasses import replace

import pytest

from mcp_telegram.folders.command_receipts import (
    FOLDER_COMMAND_SCHEMA,
    bind_attempt,
    record_wire_outcome,
    validate_binding,
)
from mcp_telegram.native_write_fence import NativeWriteReceiptBinding


def test_exact_attempt_terminal_binding_and_late_result() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute(FOLDER_COMMAND_SCHEMA)
    conn.execute("CREATE TABLE daemon_state (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO folder_command_receipts(account_id,operation_key,dc_id,key_id,action,target,semantic_fingerprint,binding_revision,outcome,created_at,updated_at) VALUES (1,'op',2,3,'update',4,'hash',5,'pending',1,1)"
    )
    binding = NativeWriteReceiptBinding("folder", 1, 2, 3, "op", "update", 4, "hash", 5)
    bind_attempt(conn, binding, "attempt")
    bind_attempt(conn, binding, "attempt")
    assert not validate_binding(conn, binding, "attempt")
    with pytest.raises(ValueError):
        bind_attempt(conn, binding, "another")
    record_wire_outcome(conn, binding, "attempt", "unknown")
    assert validate_binding(conn, binding, "attempt")
    assert not validate_binding(conn, replace(binding, binding_revision=6), "attempt")
    record_wire_outcome(conn, binding, "attempt", "acknowledged")
    assert conn.execute(
        "SELECT outcome, binding_revision, terminal_revision FROM folder_command_receipts"
    ).fetchone() == ("unknown", 5, 1)
    with pytest.raises(ValueError):
        record_wire_outcome(conn, binding, "different", "unknown")
    with pytest.raises(ValueError):
        record_wire_outcome(conn, binding, "attempt", "invalid")
    with pytest.raises(ValueError):
        bind_attempt(conn, replace(binding, domain="contact_note"), "attempt")
    with pytest.raises(ValueError):
        bind_attempt(conn, binding, "")
    with pytest.raises(ValueError):
        validate_binding(object(), binding, "attempt")
    conn.close()


def test_receipt_preserves_unsigned_auth_key_id() -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute(FOLDER_COMMAND_SCHEMA)
    conn.execute("CREATE TABLE daemon_state (key TEXT PRIMARY KEY, value TEXT)")
    unsigned_key = 2**64 - 1
    conn.execute(
        "INSERT INTO folder_command_receipts(account_id,operation_key,dc_id,key_id,action,target,semantic_fingerprint,binding_revision,outcome,created_at,updated_at) VALUES (1,'op',2,?,'update',4,'hash',5,'pending',1,1)",
        (str(unsigned_key),),
    )
    binding = NativeWriteReceiptBinding("folder", 1, 2, unsigned_key, "op", "update", 4, "hash", 5)
    bind_attempt(conn, binding, "attempt")
    record_wire_outcome(conn, binding, "attempt", "rejected")
    assert validate_binding(conn, binding, "attempt")
    conn.close()


@pytest.mark.asyncio
async def test_registered_owner_reconstructs_from_durable_intent_and_current_peer() -> None:
    from types import SimpleNamespace
    from typing import cast

    from telethon.tl.types import InputPeerUser

    from mcp_telegram.auth_scope import TelegramAuthScope
    from mcp_telegram.folders.commands import (
        FolderReceiptClient,
        FolderReceiptOwner,
        folder_intent_fingerprint,
        native_folder_request,
    )
    from mcp_telegram.folders.contracts import FolderRule
    from mcp_telegram.folders.sqlite_repository import encode_folder_definition
    from mcp_telegram.sqlite_writer import SQLiteWriter

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute(FOLDER_COMMAND_SCHEMA)
    conn.execute("CREATE TABLE daemon_state (key TEXT PRIMARY KEY, value TEXT)")
    rule = FolderRule(2, "A", included_ids=(42,))
    intended = encode_folder_definition(rule)
    before = encode_folder_definition(replace(rule, title="B"))
    fingerprint = folder_intent_fingerprint("update", 2, before, intended)
    binding = NativeWriteReceiptBinding("folder", 1, 2, 3, "op", "update", 2, fingerprint, 1)
    conn.execute(
        "INSERT INTO folder_command_receipts(account_id,operation_key,dc_id,key_id,action,target,semantic_fingerprint,binding_revision,outcome,before_image,intended_definition,created_at,updated_at) VALUES (1,'op',2,'3','update',2,?,1,'pending',?,?,1,1)",
        (fingerprint, before, intended),
    )

    class Writer:
        async def run(self, fn: object) -> object:
            return fn(conn)  # type: ignore[operator]

    scope = TelegramAuthScope(1, 1, 2, 3)
    client = SimpleNamespace(
        retained_auth_scope=lambda: scope, retained_draft_peer=lambda peer: InputPeerUser(peer, 99)
    )
    owner = FolderReceiptOwner(cast(SQLiteWriter, Writer()), cast(FolderReceiptClient, client))
    request = native_folder_request("update", 2, rule, lambda peer: InputPeerUser(peer, 99))
    assert await owner.verify_request(binding, request)
    client.retained_draft_peer = lambda peer: InputPeerUser(peer, 100)
    assert not await owner.verify_request(binding, request)
    await owner.bind_attempt(binding, "a")
    assert not await owner.validate_binding(binding, "a")
    await owner.record_wire_outcome(binding, "a", "unknown")
    assert await owner.validate_binding(binding, "a")
    conn.close()


def test_startup_recovery_requires_exact_binding_and_attempt() -> None:
    from mcp_telegram.folders.command_receipts import recover_binding

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute(FOLDER_COMMAND_SCHEMA)
    conn.execute("CREATE TABLE daemon_state (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO folder_command_receipts(account_id,operation_key,dc_id,key_id,action,target,semantic_fingerprint,binding_revision,outcome,created_at,updated_at) VALUES (1,'op',2,'3','update',4,'hash',5,'pending',1,1)"
    )
    binding = NativeWriteReceiptBinding("folder", 1, 2, 3, "op", "update", 4, "hash", 5)
    bind_attempt(conn, binding, "a")
    assert not recover_binding(conn, binding, "different")
    assert not recover_binding(conn, replace(binding, target=9), "a")
    assert recover_binding(conn, binding, "a")
    assert recover_binding(conn, binding, "a")
    assert conn.execute("SELECT outcome,terminal_revision FROM folder_command_receipts").fetchone() == ("unknown", 1)
    conn.close()


def test_observation_requires_later_order_and_selected_delta() -> None:
    from mcp_telegram.folders.command_receipts import refine_from_observation

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute(FOLDER_COMMAND_SCHEMA)
    conn.execute("CREATE TABLE daemon_state (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO folder_command_receipts(account_id,operation_key,dc_id,key_id,action,target,semantic_fingerprint,binding_revision,outcome,verification,intended_definition,dispatch_order,created_at,updated_at) VALUES (1,'op',2,'3','update',4,'hash',5,'unknown','exhausted','expected',10,1,1)"
    )
    refine_from_observation(conn, 1, 10, {4: "expected"}, complete=True)
    refine_from_observation(conn, 1, 11, {5: "other"}, complete=False, affected_folder_id=5)
    assert conn.execute("SELECT outcome,verification FROM folder_command_receipts").fetchone() == (
        "unknown",
        "exhausted",
    )
    refine_from_observation(conn, 1, 11, {4: "expected"}, complete=False, affected_folder_id=4)
    assert conn.execute("SELECT outcome,verification FROM folder_command_receipts").fetchone() == ("unknown", "matched")
    conn.close()


def test_verification_attempts_are_bounded_and_not_reset_by_restart() -> None:
    from mcp_telegram.auth_scope import TelegramAuthScope
    from mcp_telegram.folders.command_receipts import recover_interrupted_operations
    from mcp_telegram.folders.commands import _reserve_folder_verification

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute(FOLDER_COMMAND_SCHEMA)
    conn.execute("CREATE TABLE daemon_state (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO folder_command_receipts(account_id,operation_key,dc_id,key_id,action,target,semantic_fingerprint,binding_revision,outcome,verification,verification_deadline,next_verification_at,dispatch_order,created_at,updated_at) VALUES (1,'op',2,'3','update',4,'hash',1,'unknown','pending',1000,100,1,1,1)"
    )
    assert len(_reserve_folder_verification(conn, 1, None, 100, force=False)) == 1
    assert not _reserve_folder_verification(conn, 1, None, 104, force=False)
    assert len(_reserve_folder_verification(conn, 1, None, 105, force=False)) == 1
    assert len(_reserve_folder_verification(conn, 1, None, 135, force=False)) == 1
    assert not _reserve_folder_verification(conn, 1, None, 165, force=False)
    assert conn.execute("SELECT verification,verification_attempts FROM folder_command_receipts").fetchone() == (
        "exhausted",
        3,
    )
    assert recover_interrupted_operations(conn, TelegramAuthScope(1, 1, 2, 3), 200) == 0
    assert conn.execute("SELECT verification,verification_attempts FROM folder_command_receipts").fetchone() == (
        "exhausted",
        3,
    )
    conn.close()


def test_recovery_only_changes_verified_matching_auth_scope() -> None:
    from mcp_telegram.auth_scope import TelegramAuthScope
    from mcp_telegram.folders.command_receipts import recover_interrupted_operations

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute(FOLDER_COMMAND_SCHEMA)
    conn.execute("CREATE TABLE daemon_state (key TEXT PRIMARY KEY, value TEXT)")
    for account, key in ((1, "3"), (1, "4"), (2, "3")):
        conn.execute(
            "INSERT INTO folder_command_receipts(account_id,operation_key,dc_id,key_id,action,target,semantic_fingerprint,binding_revision,outcome,verification_deadline,created_at,updated_at) VALUES (?, ?, 2, ?, 'update',4,'hash',1,'pending',500,1,1)",
            (account, f"op-{account}-{key}", key),
        )
    assert recover_interrupted_operations(conn, TelegramAuthScope(1, 1, 2, 3), 200) == 1
    assert conn.execute(
        "SELECT account_id,key_id,outcome,verification_deadline FROM folder_command_receipts ORDER BY account_id,key_id"
    ).fetchall() == [(1, "3", "unknown", 500), (1, "4", "pending", 500), (2, "3", "pending", 500)]
    conn.close()
