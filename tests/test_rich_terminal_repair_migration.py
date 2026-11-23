import time
from pathlib import Path
from typing import cast

import pytest

import mcp_telegram.sync_db as schema
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema


def test_v105_reopens_only_current_unclassified_recent_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sync.db"
    with monkeypatch.context() as old:
        old.setattr(schema, "_CURRENT_SCHEMA_VERSION", 104)
        ensure_sync_schema(path)
    conn = _open_sync_db(path)
    for dialog, status, enabled in [
        (1, "synced", 1),
        (2, "syncing", 1),
        (3, "synced", 0),
        (4, "access_lost", 1),
        (-1000000000001, "synced", 1),
    ]:
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (?,?)", (dialog, status))
        conn.execute(
            "INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES (?,?,1)", (dialog, enabled)
        )
    for dialog, message in [(1, n) for n in range(1, 13)] + [(2, 1), (3, 1), (4, 1), (-1000000000001, 10)]:
        conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at) VALUES (?,?,1777593600)", (dialog, message))
        conn.execute(
            "INSERT INTO hydration_jobs(kind,dialog_id,message_id,due_at,attempts,priority,terminal,last_outcome) VALUES ('rich_composition',?,?,42,4,0,1,'terminal_error')",
            (dialog, message),
        )
    conn.execute("UPDATE messages SET sent_at=1, edit_date=1777593600 WHERE dialog_id=1 AND message_id=2")
    conn.execute("UPDATE messages SET sent_at=1 WHERE dialog_id=1 AND message_id=3")
    conn.execute("UPDATE messages SET rich_message='{}' WHERE dialog_id=1 AND message_id=4")
    conn.execute("UPDATE messages SET is_deleted=1 WHERE dialog_id=1 AND message_id=5")
    conn.execute("UPDATE hydration_jobs SET last_error_code='missing_response' WHERE dialog_id=1 AND message_id=6")
    conn.execute("UPDATE hydration_jobs SET last_outcome='exhausted' WHERE dialog_id=1 AND message_id=7")
    conn.execute("UPDATE hydration_jobs SET priority=1 WHERE dialog_id=1 AND message_id=8")
    conn.execute("UPDATE hydration_jobs SET kind='media_metadata' WHERE dialog_id=1 AND message_id=9")
    conn.execute("INSERT INTO nonchannel_message_deletions(message_id,deleted_at) VALUES (10,1)")
    conn.execute(
        "INSERT INTO message_observations(dialog_id,message_id,observation_order,is_deleted) VALUES (1,11,3,1)"
    )
    conn.execute("UPDATE hydration_jobs SET terminal=0 WHERE dialog_id=1 AND message_id=12")
    conn.execute("INSERT INTO daemon_state(key,value) VALUES ('rich_composition_hydration_cursor','unchanged')")
    conn.commit()
    conn.close()
    before = int(time.time())
    ensure_sync_schema(path)
    after = int(time.time())
    conn = _open_sync_db(path)
    rows = cast(
        list[tuple[int, int, int, int, int, str]],
        conn.execute(
            "SELECT dialog_id,message_id,due_at,attempts,priority,last_outcome FROM hydration_jobs WHERE kind='rich_composition' AND terminal=0 ORDER BY dialog_id,message_id"
        ).fetchall(),
    )
    assert [(r[0], r[1]) for r in rows] == [(-1000000000001, 10), (1, 1), (1, 2), (1, 12), (2, 1)]
    for row in rows:
        if row[1] != 12:
            assert before <= row[2] <= after
            assert row[3:] == (4, 0, "terminal_error")
    assert conn.execute("SELECT value FROM daemon_state WHERE key='rich_composition_hydration_cursor'").fetchone() == (
        "unchanged",
    )
    conn.execute(
        "UPDATE hydration_jobs SET terminal=1,last_error_code='missing_response' WHERE dialog_id=1 AND message_id=1"
    )
    conn.execute("UPDATE hydration_jobs SET terminal=1 WHERE dialog_id=1 AND message_id=2")
    conn.commit()
    conn.close()
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    assert conn.execute(
        "SELECT terminal,last_error_code FROM hydration_jobs WHERE dialog_id=1 AND message_id=1"
    ).fetchone() == (1, "missing_response")
    assert conn.execute(
        "SELECT terminal,last_error_code FROM hydration_jobs WHERE dialog_id=1 AND message_id=2"
    ).fetchone() == (1, None)
    conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["rich", "absence", "missing", "late_edit"])
async def test_repaired_job_settles_through_worker(  # noqa: PLR0915
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: str
) -> None:
    import asyncio
    from datetime import UTC, datetime
    from unittest.mock import patch

    from telethon.tl import types

    from mcp_telegram.auth_scope import TelegramAuthScope
    from mcp_telegram.fact_hydration import MessageFactHydrationWorker
    from mcp_telegram.hydration_queue import HydrationQueueRepository
    from mcp_telegram.image_authorization import ScopeCell, ScopeToken
    from mcp_telegram.media_hydration import RichCompositionHydrationHandler
    from mcp_telegram.observation_order import allocate_observation_order
    from mcp_telegram.sync_transactions import write_transaction
    from tests.sqlite_writer_helpers import inline_writer

    path = tmp_path / "sync.db"
    with monkeypatch.context() as old:
        old.setattr(schema, "_CURRENT_SCHEMA_VERSION", 104)
        ensure_sync_schema(path)
    conn = _open_sync_db(path)
    conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
    conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (1,'synced')")
    conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES (1,1,1)")
    conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at) VALUES (1,1,1777593600)")
    conn.execute(
        "INSERT INTO hydration_jobs(kind,dialog_id,message_id,due_at,attempts,priority,terminal,last_outcome) VALUES ('rich_composition',1,1,1,1,0,1,'terminal_error')"
    )
    conn.commit()
    conn.close()
    ensure_sync_schema(path)
    conn = _open_sync_db(path)

    class Client:
        def __init__(self) -> None:
            self.cell = ScopeCell()
            assert self.cell.publish(TelegramAuthScope(1, 42, 2, 99))

        def capture_image_scope(self) -> ScopeToken | None:
            return self.cell.capture()

        def claim_image_scope(self, token: ScopeToken) -> bool:
            return self.cell.claim(token)

    handler = RichCompositionHydrationHandler(batch_size=1)
    worker = MessageFactHydrationWorker(
        Client(),
        conn,
        asyncio.Event(),
        writer=inline_writer(conn),
        handlers=(handler,),
        interval_seconds=1,
        max_requests_per_cycle=3,
        max_jobs_per_cycle=1,
        retry_delay_seconds=1,
        circuit_retry_seconds=1,
        max_attempts=3,
        pause_between_requests_seconds=0,
        backfill_debt_limit=1,
        profile_owner_account_id=42,
    )
    job = HydrationQueueRepository(conn).due_jobs(int(time.time()), 1, kind="rich_composition")[0]
    response = types.Message(
        id=1, peer_id=types.PeerUser(1), date=datetime.fromtimestamp(1777593600, UTC), message="retained"
    )
    if result in ("rich", "late_edit"):
        response.rich_message = types.RichMessage(
            blocks=[types.PageBlockParagraph(text=types.TextPlain(text="recovered"))], photos=[], documents=[]
        )

    async def request(*_args: object, **_kwargs: object) -> object:
        if result == "late_edit":
            with write_transaction(conn):
                sequence = allocate_observation_order(conn)
                conn.execute(
                    "INSERT INTO message_observations(dialog_id,message_id,observation_order,is_deleted) VALUES (1,1,?,0)",
                    (sequence,),
                )
                conn.execute("UPDATE messages SET text='newer',edit_date=1777593700 WHERE dialog_id=1 AND message_id=1")
        return [] if result == "missing" else [response]

    with patch.object(worker, "_request_batch", side_effect=request):
        await worker._process_batch(handler, [job], int(time.time()))
    if result == "missing":
        assert conn.execute("SELECT terminal,last_outcome,last_error_code,attempts FROM hydration_jobs").fetchone() == (
            1,
            "terminal_error",
            "missing_response",
            2,
        )
    elif result == "late_edit":
        assert conn.execute("SELECT text,rich_message FROM messages").fetchone() == ("newer", None)
        assert conn.execute("SELECT terminal FROM hydration_jobs").fetchone() == (0,)
    else:
        rich = cast(tuple[str | None], conn.execute("SELECT rich_message FROM messages").fetchone())[0]
        assert rich is not None
        assert ("recovered" in rich) == (result == "rich")
        assert conn.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)
    conn.close()
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    if result == "missing":
        assert conn.execute("SELECT terminal,last_error_code FROM hydration_jobs").fetchone() == (1, "missing_response")
    elif result != "late_edit":
        assert conn.execute("SELECT COUNT(*) FROM hydration_jobs").fetchone() == (0,)
    conn.close()


def test_v105_rolls_back_queue_and_version_on_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "sync.db"
    with monkeypatch.context() as old:
        old.setattr(schema, "_CURRENT_SCHEMA_VERSION", 104)
        ensure_sync_schema(path)
    conn = _open_sync_db(path)
    conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (1,'synced')")
    conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES (1,1,1)")
    for message_id in (1, 2):
        conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at) VALUES (1,?,1777593600)", (message_id,))
        conn.execute(
            "INSERT INTO hydration_jobs(kind,dialog_id,message_id,due_at,priority,terminal,last_outcome) VALUES ('rich_composition',1,?,1,0,1,'terminal_error')",
            (message_id,),
        )
    conn.commit()
    conn.close()
    original = schema.hydration_observation_current
    calls = 0

    def fail_on_second(conn: schema.SyncDatabaseConnection, dialog_id: int, message_id: int, order: int | None) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected")
        return original(conn, dialog_id, message_id, order)

    monkeypatch.setattr(schema, "hydration_observation_current", fail_on_second)
    with pytest.raises(RuntimeError, match="injected"):
        ensure_sync_schema(path)
    conn = _open_sync_db(path)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone() == (104,)
    assert conn.execute("SELECT terminal,due_at FROM hydration_jobs ORDER BY message_id").fetchall() == [(1, 1), (1, 1)]
    conn.close()
