from __future__ import annotations

import asyncio
import logging
import sqlite3

import pytest
from telethon.errors import RPCError  # type: ignore[import-untyped]
from telethon.errors.rpcbaseerrors import BadRequestError  # type: ignore[import-untyped]
from telethon.errors.rpcerrorlist import MsgIdInvalidError  # type: ignore[import-untyped]

import mcp_telegram.telegram_rpc_error as rpc_error
from mcp_telegram.fact_hydration import HydrationDropObservation, MessageFactHydrationWorker
from mcp_telegram.hydration_queue import HydrationJob, HydrationPriority
from mcp_telegram.telegram_rpc_error import describe_telegram_rpc_error


def test_bad_request_uses_own_safe_message_and_mro_code() -> None:
    descriptor = describe_telegram_rpc_error(BadRequestError(None, "MSG_VOICE_TOO_LONG"))

    assert descriptor.error_type == "BadRequestError"
    assert descriptor.code == 400
    assert descriptor.symbol == "MSG_VOICE_TOO_LONG"


def test_generated_msg_id_invalid_reverse_maps_to_correct_symbol() -> None:
    descriptor = describe_telegram_rpc_error(MsgIdInvalidError(None))

    assert descriptor.error_type == "MsgIdInvalidError"
    assert descriptor.code == 400
    assert descriptor.symbol == "MSG_ID_INVALID"


def test_unsafe_message_does_not_leak_into_descriptor() -> None:
    secret = "token=do-not-log"
    descriptor = describe_telegram_rpc_error(RPCError(None, f"BAD\n{secret}"))

    assert descriptor.symbol is None
    assert secret not in repr(descriptor)


def test_descriptor_never_inspects_request_args_or_repr() -> None:
    class ExplosiveError(Exception):
        def __repr__(self) -> str:
            raise AssertionError("repr inspected")

        def __str__(self) -> str:
            raise AssertionError("str inspected")

    error = ExplosiveError("private args")
    error.request = object()  # type: ignore[attr-defined]

    descriptor = describe_telegram_rpc_error(error)

    assert descriptor.error_type == "ExplosiveError"
    assert descriptor.symbol is None


def test_code_rejects_bool_and_out_of_range_values() -> None:
    class ParentError(Exception):
        code = 500

    class ChildError(ParentError):
        code = True

    class OutOfRangeError(Exception):
        code = 600

    assert describe_telegram_rpc_error(ChildError()).code == 500
    assert describe_telegram_rpc_error(OutOfRangeError()).code is None


def test_reverse_lookup_requires_one_unique_safe_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    class DuplicateError(Exception):
        pass

    monkeypatch.setattr(rpc_error, "rpc_errors_dict", {"ONE": DuplicateError, "TWO": DuplicateError})

    assert describe_telegram_rpc_error(DuplicateError()).symbol is None


def test_hydration_drop_log_aggregates_bounded_coordinates(caplog: pytest.LogCaptureFixture) -> None:
    conn = sqlite3.connect(":memory:")
    try:
        worker = MessageFactHydrationWorker(
            object(),
            conn,
            asyncio.Event(),
            handlers=(),
            interval_seconds=1,
            max_requests_per_cycle=1,
            max_jobs_per_cycle=3,
            retry_delay_seconds=1,
            circuit_retry_seconds=1,
            max_attempts=1,
            pause_between_requests_seconds=0,
        )
        jobs = [
            HydrationJob("media_metadata", 42, message_id, 1, 2, priority=HydrationPriority.BACKFILL)
            for message_id in (11, 12, 13)
        ]
        observations = tuple(
            HydrationDropObservation("attempt_limit", job.message_id, job.kind, job.dialog_id, job.attempts)
            for job in jobs
        )

        with caplog.at_level(logging.DEBUG, logger="mcp_telegram.fact_hydration"):
            worker._log_drops(jobs, observations)

        records = [record for record in caplog.records if record.message.startswith("message_fact_hydration_drop")]
        assert len(records) == 1
        assert "job_count=3" in records[0].message
        assert "message_ids=(11, 12, 13)" in records[0].message
        assert records[0].levelno == logging.WARNING
        assert records[0].exc_info is None
    finally:
        conn.close()
