"""Accepted collection pages publish sidecar identities atomically and by field clock."""

import sqlite3
from typing import cast

import pytest
from telethon.tl import types

from mcp_telegram.dialog_fact_observations import claim_dialog_fact
from mcp_telegram.dialog_identity import capture_identity_baseline, publish_dialog_identity
from mcp_telegram.dialog_identity_contracts import DialogIdentityObservation
from mcp_telegram.entity_profile.collection_repository import store_semantic_snapshot
from mcp_telegram.entity_profile.contracts import ObservationBoundary, ProfileCollectionPage
from mcp_telegram.entity_profile.semantic_normalization import normalize_semantic_profile
from mcp_telegram.models import DialogType
from mcp_telegram.sync_transactions import write_transaction
from tests.test_profile_collection_pages import Database, _commit
from tests.test_profile_collection_pages import database as database


def _seed_sidecar(conn: sqlite3.Connection) -> None:
    with write_transaction(conn):
        conn.execute("INSERT INTO entities(id,type,name,username,updated_at) VALUES(99,'user','Before','before',10)")
        conn.execute("INSERT INTO dialogs(dialog_id,type,name,username) VALUES(99,'user','Before','before')")
        conn.execute(
            "INSERT INTO entity_details(entity_id,detail_json,fetched_at,profile_owner_account_id) "
            'VALUES(99,\'{"schema":1,"about":"retained private bio"}\',10,77)'
        )
        snapshot = normalize_semantic_profile(
            types.User(99, first_name="Before", username="before"),
            types.UserFull(99, types.PeerSettings(), types.PeerNotifySettings(), 0, about="retained private bio"),
            target_id=99,
            owner_account_id=77,
            observation=ObservationBoundary(10, 11),
        )
        assert store_semantic_snapshot(conn, snapshot, 1) is not None
        assert publish_dialog_identity(
            conn,
            99,
            DialogIdentityObservation(
                99,
                name="Before",
                username="before",
                dialog_type=DialogType.USER,
                complete=True,
                source="realtime",
                observed_at=10,
            ),
            capture_identity_baseline(conn, 99),
        )
        assert claim_dialog_fact(conn, 99, "name", 10)
        assert claim_dialog_fact(conn, 99, "username", 10)


def _page(*identities: DialogIdentityObservation) -> ProfileCollectionPage:
    return ProfileCollectionPage(
        42, "avatar_history", (), ObservationBoundary(100, 101), 0, terminal=True, sidecar_identities=identities
    )


def _identity(*, peer: int = 99, complete: bool = True) -> DialogIdentityObservation:
    return DialogIdentityObservation(
        peer,
        name="After",
        username="after",
        dialog_type=DialogType.USER,
        complete=complete,
        source="profile",
        observed_at=100,
    )


def _rows(conn: sqlite3.Connection, sql: str) -> list[tuple[object, ...]]:
    return cast(list[tuple[object, ...]], conn.execute(sql).fetchall())


def _private_rows(conn: sqlite3.Connection) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    return (
        _rows(conn, "SELECT * FROM entity_details WHERE entity_id=99"),
        _rows(conn, "SELECT * FROM entity_profile_semantic_snapshots WHERE entity_id=99"),
    )


def test_complete_sidecar_publishes_canonical_identity_without_renewing_private_profile(database: Database) -> None:
    _path, conn, repo = database
    _seed_sidecar(conn)
    private = _private_rows(conn)
    with write_transaction(conn):
        assert _commit(repo, _page(_identity()))
    assert _rows(conn, "SELECT name,username,updated_at FROM entities WHERE id=99") == [("After", "after", 100)]
    assert _rows(
        conn,
        "SELECT name,username,identity_observed_at,identity_source,identity_complete FROM dialogs WHERE dialog_id=99",
    ) == [("After", "after", 100, "profile", 1)]
    assert _rows(
        conn, "SELECT field,observation_order FROM dialog_fact_observations WHERE dialog_id=99 ORDER BY field"
    ) == [("name", 100), ("username", 100)]
    assert _private_rows(conn) == private


@pytest.mark.parametrize("newer_field", ["name", "username"])
def test_sidecar_honors_independent_identity_clocks(database: Database, newer_field: str) -> None:
    _path, conn, repo = database
    _seed_sidecar(conn)
    with write_transaction(conn):
        assert claim_dialog_fact(conn, 99, newer_field, 200)
    private = _private_rows(conn)
    with write_transaction(conn):
        assert _commit(repo, _page(_identity()))
    expected = ("Before", "after") if newer_field == "name" else ("After", "before")
    assert _rows(conn, "SELECT name,username FROM entities WHERE id=99") == [expected]
    assert _rows(conn, "SELECT name,username FROM dialogs WHERE dialog_id=99") == [expected]
    assert _rows(
        conn, "SELECT field,observation_order FROM dialog_fact_observations WHERE dialog_id=99 ORDER BY field"
    ) == [(field, 200 if field == newer_field else 100) for field in ("name", "username")]
    assert _private_rows(conn) == private


def test_incomplete_unknown_and_target_sidecars_do_not_invent_identity(database: Database) -> None:
    _path, conn, repo = database
    _seed_sidecar(conn)
    before = (
        _rows(conn, "SELECT * FROM entities"),
        _rows(conn, "SELECT * FROM dialogs"),
        _rows(conn, "SELECT * FROM dialog_fact_observations"),
    )
    private = _private_rows(conn)
    with write_transaction(conn):
        assert _commit(repo, _page(_identity(complete=False), _identity(peer=999), _identity(peer=42)))
    assert (
        _rows(conn, "SELECT * FROM entities"),
        _rows(conn, "SELECT * FROM dialogs"),
        _rows(conn, "SELECT * FROM dialog_fact_observations"),
    ) == before
    assert _private_rows(conn) == private


def test_rejected_collection_transaction_cannot_leak_sidecar_identity_or_clocks(database: Database) -> None:
    _path, conn, repo = database
    _seed_sidecar(conn)
    before = (
        _rows(conn, "SELECT * FROM entities"),
        _rows(conn, "SELECT * FROM dialogs"),
        _rows(conn, "SELECT * FROM dialog_fact_observations"),
    )
    private = _private_rows(conn)
    with write_transaction(conn):
        conn.execute(
            "CREATE TRIGGER reject_page_advance BEFORE UPDATE OF next_section ON entity_profile_refresh_state WHEN NEW.next_section<>OLD.next_section BEGIN SELECT RAISE(ABORT,'reject page advance'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="reject page advance"), write_transaction(conn):
        _commit(repo, _page(_identity()))
    assert (
        _rows(conn, "SELECT * FROM entities"),
        _rows(conn, "SELECT * FROM dialogs"),
        _rows(conn, "SELECT * FROM dialog_fact_observations"),
    ) == before
    assert _private_rows(conn) == private
    assert _rows(conn, "SELECT * FROM entity_profile_collection_membership") == []
