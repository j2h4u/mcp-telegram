from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from mcp_telegram.models import DialogType
from mcp_telegram.own_only import (
    OwnOnlyBasis,
    OwnOnlyClassification,
    OwnOnlyContext,
    classify_own_only_dialog,
    enroll_own_only_dialog,
    query_own_only_candidates,
)


def _context() -> OwnOnlyContext:
    return OwnOnlyContext(
        account_id=42,
        personal_channel_id=9001,
        personal_channel_linked_chat_id=8001,
    )


def test_direct_message_has_machine_readable_basis() -> None:
    result = classify_own_only_dialog(
        dialog_id=7,
        dialog_type=DialogType.USER,
        entity=SimpleNamespace(),
        context=_context(),
    )
    assert result.included is True
    assert result.basis == (OwnOnlyBasis.DIRECT_MESSAGE,)
    assert result.inclusion_basis == ("direct_message",)


def test_self_user_is_not_a_dm() -> None:
    result = classify_own_only_dialog(
        dialog_id=42,
        dialog_type="user",
        entity=SimpleNamespace(),
        context=_context(),
    )
    assert result.included is False


def test_personal_and_admin_owned_channels_are_included() -> None:
    personal = classify_own_only_dialog(
        dialog_id=-1000000009001,
        dialog_type="channel",
        entity=SimpleNamespace(creator=False, admin_rights=None),
        context=_context(),
    )
    owned = classify_own_only_dialog(
        dialog_id=-1000000009002,
        dialog_type="channel",
        entity=SimpleNamespace(creator=False, admin_rights=SimpleNamespace(post_messages=True)),
        context=_context(),
    )
    assert personal.basis == (OwnOnlyBasis.PERSONAL_CHANNEL,)
    assert owned.basis == (OwnOnlyBasis.OWNED_CHANNEL,)


def test_only_personal_channel_discussion_is_included() -> None:
    result = classify_own_only_dialog(
        dialog_id=-1000000008001,
        dialog_type="forum",
        entity=SimpleNamespace(creator=False, admin_rights=None),
        context=_context(),
    )
    unrelated = classify_own_only_dialog(
        dialog_id=-1000000008002,
        dialog_type="supergroup",
        entity=SimpleNamespace(creator=True, admin_rights=None),
        context=_context(),
    )
    assert result.basis == (OwnOnlyBasis.PERSONAL_CHANNEL_DISCUSSION,)
    assert unrelated.included is False


def test_candidate_query_keeps_rights_classification_out_of_sql() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE TABLE dialogs (dialog_id INTEGER, name TEXT, type TEXT, linked_chat_id INTEGER, last_message_at INTEGER, hidden INTEGER)"
        )
        conn.executemany(
            "INSERT INTO dialogs VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, "dm", "user", None, 10, 0),
                (-1000000009001, "channel", "channel", -1000000008001, 20, 0),
                (-1000000008001, "discussion", "forum", None, 30, 0),
                (-1000000007001, "other", "supergroup", None, 40, 0),
            ],
        )
        assert [row["dialog_id"] for row in query_own_only_candidates(conn, personal_channel_id=9001)] == [
            -1000000009001,
            -1000000008001,
            1,
        ]
    finally:
        conn.close()


def test_enroll_own_only_dialog_noop_when_not_included() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE TABLE own_only_dialogs (dialog_id INTEGER PRIMARY KEY, inclusion_basis TEXT, updated_at INTEGER)"
        )
        conn.execute(
            "CREATE TABLE synced_dialogs (dialog_id INTEGER, status TEXT, "
            "linked_chat_id INTEGER, linked_chat_resolved_at INTEGER)"
        )
        classification = OwnOnlyClassification(included=False, basis=())
        enroll_own_only_dialog(conn, 42, classification, now=100)

        assert conn.execute("SELECT COUNT(*) FROM own_only_dialogs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM synced_dialogs").fetchone()[0] == 0
    finally:
        conn.close()


def test_enroll_own_only_dialog_persists_when_included() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE TABLE own_only_dialogs (dialog_id INTEGER PRIMARY KEY, inclusion_basis TEXT, updated_at INTEGER)"
        )
        conn.execute(
            "CREATE TABLE synced_dialogs (dialog_id INTEGER, status TEXT, "
            "linked_chat_id INTEGER, linked_chat_resolved_at INTEGER)"
        )
        classification = OwnOnlyClassification(included=True, basis=(OwnOnlyBasis.DIRECT_MESSAGE,))
        enroll_own_only_dialog(conn, 42, classification, now=100)

        assert conn.execute("SELECT dialog_id FROM own_only_dialogs WHERE dialog_id=42").fetchone() == (42,)
        assert conn.execute("SELECT dialog_id, status FROM synced_dialogs WHERE dialog_id=42").fetchone() == (
            42,
            "own_only",
        )
    finally:
        conn.close()
