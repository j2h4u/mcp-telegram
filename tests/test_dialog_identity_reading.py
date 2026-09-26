from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

from mcp_telegram.models import DialogType
from mcp_telegram.reading import ReadingDeps, ReadingService
from mcp_telegram.reading.query_records import read_message_from_row
from mcp_telegram.reading.service import _SearchMessagesRequest
from mcp_telegram.telegram_fragments import FragmentContextService
from mcp_telegram.telegram_reading import TelegramHistoryGateway


class _TestLogger:
    def debug(self, msg: str, *args: object, **kwargs: object) -> None:
        pass

    def info(self, msg: str, *args: object, **kwargs: object) -> None:
        pass

    def warning(self, msg: str, *args: object, **kwargs: object) -> None:
        pass

    def error(self, msg: str, *args: object, **kwargs: object) -> None:
        pass

    def exception(self, msg: str, *args: object, **kwargs: object) -> None:
        pass


def _seed_unread_dialog(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    name: str | None,
    kind: str,
    username: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO dialogs(dialog_id,name,type,username,identity_source,identity_complete) "
        "VALUES (?,?,?,?, 'directory', 1)",
        (dialog_id, name, kind, username),
    )
    conn.execute(
        "INSERT INTO synced_dialogs(dialog_id,status,read_inbox_max_id,last_event_at) VALUES (?,'synced',0,10)",
        (dialog_id,),
    )
    conn.execute(
        "INSERT INTO messages(dialog_id,message_id,sent_at,text,out,is_service,is_deleted) "
        "VALUES (?,1,10,'unread',0,0,0)",
        (dialog_id,),
    )


def test_inbox_selects_and_labels_with_one_canonical_identity_batch(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    conn = make_synced_db()
    _seed_unread_dialog(conn, -1001, name="ИИ Лаборатория", kind="supergroup")
    _seed_unread_dialog(conn, -1002, name="Old user cache", kind="user")
    conn.execute("INSERT INTO entities(id,type,name,username,updated_at) VALUES (-1001,'user','Stale','stale',99)")
    conn.execute(
        "INSERT INTO entities(id,type,name,username,updated_at) VALUES (-1002,'supergroup','Stale group','stalegroup',99)"
    )
    identities_reads = []
    conn.set_trace_callback(
        lambda sql: (
            identities_reads.append(sql) if "WHERE d.dialog_id IN" in sql and "identity_observed_at" in sql else None
        )
    )
    service = SimpleNamespace(
        _conn=conn,
        _deps=SimpleNamespace(deleted_message_visibility_seconds=86_400),
        _should_include_unread_dialog=ReadingService._should_include_unread_dialog,
    )

    entries, counts = ReadingService._collect_unread_dialogs(
        cast(ReadingService, service),
        group_size_threshold=100,
        include_dialog_types=(DialogType.SUPERGROUP,),
    )

    assert len(identities_reads) == 1
    assert counts == {-1001: 1}
    assert len(entries) == 1
    assert entries[0]["display_name"] == "ИИ Лаборатория"
    assert entries[0]["display_name_source"] == "name"
    assert entries[0]["category"] is DialogType.SUPERGROUP


def test_missing_dialog_name_keeps_numeric_display_provenance(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    conn = make_synced_db()
    _seed_unread_dialog(conn, -1003, name=None, kind="supergroup")
    service = SimpleNamespace(
        _conn=conn,
        _deps=SimpleNamespace(deleted_message_visibility_seconds=86_400),
        _should_include_unread_dialog=ReadingService._should_include_unread_dialog,
    )

    entries, _counts = ReadingService._collect_unread_dialogs(cast(ReadingService, service), 100)

    assert entries[0]["display_name"] == "-1003"
    assert entries[0]["display_name_source"] == "numeric"
    assert entries[0]["category"] is DialogType.SUPERGROUP


def test_unread_summary_uses_canonical_bundle_without_entities(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    conn = make_synced_db()
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO dialogs(dialog_id,name,type,identity_source,identity_complete,unread_count) "
        "VALUES (-1004,'ИИ Лаборатория','supergroup','directory',1,1)"
    )

    result = ReadingService._get_unread_summary_sync(conn, {"limit": 10})

    dialog = result["data"]["dialogs"][0]
    assert dialog["name"] == "ИИ Лаборатория"
    assert dialog["dialog_type"] == DialogType.SUPERGROUP.value
    assert dialog["display_name_source"] == "name"


def test_global_search_uses_canonical_dialog_label_and_source(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    conn = make_synced_db()
    conn.row_factory = sqlite3.Row
    _seed_unread_dialog(conn, -1005, name="ИИ Лаборатория", kind="supergroup")
    conn.execute("INSERT INTO entities(id,type,name,username,updated_at) VALUES (-1005,'user','Stale','stale',99)")
    conn.execute("INSERT INTO messages_fts(dialog_id,message_id,stemmed_text) VALUES (-1005,1,'needle')")
    conn.set_trace_callback(
        lambda sql: (
            identity_queries.append(sql) if "WHERE d.dialog_id IN" in sql and "identity_observed_at" in sql else None
        )
    )
    identity_queries: list[str] = []
    service = ReadingService(
        ReadingDeps(
            conn=conn,
            sync_db_path=None,
            self_id=1,
            resolve_dialog_id=lambda _selector: asyncio.sleep(0, result=0),
            fragment_context=cast(FragmentContextService, object()),
            history_gateway=cast(TelegramHistoryGateway, object()),
            logger=_TestLogger(),
            rid=lambda: "",
            deleted_message_visibility_seconds=86_400,
            draft_response_budget_bytes=1024,
        )
    )
    service._enrich_search_messages = lambda rows: [read_message_from_row(row) for row in rows]
    service._read_state_per_dialog = lambda messages: {}

    result = asyncio.run(
        service._search_messages_global_result(
            _SearchMessagesRequest(0, None, "needle", 10, 0, None, "sent"),
            '"needle"',
        )
    )

    message = result["data"]["messages"][0]
    assert message["dialog_name"] == "ИИ Лаборатория"
    assert message["dialog_name_source"] == "name"
    assert len(identity_queries) == 1
