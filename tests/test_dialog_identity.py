from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import cast

import pytest

from mcp_telegram.dialog_identity import (
    capture_identity_baseline,
    publish_dialog_identity,
    read_dialog_identities,
    read_local_dialog_identities,
)
from mcp_telegram.dialog_identity_contracts import IDENTITY_OMITTED, DialogIdentityObservation
from mcp_telegram.models import DialogType


def _dialog(conn: sqlite3.Connection, dialog_id: int, **values: object) -> None:
    fields = {"dialog_id": dialog_id, **values}
    conn.execute(
        f"INSERT INTO dialogs({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", tuple(fields.values())
    )


def test_complete_publish_is_existing_row_cas_and_keeps_presence_revision(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    conn = make_synced_db()
    _dialog(conn, 10, name="Old", type="user", username="old")
    baseline = capture_identity_baseline(conn, 10)
    observation = DialogIdentityObservation(10, "New", None, DialogType.SUPERGROUP, True, "realtime", 123)
    assert publish_dialog_identity(conn, 10, observation, baseline)
    assert not publish_dialog_identity(conn, 10, observation, baseline)  # equal time cannot defeat the revision fence
    row = cast(
        tuple[str | None, str | None, str | None, int | None, int, str | None, int, int],
        conn.execute(
            "SELECT name,username,type,identity_observed_at,identity_complete,identity_source,identity_revision,revision "
            "FROM dialogs WHERE dialog_id=10"
        ).fetchone(),
    )
    assert row == ("New", None, "supergroup", 123, 1, "realtime", 1, 0)


def test_canonical_bundle_suppresses_stale_entity_fields_and_numeric_is_explicit(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    conn = make_synced_db()
    _dialog(conn, 11, name=None, type="supergroup", username=None, identity_source="directory")
    conn.execute("INSERT INTO entities(id,type,name,username,updated_at) VALUES (11,'user','Stale','stale',999)")
    identity = read_dialog_identities(conn, [11])[11]
    assert (identity.name, identity.username, identity.dialog_type) == (None, None, DialogType.SUPERGROUP)
    assert (identity.display_name, identity.display_name_source) == ("11", "numeric")
    assert read_local_dialog_identities(conn)[11] == identity


def test_partial_clear_preserves_omitted_fields_but_forgets_legacy_age(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    conn = make_synced_db()
    _dialog(conn, 12, name="Known", type="user", username="known", identity_source="legacy")
    baseline = capture_identity_baseline(conn, 12)
    observation = DialogIdentityObservation(12, IDENTITY_OMITTED, None, IDENTITY_OMITTED, False, "profile", 500)
    assert publish_dialog_identity(conn, 12, observation, baseline)
    identity = read_dialog_identities(conn, [12])[12]
    assert (identity.name, identity.username, identity.dialog_type) == ("Known", None, DialogType.USER)
    assert identity.observed_at is None and not identity.complete and identity.source == "mixed"


@pytest.mark.parametrize("unknown_type", [None, DialogType.UNKNOWN, "unknown", "Unknown", "not-a-type"])
def test_partial_unknown_type_preserves_known_type(
    make_synced_db: Callable[[], sqlite3.Connection],
    unknown_type: DialogType | str | None,
) -> None:
    conn = make_synced_db()
    _dialog(conn, 121, name="Before", type="supergroup", username="before", identity_source="realtime")
    baseline = capture_identity_baseline(conn, 121)
    observation = DialogIdentityObservation(
        121, name="After", dialog_type=unknown_type, complete=False, source="profile", observed_at=20
    )
    assert publish_dialog_identity(conn, 121, observation, baseline)
    identity = read_dialog_identities(conn, [121])[121]
    assert identity.name == "After"
    assert identity.dialog_type is DialogType.SUPERGROUP
    assert identity.source == "mixed"


@pytest.mark.parametrize("unknown_type", [None, DialogType.UNKNOWN, "unknown", "Unknown", "not-a-type"])
def test_unknown_type_only_is_noop_without_revision_advance(
    make_synced_db: Callable[[], sqlite3.Connection],
    unknown_type: DialogType | str | None,
) -> None:
    conn = make_synced_db()
    _dialog(conn, 122, name="Known", type="user", identity_source="directory")
    baseline = capture_identity_baseline(conn, 122)
    observation = DialogIdentityObservation(122, dialog_type=unknown_type, source="profile", observed_at=21)
    assert not publish_dialog_identity(conn, 122, observation, baseline)
    assert conn.execute("SELECT type,identity_revision FROM dialogs WHERE dialog_id=122").fetchone() == ("user", 0)


@pytest.mark.parametrize("unknown_type", [None, DialogType.UNKNOWN, "unknown", "Unknown", "not-a-type"])
def test_complete_unknown_type_is_rejected(
    make_synced_db: Callable[[], sqlite3.Connection],
    unknown_type: DialogType | str | None,
) -> None:
    conn = make_synced_db()
    _dialog(conn, 123, name="Known", type="user")
    observation = DialogIdentityObservation(
        123, name="New", username=None, dialog_type=unknown_type, complete=True, source="directory", observed_at=22
    )
    with pytest.raises(ValueError, match="known dialog_type"):
        publish_dialog_identity(conn, 123, observation, capture_identity_baseline(conn, 123))


def test_profile_fallback_is_partial_and_never_uses_updated_at(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    conn = make_synced_db()
    _dialog(conn, 13, name=None, type="unknown", username=None)
    conn.execute("INSERT INTO entities(id,type,name,username,updated_at) VALUES (13,'user','Profile','p',456)")
    identity = read_dialog_identities(conn, [13])[13]
    assert (identity.name, identity.username, identity.source, identity.observed_at, identity.complete) == (
        "Profile",
        "p",
        "profile",
        None,
        False,
    )


def test_identity_reads_respect_sqlite_parameter_limit_without_per_id_queries(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    conn = make_synced_db()
    conn.executemany(
        "INSERT INTO dialogs(dialog_id,name,type) VALUES (?,?,'user')",
        [(dialog_id, f"Dialog {dialog_id}") for dialog_id in range(1000)],
    )
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    identities = read_dialog_identities(conn, range(1000))
    conn.set_trace_callback(None)
    dialog_reads = [statement for statement in statements if "FROM dialogs d LEFT JOIN entities" in statement]
    assert len(identities) == 1000
    assert len(dialog_reads) == 2


def test_nonexistent_dialog_is_not_created_and_invalid_ids_are_rejected(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    conn = make_synced_db()
    conn.execute("INSERT INTO entities(id,type,name,username,updated_at) VALUES (14,'user','Profile','p',456)")
    assert capture_identity_baseline(conn, 14) is None
    assert not publish_dialog_identity(
        conn, 14, DialogIdentityObservation(14, "New", None, "user", True, "profile", 1), 0
    )
    assert conn.execute("SELECT 1 FROM dialogs WHERE dialog_id=14").fetchone() is None
    with pytest.raises(TypeError):
        capture_identity_baseline(conn, True)
    with pytest.raises(ValueError):
        publish_dialog_identity(conn, 99, DialogIdentityObservation(14, name="x"), 0)


def test_owner_joins_caller_transaction_and_presence_revision_is_independent(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    conn = make_synced_db()
    _dialog(conn, 15, name="A", type="user", username="a")
    base = capture_identity_baseline(conn, 15)
    conn.commit()
    conn.execute("BEGIN")
    assert publish_dialog_identity(conn, 15, DialogIdentityObservation(15, "B", "b", "user", True, "profile", 8), base)
    conn.execute("UPDATE dialogs SET hidden=1 WHERE dialog_id=15")
    assert conn.execute("SELECT identity_revision,revision FROM dialogs WHERE dialog_id=15").fetchone() == (1, 1)
    conn.rollback()
    assert conn.execute("SELECT name,identity_revision,revision FROM dialogs WHERE dialog_id=15").fetchone() == (
        "A",
        0,
        0,
    )
