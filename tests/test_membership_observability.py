"""Membership diagnostics are bounded, aggregated and best effort."""

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest

from helpers import build_mock_message
from mcp_telegram import daemon, event_handlers
from mcp_telegram.config import RuntimeObservationConfig
from mcp_telegram.entity_profile.membership import MembershipSignalCounts
from mcp_telegram.rpc_admission_observations import RpcAdmissionObservationAggregator
from mcp_telegram.runtime_observations import MAX_PAYLOAD_BYTES, encode_payload
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from tests.test_event_handlers import _RecordingDemandSink, insert_synced_dialog, make_manager, make_new_message_event


class Recorder:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.reject = False

    def record(self, **values: object) -> bool:
        if self.reject:
            return False
        self.rows.append(values)
        return True


def test_membership_summary_is_bounded_and_retries_rejected_flush() -> None:
    recorder = Recorder()
    observer = RpcAdmissionObservationAggregator(recorder, policy=RuntimeObservationConfig(), clock=lambda: 0.0)
    for _ in range(100):
        observer.observe_membership({"received": 1, "queued": 1}, source="message_membership")
    observer.observe_membership({"ignored": 2, "ignored_ineligible": 2}, source="message_membership")
    observer.observe_membership({"private-id": 123}, source="message_membership")
    observer.observe_membership({"received": 1}, source="private-source")
    observer.observe_membership({"received": 0}, source="retained_repair")
    observer.observe_membership({"received": -1}, source="message_membership")
    observer.observe_membership({"received": 1, "queued": 1}, source="access_loss")
    recorder.reject = True
    observer.flush()
    assert recorder.rows == []
    recorder.reject = False
    observer.flush()
    assert len(recorder.rows) == 2
    payload = recorder.rows[0]["payload"]
    assert payload == {
        "received": 100,
        "queued": 100,
        "ignored": 2,
        "ignored_ineligible": 2,
        "source": "message_membership",
        "reason": "unspecified",
    }
    assert len(encode_payload(cast(Mapping[str, object], payload)).encode()) <= MAX_PAYLOAD_BYTES
    assert "private" not in json.dumps(payload)
    assert recorder.rows[1]["payload"] == {"received": 1, "queued": 1, "source": "access_loss", "reason": "unspecified"}


def test_membership_observer_survives_recorder_errors_and_caps_counts() -> None:
    class FailingRecorder(Recorder):
        def record(self, **values: object) -> bool:
            raise OSError("private error")

    observer = RpcAdmissionObservationAggregator(
        FailingRecorder(), policy=RuntimeObservationConfig(), clock=lambda: 0.0
    )
    observer.observe_membership({"received": 10**20}, source="retained_repair")
    observer.flush()
    assert observer._membership_counts == {("retained_repair", "unspecified"): {"received": 2147483647}}


def test_composition_binds_distinct_membership_and_access_loss_sources() -> None:
    recorder = Recorder()
    observer = RpcAdmissionObservationAggregator(recorder, policy=RuntimeObservationConfig(), clock=lambda: 0.0)

    class Handler:
        membership: Callable[[MembershipSignalCounts], None]
        access: Callable[[MembershipSignalCounts], None]

        def bind_membership_observer(self, callback: Callable[[MembershipSignalCounts], None]) -> None:
            self.membership = callback

        def bind_access_loss_observer(self, callback: Callable[[MembershipSignalCounts], None]) -> None:
            self.access = callback

    handler = Handler()
    ctx = cast(
        daemon._SyncMainContext,
        SimpleNamespace(handler_manager=handler, rpc_observation_sink=None, rpc_admission_observer=observer),
    )
    daemon._bind_event_observers(ctx)
    counts = MembershipSignalCounts(received=1, queued=1)
    handler.membership(counts)
    handler.access(counts)
    observer.flush()
    assert {cast(dict[str, object], row["payload"])["source"] for row in recorder.rows} == {
        "message_membership",
        "access_loss",
    }


@pytest.mark.asyncio
async def test_realtime_observer_runs_after_commit_and_cannot_block_wakeup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    insert_synced_dialog(conn, 1001)
    manager = make_manager(MagicMock(), conn, asyncio.Event())
    manager.register()
    sink = _RecordingDemandSink()
    manager.bind_demand_sink(sink)
    original = event_handlers.insert_messages_with_fts

    def insert(*args: object, **kwargs: object) -> object:
        result = original(*args, **kwargs)  # type: ignore[arg-type]
        return replace(result, membership_counts=MembershipSignalCounts(received=1, queued=1))

    def observe(counts: MembershipSignalCounts) -> None:
        assert counts.queued == 1
        assert conn.execute("SELECT count(*) FROM messages WHERE dialog_id=1001 AND message_id=500").fetchone()[0] == 1
        raise RuntimeError("observer failure")

    monkeypatch.setattr(event_handlers, "insert_messages_with_fts", insert)
    manager.bind_membership_observer(observe)
    await manager.on_new_message(make_new_message_event(1001, build_mock_message(id=500, text="hello")))
    assert sink.offered
    conn.close()
