"""Dialog message dates remain separate from synchronization observations."""

from tests.test_daemon_api import _insert_synced_dialog, _make_db_with_dialogs, _seed_dialog_row, make_server


async def test_dialog_list_uses_only_stored_message_time_and_keeps_observation_times() -> None:
    conn = _make_db_with_dialogs()
    try:
        _seed_dialog_row(conn, 1, last_message_at=100)
        _seed_dialog_row(conn, 2, last_message_at=None)
        _seed_dialog_row(conn, 4, last_message_at=None)
        _seed_dialog_row(conn, 5, last_message_at=None)
        _insert_synced_dialog(conn, 1, status="synced", last_event_at=400, last_synced_at=500)
        _insert_synced_dialog(conn, 2, status="access_lost", last_event_at=600, last_synced_at=700, access_lost_at=800)
        _insert_synced_dialog(
            conn, 3, status="access_lost", last_event_at=900, last_synced_at=1000, access_lost_at=1100
        )
        _insert_synced_dialog(conn, 4, status="synced", last_event_at=600)
        _insert_synced_dialog(conn, 5, status="synced", last_synced_at=700)
        _insert_synced_dialog(conn, 6, status="access_lost", access_lost_at=1100)
        # A retained deleted row does not create a new last-message fact.
        conn.executemany(
            "INSERT INTO messages(dialog_id,message_id,sent_at,is_deleted) VALUES(?,1,1200,1)", [(1,), (3,)]
        )
        result = await make_server(conn)._list_dialogs({})
        rows = result["data"]["dialogs"]
        assert [row["id"] for row in rows] == [1, 2, 3, 4, 5, 6]
        by_id = {row["id"]: row for row in rows}
        assert [by_id[dialog_id]["last_message_at"] for dialog_id in (1, 2, 3, 4, 5, 6)] == [
            100,
            None,
            None,
            None,
            None,
            None,
        ]
        assert by_id[2]["access_lost_at"] == 800
        assert by_id[3]["access_lost_at"] == 1100
    finally:
        conn.close()
