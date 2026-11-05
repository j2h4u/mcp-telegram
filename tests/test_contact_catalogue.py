"""Contact snapshots remain canonical, account scoped, and usable without dialogs."""

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest
from telethon.tl import functions, types

from mcp_telegram.auth_scope import TelegramAuthScope
from mcp_telegram.contacts.refresh import ContactCatalogueRefresher
from mcp_telegram.contacts.repository import read_contacts, request_contact_catalogue
from mcp_telegram.contacts.state import read_contact_command_state
from mcp_telegram.image_authorization import ScopeCell
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema, open_sync_db_reader
from mcp_telegram.sync_transactions import write_transaction


@pytest.mark.parametrize(
    ("value", "authority", "expected"),
    [
        (True, True, True),
        (False, True, False),
        (None, True, None),
        ("true", True, None),
        (1, True, None),
        (True, False, None),
        (True, 1, None),
        (True, None, None),
        ("__missing__", True, None),
    ],
)
def test_contact_mutuality_uses_only_authoritative_boolean(
    tmp_path: Path, value: object, authority: object, expected: bool | None
) -> None:
    path = tmp_path / "contacts.db"
    ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        fields = {"user.contact": True, "user.mutual_contact": value}
        if value == "__missing__":
            del fields["user.mutual_contact"]
        coverage = {"user.contact": {"authoritative": True}, "user.mutual_contact": {"authoritative": authority}}
        conn.execute(
            "INSERT INTO entity_profile_semantic_snapshots(owner_account_id,entity_id,normalization_version,fields_json,coverage_json,provenance_json,source) VALUES(42,55,'v1',?,?,?,'rpc')",
            (json.dumps(fields), json.dumps(coverage), "{}"),
        )
        conn.execute(
            "INSERT INTO entity_profile_semantic_snapshots(owner_account_id,entity_id,normalization_version,fields_json,coverage_json,provenance_json,source) VALUES(43,55,'v1',?,?,?,'rpc')",
            (
                json.dumps({"user.contact": True, "user.mutual_contact": True}),
                json.dumps({"user.contact": {"authoritative": True}, "user.mutual_contact": {"authoritative": True}}),
                "{}",
            ),
        )
        conn.commit()
        before = read_contact_command_state(conn, 42, 55)["expected_version"]
    with closing(sqlite3.connect(path)) as conn:
        page = read_contacts(conn, 42, limit=10, after_entity_id=None)
        assert cast(list[dict[str, object]], page["items"])[0]["mutual_contact"] is expected
        other = read_contacts(conn, 43, limit=10, after_entity_id=None)
        assert cast(list[dict[str, object]], other["items"])[0]["mutual_contact"] is True
        assert read_contact_command_state(conn, 42, 55)["expected_version"] == before
        conn.execute(
            "UPDATE entity_profile_semantic_snapshots SET fields_json=json_set(fields_json, '$.\"user.mutual_contact\"', json('false')) WHERE owner_account_id=42 AND entity_id=55"
        )
        assert read_contact_command_state(conn, 42, 55)["expected_version"] == before


@pytest.mark.asyncio
@pytest.mark.parametrize("mutual", [True, False])
async def test_complete_snapshot_keeps_contact_without_dialog_and_removes_only_membership(
    tmp_path: Path, request: pytest.FixtureRequest, mutual: bool
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    request.addfinalizer(reader.close)
    cell = ScopeCell()
    cell.publish(TelegramAuthScope(1, 42, 2, 3))
    responses: list[object] = [
        types.contacts.Contacts(
            [types.Contact(55, mutual)],
            1,
            [types.User(55, contact=True, mutual_contact=not mutual, first_name="Saved")],
        ),
        types.contacts.Contacts([types.Contact(55, not mutual)], 1, [types.User(55, contact=True)]),
        types.contacts.Contacts([types.Contact(55, cast(bool, 1))], 1, [types.User(55, contact=True)]),
        types.contacts.Contacts([], 0, []),
    ]

    class Client:
        async def __call__(self, request: object) -> object:
            del request
            return responses.pop(0)

    def request_refresh(conn: sqlite3.Connection) -> int:
        with write_transaction(conn):
            return request_contact_catalogue(conn, 42)

    refresher = ContactCatalogueRefresher(Client(), reader, writer, capture_scope=cell.capture, claim_scope=cell.claim)
    revision = await writer.run(request_refresh)
    result = await refresher.refresh(expected_revision=revision)
    assert result.accepted
    assert read_contact_command_state(reader, 42, 55)["observation_order"] == result.observation_order
    contacts = read_contacts(reader, 42, limit=10, after_entity_id=None)
    assert contacts["items"] == [
        {
            "entity_id": 55,
            "membership": True,
            "mutual_contact": mutual,
            "first_name": "Saved",
            "last_name": "",
            "expected_version": read_contact_command_state(reader, 42, 55)["expected_version"],
        }
    ]
    assert reader.execute("SELECT 1 FROM dialogs WHERE dialog_id=55").fetchone() is None
    assert read_contacts(reader, 43, limit=10, after_entity_id=None)["items"] == []
    revision = await writer.run(request_refresh)
    assert (await refresher.refresh(expected_revision=revision)).accepted
    assert cast(list[dict[str, object]], read_contacts(reader, 42, limit=10, after_entity_id=None)["items"])[0][
        "mutual_contact"
    ] is (not mutual)
    revision = await writer.run(request_refresh)
    assert (await refresher.refresh(expected_revision=revision)).accepted
    assert cast(list[dict[str, object]], read_contacts(reader, 42, limit=10, after_entity_id=None)["items"])[0][
        "mutual_contact"
    ] is (not mutual)
    revision = await writer.run(request_refresh)
    assert (await refresher.refresh(expected_revision=revision)).accepted
    state = read_contact_command_state(reader, 42, 55)
    assert state["membership"] is False and state["expected_version"] is not None
    assert state["first_name"] is None and state["last_name"] is None and state["phone_state"] == "unavailable"
    reader.close()
    await writer.aclose()


@pytest.mark.asyncio
async def test_partial_snapshot_does_not_remove_retained_contacts(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    cell = ScopeCell()
    cell.publish(TelegramAuthScope(1, 42, 2, 3))
    response = types.contacts.Contacts([types.Contact(55, False)], 1, [])

    class Client:
        async def __call__(self, request: object) -> object:
            del request
            return response

    def request(conn: sqlite3.Connection) -> int:
        with write_transaction(conn):
            return request_contact_catalogue(conn, 42)

    revision = await writer.run(request)
    refresher = ContactCatalogueRefresher(Client(), reader, writer, capture_scope=cell.capture, claim_scope=cell.claim)
    assert not (await refresher.refresh(expected_revision=revision)).accepted
    assert read_contacts(reader, 42, limit=10, after_entity_id=None)["coverage"] == {
        "state": "unknown",
        "observed_at": None,
    }
    reader.close()
    await writer.aclose()


@pytest.mark.parametrize("failure", ["partial", "not_modified", "storage"])
async def test_incomplete_or_failed_refresh_retains_saved_baseline(
    tmp_path: Path, failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_telegram.contacts import refresh as contact_refresh

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    cell = ScopeCell()
    assert cell.publish(TelegramAuthScope(1, 42, 2, 3))
    baseline = types.contacts.Contacts(
        [types.Contact(55, False)], 1, [types.User(55, contact=True, first_name="Saved", phone="123")]
    )
    next_response: object = baseline

    class Client:
        async def __call__(self, request: object) -> object:
            assert isinstance(request, functions.contacts.GetContactsRequest)
            assert request.hash == 0
            return next_response

    def request(conn: sqlite3.Connection) -> int:
        with write_transaction(conn):
            return request_contact_catalogue(conn, 42)

    refresher = ContactCatalogueRefresher(Client(), reader, writer, capture_scope=cell.capture, claim_scope=cell.claim)
    try:
        assert (await refresher.refresh(expected_revision=await writer.run(request))).accepted
        before = read_contact_command_state(reader, 42, 55)
        coverage = read_contacts(reader, 42, limit=10, after_entity_id=None)["coverage"]
        revision = await writer.run(request)
        if failure == "partial":
            next_response = types.contacts.Contacts([types.Contact(99, False)], 1, [])
        elif failure == "not_modified":
            next_response = types.contacts.ContactsNotModified()
        else:
            next_response = types.contacts.Contacts(
                [types.Contact(99, False)], 1, [types.User(99, contact=True, first_name="New")]
            )
            original = contact_refresh._publish_contact

            def fail_after_write(*args: object, **kwargs: object) -> None:
                cast(Callable[..., None], original)(*args, **kwargs)
                raise sqlite3.OperationalError("controlled storage failure")

            monkeypatch.setattr(contact_refresh, "_publish_contact", fail_after_write)
        if failure == "storage":
            with pytest.raises(sqlite3.OperationalError, match="controlled storage failure"):
                await refresher.refresh(expected_revision=revision)
        else:
            assert not (await refresher.refresh(expected_revision=revision)).accepted
        assert read_contact_command_state(reader, 42, 55) == before
        assert read_contact_command_state(reader, 42, 99)["membership"] is None
        assert read_contacts(reader, 42, limit=10, after_entity_id=None)["coverage"] == coverage
    finally:
        reader.close()
        await writer.aclose()
    with closing(sqlite3.connect(path)) as reopened:
        assert read_contact_command_state(reopened, 42, 55) == before
        assert read_contact_command_state(reopened, 42, 99)["membership"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", ["revision", "scope", "not_modified"])
async def test_stale_or_incomplete_reply_never_publishes(tmp_path: Path, interrupt: str) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    cell = ScopeCell()
    scope = TelegramAuthScope(1, 42, 2, 3)
    cell.publish(scope)

    def request(conn: sqlite3.Connection) -> int:
        with write_transaction(conn):
            return request_contact_catalogue(conn, 42)

    revision = await writer.run(request)

    class Client:
        async def __call__(self, request: object) -> object:
            del request
            if interrupt == "revision":
                await writer.run(request_refresh)
            elif interrupt == "scope":
                cell.begin_change()
            elif interrupt == "not_modified":
                return types.contacts.ContactsNotModified()
            return types.contacts.Contacts([types.Contact(55, False)], 1, [types.User(55, first_name="Saved")])

    request_refresh = request
    refresher = ContactCatalogueRefresher(Client(), reader, writer, capture_scope=cell.capture, claim_scope=cell.claim)
    assert not (await refresher.refresh(expected_revision=revision)).accepted
    assert read_contact_command_state(reader, 42, 55)["membership"] is None
    assert read_contacts(reader, 42, limit=10, after_entity_id=None)["coverage"] == {
        "state": "unknown",
        "observed_at": None,
    }
    reader.close()
    await writer.aclose()


async def test_newer_mutual_observation_wins_over_inflight_catalogue(tmp_path: Path) -> None:
    from mcp_telegram.entity_profile.contracts import (
        ProfileFieldClass,
        ProfileFieldCoverage,
        ProfileFieldObservation,
        ProfileFieldState,
        SuppliedProfileUser,
    )
    from mcp_telegram.entity_profile.repository import EntityProfileRepository
    from mcp_telegram.observation_order import allocate_observation_order

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    cell = ScopeCell()
    cell.publish(TelegramAuthScope(1, 42, 2, 3))

    def request(conn: sqlite3.Connection) -> int:
        with write_transaction(conn):
            return request_contact_catalogue(conn, 42)

    def publish_newer(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            order = allocate_observation_order(conn)
            conn.execute("INSERT INTO entities(id,type,updated_at) VALUES(55,'user',1)")
            coverage = ProfileFieldCoverage(ProfileFieldClass.PRIVATE_FACT, ProfileFieldState.PRESENT, True)
            EntityProfileRepository(conn, section_ttl_seconds=300).apply_supplied_user(
                SuppliedProfileUser(
                    55, {"user.mutual_contact": True}, {"user.mutual_contact": coverage}, {}, False, False
                ),
                observation=ProfileFieldObservation(42, order, 200, "update"),
            )

    class Client:
        async def __call__(self, request: object) -> object:
            del request
            await writer.run(publish_newer)
            return types.contacts.Contacts([types.Contact(55, False)], 1, [types.User(55)])

    try:
        revision = await writer.run(request)
        refresher = ContactCatalogueRefresher(
            Client(), reader, writer, capture_scope=cell.capture, claim_scope=cell.claim
        )
        assert (await refresher.refresh(expected_revision=revision)).accepted
        items = cast(list[dict[str, object]], read_contacts(reader, 42, limit=10, after_entity_id=None)["items"])
        assert items[0]["mutual_contact"] is True
    finally:
        reader.close()
        await writer.aclose()


def test_contact_only_profile_and_rename_preserve_public_identity(tmp_path: Path) -> None:
    import asyncio
    from unittest.mock import MagicMock

    from mcp_telegram.sync_db import _open_sync_db
    from sqlite_writer_helpers import event_manager

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    with write_transaction(conn):
        conn.execute("INSERT INTO entities(id,type,name,updated_at) VALUES (55,'user','Public',1)")
    manager = event_manager(MagicMock(), conn, asyncio.Event())
    manager.set_self_id(42)
    user = types.User(55, contact=True, first_name="Saved", phone="123")
    manager._write_profile_pushes(conn, object(), [user], [])
    initial = read_contact_command_state(conn, 42, 55)
    assert initial["first_name"] == "Saved" and initial["observation_order"] is not None
    manager._write_profile_pushes(conn, types.UpdateUserPhone(55, "456"), [], [])
    changed = read_contact_command_state(conn, 42, 55)
    assert changed["phone"] == "456" and changed["observation_order"] is None
    assert conn.execute("SELECT name FROM entities WHERE id=55").fetchone() == ("Public",)
    manager._write_profile_pushes(conn, types.UpdateUserName(55, "Renamed", "", []), [], [])
    state = read_contact_command_state(conn, 42, 55)
    assert state["first_name"] == "Saved"
    semantic = cast(
        tuple[str] | None,
        conn.execute(
            "SELECT json_extract(fields_json, '$.\"user.first_name\"') FROM entity_profile_semantic_snapshots WHERE owner_account_id=42 AND entity_id=55"
        ).fetchone(),
    )
    assert semantic == ("Renamed",)
    history_before = cast(
        list[tuple[str]],
        conn.execute(
            "SELECT change_key FROM entity_profile_change_events WHERE change_key LIKE 'user.%' ORDER BY event_id"
        ).fetchall(),
    )
    manager._write_profile_pushes(conn, object(), [types.User(55, contact=True, first_name="New alias")], [])
    assert read_contact_command_state(conn, 42, 55)["first_name"] == "New alias"
    assert conn.execute(
        "SELECT json_extract(fields_json, '$.\"user.first_name\"') FROM entity_profile_semantic_snapshots WHERE owner_account_id=42 AND entity_id=55"
    ).fetchone() == ("Renamed",)
    assert (
        conn.execute(
            "SELECT change_key FROM entity_profile_change_events WHERE change_key LIKE 'user.%' ORDER BY event_id"
        ).fetchall()
        == history_before
    )
    assert conn.execute("SELECT name FROM entities WHERE id=55").fetchone() == ("Public",)
    assert conn.execute("SELECT 1 FROM dialogs WHERE dialog_id=55").fetchone() is None
    manager._write_profile_pushes(conn, types.UpdateUserPhone(55, ""), [], [])
    state = read_contact_command_state(conn, 42, 55)
    assert state["phone_state"] == "unavailable" and state["phone"] is None
    fields = cast(
        tuple[str] | None,
        conn.execute(
            "SELECT fields_json FROM entity_profile_semantic_snapshots WHERE owner_account_id=42 AND entity_id=55"
        ).fetchone(),
    )
    assert fields is not None and '"user.phone":"456"' in fields[0]
    manager._write_profile_pushes(conn, object(), [types.User(55, first_name="Public")], [])
    assert read_contact_command_state(conn, 42, 55)["membership"] is False
    conn.close()


def test_contact_intake_revokes_tracked_peer_and_reset_revokes_all() -> None:
    from mcp_telegram.contacts.intake import (
        capture_contact_intake,
        catalogue_marker,
        intake_marker,
        invalidate_contact_scope,
        is_current,
        track_contact,
    )

    before = catalogue_marker()
    with track_contact(55) as first, track_contact(56) as second:
        observed = intake_marker(55)
        capture_contact_intake([types.UpdateUserName(55, "Name", "", [])])
        assert not is_current(55, first) and is_current(56, second)
        assert intake_marker(55) != observed and catalogue_marker() > before
        capture_contact_intake([types.UpdateContactsReset()])
        assert not is_current(56, second)
        assert intake_marker(55) != observed
    before = catalogue_marker()
    invalidate_contact_scope()
    assert catalogue_marker() > before


def test_full_user_without_surname_can_compose_rename(tmp_path: Path) -> None:
    from telethon.tl.functions.contacts import AddContactRequest

    from mcp_telegram.contacts.composition import ContactIntent, compose_contact
    from mcp_telegram.entity_profile.collection_repository import store_semantic_snapshot
    from mcp_telegram.entity_profile.contracts import ObservationBoundary, TargetKind
    from mcp_telegram.entity_profile.full_user_normalization import normalize_full_user_response

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    full = types.UserFull(55, types.PeerSettings(), types.PeerNotifySettings(), 0)
    response = types.users.UserFull(full, [], [types.User(55, contact=True, first_name="Saved", last_name=None)])
    result = normalize_full_user_response(
        response,
        target_id=55,
        target_kind=TargetKind.USER,
        owner_account_id=42,
        observation=ObservationBoundary(100, 101),
    )
    assert result.semantic_snapshot is not None
    with write_transaction(conn):
        conn.execute("INSERT INTO entities(id,type,updated_at) VALUES (55,'user',1)")
        store_semantic_snapshot(conn, result.semantic_snapshot, 1)
    state = read_contact_command_state(conn, 42, 55)
    assert state["first_name"] == "Saved" and state["last_name"] == ""
    request = compose_contact(ContactIntent("update", "Renamed", None), state, types.InputUser(55, 10))
    assert isinstance(request, AddContactRequest)
    assert request.first_name == "Renamed" and request.last_name == ""
    conn.close()


def test_migration_normalizes_ambiguous_current_contact_names_without_touching_history(tmp_path: Path) -> None:
    import json

    from mcp_telegram.sync_db import _apply_migration_98

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("DROP TABLE contact_operation_receipts")
        conn.execute("DROP TABLE contact_catalogue_state")
        conn.execute("DELETE FROM schema_version WHERE version=98")
        conn.executemany("INSERT INTO entities(id,type,updated_at) VALUES (?,'user',1)", [(55,), (56,)])
        for entity, contact in ((55, True), (56, False)):
            fields = {
                "user.contact": contact,
                "user.first_name": "Old",
                "user.last_name": "Alias",
                "user.username": "keep",
            }
            coverage = {key: {"authoritative": True} for key in fields}
            provenance = {"fields": {key: {"order": 1} for key in fields}}
            conn.execute(
                "INSERT INTO entity_profile_semantic_snapshots(owner_account_id,entity_id,normalization_version,fields_json,coverage_json,provenance_json,source) VALUES (42,?,'v',?,?,?,'rpc')",
                (entity, json.dumps(fields), json.dumps(coverage), json.dumps(provenance)),
            )
        conn.execute(
            "INSERT INTO entity_profile_change_events(owner_account_id,entity_id,observation_order,change_key,category,before_json,after_json,previous_provenance_json,observed_at,source) VALUES (42,55,1,'user.first_name','profile','\"old\"','\"new\"','{}',1,'rpc')"
        )
    assert _apply_migration_98(conn, 97) == 98
    rows = cast(
        list[tuple[int, str, str, str]],
        conn.execute(
            "SELECT entity_id,fields_json,coverage_json,provenance_json FROM entity_profile_semantic_snapshots ORDER BY entity_id"
        ).fetchall(),
    )
    contact_fields, contact_coverage, contact_provenance = (
        cast(dict[str, object], json.loads(value)) for value in rows[0][1:]
    )
    assert "user.first_name" not in contact_fields and "user.last_name" not in contact_fields
    provenance_fields = contact_provenance["fields"]
    assert isinstance(provenance_fields, dict)
    assert "user.first_name" not in contact_coverage and "user.first_name" not in provenance_fields
    assert contact_fields["user.username"] == "keep"
    assert json.loads(rows[1][1])["user.first_name"] == "Old"
    assert conn.execute("SELECT change_key,before_json,after_json FROM entity_profile_change_events").fetchone() == (
        "user.first_name",
        '"old"',
        '"new"',
    )
    conn.close()
