"""Referenced response peers publish only authorized canonical identity facts."""

import sqlite3
from contextlib import closing
from pathlib import Path

from telethon.tl import types

from mcp_telegram.dialog_fact_observations import claim_dialog_fact
from mcp_telegram.dialog_identity import publish_supplied_identities, read_dialog_identities
from mcp_telegram.observation_order import allocate_observation_order
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telethon_dialog import observe_referenced_dialog_identities


def test_referenced_identity_fields_order_and_reopened_local_reader(tmp_path: Path) -> None:
    path = tmp_path / "identity.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    with write_transaction(conn):
        conn.execute("INSERT INTO entities(id,type,name,username,updated_at) VALUES(88,'user','Old','old',1)")
        conn.execute("INSERT INTO dialogs(dialog_id,name,username,type) VALUES(88,'Old','old','user')")
        old_order = allocate_observation_order(conn)
        later_order = allocate_observation_order(conn)
        assert claim_dialog_fact(conn, 88, "name", later_order)
        conn.execute("UPDATE entities SET name='Live' WHERE id=88")
        conn.execute("UPDATE dialogs SET name='Live' WHERE dialog_id=88")
        response = [
            types.User(88, first_name="Late", username="new"),
            types.User(99, first_name="New", username="new99"),
            types.User(100, first_name="Unreferenced"),
        ]
        facts = observe_referenced_dialog_identities(response, {88, 99}, source="profile", observed_at=10)
        publish_supplied_identities(conn, facts, observation_order=old_order)
        assert conn.execute("SELECT name,username FROM entities WHERE id=88").fetchone() == ("Live", "new")
        assert conn.execute("SELECT name,username FROM dialogs WHERE dialog_id=88").fetchone() == ("Live", "new")
        assert conn.execute("SELECT name FROM entities WHERE id=100").fetchone() is None
        assert conn.execute("SELECT count(*) FROM dialogs WHERE dialog_id=99").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM full_history_enrollment").fetchone() == (0,)
        min_order = allocate_observation_order(conn)
        partial = observe_referenced_dialog_identities(
            [types.User(99, min=True, first_name="Partial")], {99}, source="profile", observed_at=11
        )
        publish_supplied_identities(conn, partial, observation_order=min_order)
        assert conn.execute("SELECT name,username FROM entities WHERE id=99").fetchone() == ("Partial", "new99")
        clear_order = allocate_observation_order(conn)
        clear = observe_referenced_dialog_identities(
            [types.User(99, first_name="Full")], {99}, source="profile", observed_at=12
        )
        publish_supplied_identities(conn, clear, observation_order=clear_order)
    conn.close()
    with closing(sqlite3.connect(path)) as reopened:
        identities = read_dialog_identities(reopened, [88, 99])
        assert (identities[88].name, identities[88].username) == ("Live", "new")
        assert (identities[99].name, identities[99].username) == ("Full", None)
        assert reopened.execute("SELECT count(*) FROM dialogs WHERE dialog_id=99").fetchone() == (0,)


def test_referenced_complete_and_stale_identity_do_not_change_other_fields(tmp_path: Path) -> None:
    path = tmp_path / "complete.db"
    ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn, write_transaction(conn):
        conn.execute("INSERT INTO entities(id,type,name,username,updated_at) VALUES(88,'user','Old','old',1)")
        conn.execute("INSERT INTO dialogs(dialog_id,name,username,type) VALUES(88,'Old','old','user')")
        old_order = allocate_observation_order(conn)
        newer_order = allocate_observation_order(conn)
        facts = observe_referenced_dialog_identities(
            [types.User(88, first_name="Accepted", username="new")], {88}, source="profile", observed_at=12
        )
        publish_supplied_identities(conn, facts, observation_order=newer_order)
        assert conn.execute("SELECT name,username,identity_complete FROM dialogs WHERE dialog_id=88").fetchone() == (
            "Accepted",
            "new",
            1,
        )
        stale = observe_referenced_dialog_identities(
            [types.User(88, first_name="Stale", username="stale")], {88}, source="profile", observed_at=13
        )
        publish_supplied_identities(conn, stale, observation_order=old_order)
        assert conn.execute("SELECT name,username,updated_at FROM entities WHERE id=88").fetchone() == (
            "Accepted",
            "new",
            12,
        )
        assert conn.execute("SELECT name,username,identity_complete FROM dialogs WHERE dialog_id=88").fetchone() == (
            "Accepted",
            "new",
            1,
        )


def test_unknown_partial_and_empty_peer_do_not_invent_entity(tmp_path: Path) -> None:
    path = tmp_path / "partial.db"
    ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn, write_transaction(conn):
        order = allocate_observation_order(conn)
        facts = observe_referenced_dialog_identities(
            [types.User(99, min=True, first_name="Partial"), types.UserEmpty(100)],
            {99, 100},
            source="profile",
            observed_at=10,
        )
        publish_supplied_identities(conn, facts, observation_order=order)
        assert conn.execute("SELECT count(*) FROM entities").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM dialogs").fetchone() == (0,)


def test_supported_groups_channels_and_bots_keep_classification(tmp_path: Path) -> None:
    path = tmp_path / "kinds.db"
    ensure_sync_schema(path)
    channel_id = -1000000000200
    with closing(sqlite3.connect(path)) as conn, write_transaction(conn):
        order = allocate_observation_order(conn)
        facts = observe_referenced_dialog_identities(
            [
                types.Chat(123, "Group", types.ChatPhotoEmpty(), 1, None, 1),
                types.Channel(200, "Channel", types.ChatPhotoEmpty(), None, username="channel"),
                types.User(88, bot=True, first_name="Bot", username="bot"),
            ],
            {-123, channel_id, 88},
            source="profile",
            observed_at=10,
        )
        publish_supplied_identities(conn, facts, observation_order=order)
        rows = conn.execute("SELECT id,type,name,username FROM entities ORDER BY id").fetchall()
        assert rows == [
            (channel_id, "channel", "Channel", "channel"),
            (-123, "group", "Group", None),
            (88, "bot", "Bot", "bot"),
        ]
        order = allocate_observation_order(conn)
        forbidden = observe_referenced_dialog_identities(
            [types.ChannelForbidden(200, 1, "Restricted")], {channel_id}, source="profile", observed_at=11
        )
        publish_supplied_identities(conn, forbidden, observation_order=order)
        assert conn.execute("SELECT type,name,username FROM entities WHERE id=?", (channel_id,)).fetchone() == (
            "channel",
            "Restricted",
            "channel",
        )
