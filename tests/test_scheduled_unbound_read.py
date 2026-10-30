import pytest

from tests.test_daemon_api import _insert_message, _insert_synced_dialog, _make_db_with_dialogs, _seed_dialog_row
from tests.test_scheduled_read_surface import (
    FUTURE_BASE,
    _create_draft_projection,
    _create_scheduled_table,
    _insert_scheduled,
    _make_scheduled_server,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["missing", "unbound"])
async def test_unbound_queue_hides_legacy_rows_but_keeps_local_history(owner: str) -> None:
    server = _make_scheduled_server(_make_db_with_dialogs())
    conn = server._conn
    _seed_dialog_row(conn, 1, name="Local dialog")
    _insert_synced_dialog(conn, 1, status="synced", last_synced_at=100)
    _insert_message(conn, 1, 10, sent_at=10, text="local one")
    _insert_message(conn, 1, 11, sent_at=20, text="local two")
    _create_scheduled_table(conn)
    _insert_scheduled(conn, 90, FUTURE_BASE, "unattributed legacy")
    _create_draft_projection(conn)
    conn.execute(
        "INSERT INTO draft_current VALUES (7,1,0,0,'present','retained draft','[]',NULL,NULL,NULL,NULL,NULL,"
        "0,0,1,'realtime_present',30,30,30,1,1)"
    )
    conn.execute("INSERT INTO draft_sync_state VALUES (7,'ready','complete',30,30,NULL)")
    if owner == "missing":
        conn.execute("DELETE FROM scheduled_sync_state WHERE key='account'")
    else:
        conn.execute("UPDATE scheduled_sync_state SET account_id=NULL WHERE key='account'")
    conn.commit()

    queue = await server._list_messages({"message_state": "scheduled", "read_scope": "all_dialogs"})
    assert queue["ok"] is True
    assert queue["data"]["messages"] == []
    assert queue["data"]["scheduled_coverage"] == {"state": "unknown", "reason": "scheduled_account_unbound"}
    assert queue["data"]["next_navigation"] is None

    scoped = await server._list_messages({"message_state": "scheduled", "dialog_id": 1})
    assert scoped["ok"] is True and scoped["data"]["messages"] == []
    assert scoped["data"]["dialog_scheduled_coverage"]["state"] == "unknown"

    result = await server._list_messages({"message_state": "all", "dialog_id": 1, "direction": "oldest", "limit": 1})
    rows = []
    for _ in range(4):
        assert result["ok"] is True
        rows.extend(result["data"]["messages"])
        assert result["data"]["scheduled_coverage"]["state"] == "unknown"
        navigation = result["data"]["next_navigation"]
        if navigation is None:
            break
        result = await server._list_messages(
            {"message_state": "all", "dialog_id": 1, "direction": "oldest", "limit": 1, "navigation": navigation}
        )
    assert [(row["message_state"], row.get("message_id")) for row in rows] == [
        ("sent", 10),
        ("sent", 11),
        ("draft", None),
    ]

    empty = await server._list_messages({"message_state": "all", "dialog_id": 1, "until_utc": "1970-01-01T00:00:05Z"})
    assert empty["data"]["messages"] == []
    assert empty["data"]["selection_state"] == "unknown"

    dialogs = await server._list_dialogs({})
    assert dialogs["data"]["scheduled_coverage"]["state"] == "unknown"
    assert all(dialog["scheduled_count"] == 0 for dialog in dialogs["data"]["dialogs"])

    searched = await server._search_messages({"dialog_id": 1, "message_state": "scheduled", "query": "unattributed"})
    assert searched["ok"] is True and searched["data"]["messages"] == []
    assert searched["data"]["scheduled_coverage"]["state"] == "unknown"


@pytest.mark.asyncio
async def test_bound_other_account_remains_hard_mismatch() -> None:
    server = _make_scheduled_server()
    conn = server._conn
    _create_scheduled_table(conn)
    _insert_scheduled(conn, 90, FUTURE_BASE, "other account")
    conn.execute("UPDATE scheduled_sync_state SET account_id=8 WHERE key='account'")
    result = await server._list_messages({"message_state": "scheduled", "read_scope": "all_dialogs"})
    assert result["ok"] is False and result["error"] == "scheduled_account_mismatch"
