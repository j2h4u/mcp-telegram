from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from mcp_telegram.models import DialogType
from mcp_telegram.own_only import (
    OwnOnlyBasis,
    OwnOnlyContext,
    classify_own_only_dialog,
    enroll_own_only_sync_dialog,
    query_own_only_candidates,
)
from tests.history_enrollment_helpers import seed_full_history_enrollment


def _context() -> OwnOnlyContext:
    return OwnOnlyContext(
        account_id=42,
        personal_channel_id=9001,
        personal_channel_linked_chat_id=8001,
    )


def test_peer_id_zero_returns_zero() -> None:
    """_peer_id(0) returns 0 — the <= 0 branch preserves the value without overflow."""
    ctx = OwnOnlyContext(account_id=1, personal_channel_id=0)
    assert ctx.personal_channel_id == 0


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
        conn.execute("CREATE TABLE synced_dialogs (dialog_id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute(
            """CREATE TABLE full_history_enrollment (
                dialog_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                source TEXT NOT NULL CHECK(source IN ('explicit', 'automatic', 'migration')),
                updated_at INTEGER NOT NULL
            ) WITHOUT ROWID"""
        )
        conn.executemany(
            "INSERT INTO dialogs VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, "dm", "user", None, 10, 0),
                (-1000000009001, "channel", "channel", -1000000008001, 20, 0),
                (-1000000008001, "discussion", "forum", None, 30, 0),
                (-1000000007001, "other", "supergroup", None, 40, 0),
                (-1000000006001, "lost", "channel", None, 50, 1),
            ],
        )
        conn.execute(
            "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'access_lost')",
            (-1000000006001,),
        )
        seed_full_history_enrollment(conn, -1000000006001, enabled=False)
        assert [row["dialog_id"] for row in query_own_only_candidates(conn, personal_channel_id=9001)] == [
            -1000000009001,
            -1000000008001,
            1,
        ]
    finally:
        conn.close()


def test_sync_enrollment_respects_outer_transaction_rollback() -> None:
    """Enrollment must roll back with the caller's message batch transaction."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE synced_dialogs (dialog_id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute("CREATE TABLE messages (message_id INTEGER PRIMARY KEY, media_kind TEXT)")

        conn.execute("BEGIN")
        try:
            conn.execute("INSERT INTO messages (message_id) VALUES (7)")
            enroll_own_only_sync_dialog(conn, 42)
            raise RuntimeError("batch failed")
        except RuntimeError as exc:
            assert str(exc) == "batch failed"
            conn.rollback()

        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM synced_dialogs").fetchone() == (0,)
    finally:
        conn.close()
