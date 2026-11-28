"""A deletion scan also publishes the surviving message it already received."""

import asyncio
import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from telethon.tl import functions, types

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.delta_sync import DmDeletionReconciliationDemandAdapter
from mcp_telegram.image_authorization import ScopeCell, ScopeToken
from mcp_telegram.message_deletion_evidence import DmScanReceipt
from mcp_telegram.messages.sqlite_bundle import insert_messages_with_fts
from mcp_telegram.messages.telegram_adapter import extract_message_row
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_demand import RpcAttemptBudget
from mcp_telegram.telegram_demand_progress import SliceOutcomeKind
from tests.sqlite_writer_helpers import event_manager, inline_writer


@pytest.mark.parametrize("case", ["normal", "revoke_before", "revoke", "revoke_after", "revoke_commit", "syncing"])
async def test_gap_scan_retains_edited_survivor_without_extra_request(tmp_path: Path, case: str) -> None:  # noqa: PLR0915
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    client = MagicMock()
    cell = ScopeCell()
    assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
    client.capture_image_scope, client.claim_image_scope = cell.capture, cell.claim
    manager = event_manager(client, conn, asyncio.Event())
    manager.set_self_id(42)
    old = types.Message(
        id=7,
        peer_id=types.PeerUser(55),
        from_id=types.PeerUser(55),
        date=datetime.fromtimestamp(10, UTC),
        message="old body",
    )
    edited = types.Message(
        id=7,
        peer_id=types.PeerUser(55),
        from_id=types.PeerUser(55),
        date=datetime.fromtimestamp(10, UTC),
        edit_date=datetime.fromtimestamp(20, UTC),
        message="updated survivor",
    )
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (55,'synced')")
        conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES (55,1,0)")
        conn.execute("INSERT INTO dialogs(dialog_id,type) VALUES (55,'user')")
        conn.execute("INSERT INTO entities(id,type,updated_at) VALUES (55,'user',0)")
        insert_messages_with_fts(conn, [extract_message_row(55, old)])

    async def get_messages(request: object) -> object:
        assert isinstance(request, functions.messages.GetMessagesRequest)
        assert len(request.id) == 1 and isinstance(request.id[0], types.InputMessageID)
        assert request.id[0].id == 7
        if case == "revoke":
            change = cell.begin_change()
            cell.end_change(change)
            assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 43, 2, 99))
            assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
        if case == "syncing":
            with write_transaction(conn):
                conn.execute("UPDATE synced_dialogs SET status='syncing' WHERE dialog_id=55")
        return types.messages.Messages(messages=[edited], topics=[], chats=[], users=[])

    client.side_effect = get_messages
    if case == "revoke_before":

        def revoke_before_claim(token: ScopeToken) -> bool:
            change = cell.begin_change()
            cell.end_change(change)
            assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
            client.claim_image_scope = cell.claim
            return cell.claim(token)

        client.claim_image_scope = revoke_before_claim
    if case in {"revoke_before", "revoke", "revoke_after", "revoke_commit"}:

        class Scanner:
            revoked = False

            async def run_dm_gap_scan_page(self, dialog_id: int, message_ids: Sequence[int]) -> DmScanReceipt:
                receipt = await manager.run_dm_gap_scan_page(dialog_id, message_ids)
                if not self.revoked:
                    self.revoked = True
                    if case == "revoke_commit":
                        calls = 0

                        def claim(token: ScopeToken) -> bool:
                            nonlocal calls
                            calls += 1
                            if calls == 2:
                                change = cell.begin_change()
                                cell.end_change(change)
                                assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
                            return cell.claim(token)

                        return DmScanReceipt(receipt.marked, receipt.scope, claim)
                    change = cell.begin_change()
                    cell.end_change(change)
                    assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
                return receipt

        scanner = manager if case in {"revoke_before", "revoke"} else Scanner()
        adapter = DmDeletionReconciliationDemandAdapter(conn, scanner, writer=inline_writer(conn), account_id=42)
        status = adapter.status(0)
        assert status is not None
        outcome = await adapter.run_slice(RpcAttemptBudget(limit=1, selected=status.selected))
        assert outcome.kind is SliceOutcomeKind.RETRYABLE_FAILURE
        row = cast(
            tuple[str], conn.execute("SELECT value FROM daemon_state WHERE key='delta_dm_gap_scan_state'").fetchone()
        )
        state = cast(dict[str, object], json.loads(row[0]))
        assert state["message_cursor"] == 0
        assert state["status"] == "running"
        text = "old body" if case in {"revoke_before", "revoke"} else "updated survivor"
        assert conn.execute("SELECT text FROM messages WHERE dialog_id=55 AND message_id=7").fetchone() == (text,)
        assert adapter.status(0) is not None
        if case == "revoke_before":
            client.assert_not_called()
        if case in {"revoke_before", "revoke_after", "revoke_commit"}:
            status = adapter.status(0)
            assert status is not None
            assert (
                await adapter.run_slice(RpcAttemptBudget(limit=1, selected=status.selected))
            ).kind is SliceOutcomeKind.PROGRESSED
    else:
        assert (await manager.run_dm_gap_scan_page(55, [7])).marked == 0
        assert conn.execute("SELECT text FROM messages WHERE dialog_id=55 AND message_id=7").fetchone() == (
            "updated survivor",
        )
        assert conn.execute("SELECT old_text FROM message_versions WHERE dialog_id=55 AND message_id=7").fetchone() == (
            "old body",
        )
        assert conn.execute(
            "SELECT message_id FROM messages_fts WHERE messages_fts MATCH 'updated' AND dialog_id=55"
        ).fetchone() == (7,)
    conn.close()
