"""History acquisition must retain the identity already carried by Telegram."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from telethon.tl import types

from mcp_telegram.delta_sync import DeltaSyncWorker
from mcp_telegram.dialog_identity import (
    capture_identity_baseline,
    publish_dialog_identity,
    read_dialog_identities,
)
from mcp_telegram.dialog_identity_contracts import DialogIdentityObservation
from mcp_telegram.history_enrollment import enable_history
from mcp_telegram.inbox_projection import _structured_inbox_group
from mcp_telegram.message_history.contracts import ForwardGapPage, FullHistoryPage
from mcp_telegram.message_history.ports import ForwardGapPagePort, FullHistoryPagePort
from mcp_telegram.message_history.telegram_adapter import (
    TelethonForwardGapPageAdapter,
    TelethonFullHistoryPageAdapter,
)
from mcp_telegram.models import DialogType
from mcp_telegram.reading import ReadingService
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.sync_worker import FullSyncWorker
from sqlite_writer_helpers import inline_writer

PEER = -1001234567890


def _channel() -> types.Channel:
    return types.Channel(
        id=1234567890,
        title="Canonical channel",
        photo=types.ChatPhotoEmpty(),
        date=datetime.now(UTC),
        broadcast=True,
        username="canonical_channel",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("forward", [False, True])
async def test_history_adapters_retain_only_target_identity_without_extra_rpc(forward: bool) -> None:
    class Client:
        requests = 0

        async def get_input_entity(self, peer: object) -> object:
            return types.InputPeerChannel(channel_id=1234567890, access_hash=1)

        async def __call__(self, request: object) -> object:
            self.requests += 1
            return types.messages.Messages(messages=[], topics=[], chats=[_channel()], users=[types.User(id=1)])

    client = Client()
    page = (
        await TelethonForwardGapPageAdapter(client).fetch_page(PEER, after_message_id=0, should_stop=lambda: False)
        if forward
        else await TelethonFullHistoryPageAdapter(client).fetch_page(PEER, before_message_id=0)
    )
    assert client.requests == 1
    assert page.identity is not None
    assert page.identity.dialog_id == PEER
    assert page.identity.name == "Canonical channel"
    assert page.identity.dialog_type is DialogType.CHANNEL


@pytest.mark.asyncio
@pytest.mark.parametrize("forward", [False, True])
@pytest.mark.parametrize("concurrent_refresh", [False, True])
async def test_history_workers_publish_orphan_identity_without_overwriting_refresh(
    make_synced_db: Callable[[], sqlite3.Connection], forward: bool, concurrent_refresh: bool
) -> None:
    conn = make_synced_db()
    enable_history(conn, PEER)
    conn.execute("UPDATE synced_dialogs SET status='synced',read_inbox_max_id=0 WHERE dialog_id=?", (PEER,))
    conn.execute(
        "INSERT INTO messages(dialog_id,message_id,sent_at,text,out,is_service,is_deleted) VALUES (?,1,10,'',0,0,0)",
        (PEER,),
    )
    conn.commit()
    service = cast(
        ReadingService, SimpleNamespace(_conn=conn, _deps=SimpleNamespace(deleted_message_visibility_seconds=86400))
    )
    entries, counts = ReadingService._collect_unread_dialogs(service, 100)
    assert counts == {PEER: 1}
    assert entries[0]["dialog_type"] == "unknown"
    assert entries[0]["display_name_source"] == "numeric"
    assert entries[0]["name_unavailable_reason"] == "identity_not_observed"
    dialog, _, _ = _structured_inbox_group({**entries[0], "dialog_id": PEER})
    assert dialog["entity"] == {"display_name": str(PEER), "telegram_id": PEER}
    assert dialog["name_unavailable_reason"] == "identity_not_observed"
    assert dialog["category"] == "unknown"

    class Port:
        async def fetch_page(self, dialog_id: int, **kwargs: object) -> FullHistoryPage | ForwardGapPage:
            assert capture_identity_baseline(conn, dialog_id) == 0
            if concurrent_refresh:
                with write_transaction(conn):
                    assert publish_dialog_identity(
                        conn,
                        dialog_id,
                        DialogIdentityObservation(
                            dialog_id, "Newer", "newer", "supergroup", True, "directory", int(time.time())
                        ),
                        0,
                    )
            identity = DialogIdentityObservation(
                dialog_id, "Canonical channel", "canonical_channel", "channel", True, "profile", int(time.time())
            )
            return ForwardGapPage((), True, identity) if forward else FullHistoryPage((), 1, None, identity)

    if forward:
        worker = DeltaSyncWorker(cast(ForwardGapPagePort, Port()), conn, asyncio.Event(), writer=inline_writer(conn))
        await worker.fetch_delta_slice_for_dialog(PEER)
    else:
        worker = FullSyncWorker(cast(FullHistoryPagePort, Port()), conn, asyncio.Event(), writer=inline_writer(conn))
        await worker._fetch_batch(PEER, 0)
    identity = read_dialog_identities(conn, [PEER])[PEER]
    assert identity.name == ("Newer" if concurrent_refresh else "Canonical channel")
    assert identity.dialog_type is (DialogType.SUPERGROUP if concurrent_refresh else DialogType.CHANNEL)
    assert identity.name_unavailable_reason is None
    entries, _ = ReadingService._collect_unread_dialogs(service, 100)
    assert bool(entries) is concurrent_refresh
