"""Replay retains the complete durable Telegram envelope in the update ring."""

import json
from pathlib import Path
from typing import cast

from telethon import events, types
from telethon.sessions import SQLiteSession

from mcp_telegram.message_delivery_observations import MessageDeliveryObservations
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.telegram_delivery import load_delivery_envelope
from tests.test_message_delivery_observations import Sink
from tests.test_update_delivery import _client, _database, _mixed, _pending


async def test_pending_delivery_replay_records_envelope_sidecars_and_sampled_traces(  # noqa: PLR0914 - complete replay lifecycle
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    client = _client(tmp_path / "session", writer)
    reopened = None
    seen: list[object] = []
    try:
        await client.start_update_delivery(99)
        user = types.User(55, first_name="Replay user")
        channel = types.ChannelForbidden(123, 456, "Replay channel")
        await _mixed(client, users=[user], chats=[channel])
        pending = _pending(path)
        assert pending is not None
        envelope = load_delivery_envelope(pending)
        updates, users, chats = envelope.unpack()
        _expected = types.Updates(
            cast(list[types.TypeUpdate], updates),
            cast(list[types.TypeUser], users),
            cast(list[types.TypeChat], chats),
            envelope.checkpoint_after.date,
            envelope.checkpoint_after.seq,
        )
        cast(SQLiteSession, client.session).close()

        reopened = _client(tmp_path / "session", writer)
        reopened.set_message_delivery_observer(MessageDeliveryObservations(Sink()))

        async def received(update: object) -> None:
            seen.append(update)

        reopened.add_event_handler(received, events.Raw())
        await reopened.start_update_delivery(99)
        entries = reopened.update_ring().peek_batch(max_count=32, max_bytes=4 * 1024 * 1024)
        assert len(entries) == 1
        entry = entries[0]
        assert entry.source == "replay" and entry.constructor == "Updates"
        restored = cast(dict[str, object], json.loads(entry.payload_json))
        assert [item["id"] for item in cast(list[dict[str, object]], restored["users"])] == [55]
        assert [item["id"] for item in cast(list[dict[str, object]], restored["chats"])] == [123]
        assert len(seen) == len(updates)
        assert len(cast(list[str], json.loads(entry.trace_ids_json))) == 2
        assert _pending(path) is None
    finally:
        if reopened is not None:
            cast(SQLiteSession, reopened.session).close()
        else:
            cast(SQLiteSession, client.session).close()
        await writer.aclose()
