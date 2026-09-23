from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

from telethon.tl import types

from mcp_telegram.channel_full_siblings import (
    capture_channel_full_siblings_token,
    write_channel_full_siblings,
)
from mcp_telegram.entity_profile.repository import EntityProfileRepository, EntitySectionCommit
from mcp_telegram.sync_db import _apply_migrations


def _db(entity_id: int = 42) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _apply_migrations(conn)
    conn.execute("INSERT INTO entities(id,type,name,updated_at) VALUES(?,'channel','Channel',100)", (entity_id,))
    conn.execute(
        "INSERT INTO entity_details(entity_id,detail_json,fetched_at,profile_revision) "
        'VALUES(?,\'{"retained":"value"}\',100,0)',
        (entity_id,),
    )
    conn.commit()
    return conn


def test_channel_full_siblings_merge_without_linkage_and_reject_stale_profile_token() -> None:
    conn = _db()
    try:
        token = capture_channel_full_siblings_token(conn, 42)
        result = SimpleNamespace(
            full_chat=SimpleNamespace(participants_count=12, pinned_msg_id=7, about="Observed"),
            chats=[],
        )
        assert write_channel_full_siblings(conn, 42, result, token, observed_at=200)
        row = cast(
            tuple[str, int] | None,
            conn.execute("SELECT detail_json, profile_revision FROM entity_details WHERE entity_id=42").fetchone(),
        )
        assert row is not None
        detail = cast(object, json.loads(row[0]))
        assert isinstance(detail, dict)
        assert detail == {
            "retained": "value",
            "subscribers_count": 12,
            "pinned_msg_id": 7,
            "about": "Observed",
        }
        assert "linked_chat_id" not in detail
        assert row[1] == 1

        stale_token = capture_channel_full_siblings_token(conn, 42)
        conn.execute(
            "UPDATE entity_details SET detail_json=?, profile_revision=profile_revision+1 WHERE entity_id=42",
            ('{"newer":true}',),
        )
        assert not write_channel_full_siblings(conn, 42, result, stale_token, observed_at=300)
        assert cast(
            tuple[str] | None,
            conn.execute("SELECT detail_json FROM entity_details WHERE entity_id=42").fetchone(),
        ) == ('{"newer":true}',)
    finally:
        conn.close()


def test_channel_identity_partial_and_blank_observations_preserve_stored_identity() -> None:
    channel_id = -1_000_000_000_042
    conn = _db(channel_id)
    try:
        conn.execute(
            "UPDATE entities SET username='canonical_name', name_normalized='channel' WHERE id=?", (channel_id,)
        )
        token = capture_channel_full_siblings_token(conn, channel_id)
        result = SimpleNamespace(
            full_chat=SimpleNamespace(participants_count=None, pinned_msg_id=None, about=None),
            chats=[
                types.Channel(
                    id=42,
                    title="   ",
                    photo=types.ChatPhotoEmpty(),
                    date=datetime(2026, 1, 1, tzinfo=UTC),
                    username=None,
                )
            ],
        )

        assert write_channel_full_siblings(conn, channel_id, result, token, observed_at=200)
        identity = cast(
            tuple[object, object, object] | None,
            conn.execute("SELECT name, username, name_normalized FROM entities WHERE id=?", (channel_id,)).fetchone(),
        )
        assert identity == ("Channel", "canonical_name", "channel")
        assert conn.execute("SELECT detail_json FROM entity_details WHERE entity_id=?", (channel_id,)).fetchone() == (
            '{"retained":"value"}',
        )
    finally:
        conn.close()


def test_channel_identity_observation_creates_missing_entity_and_normalizes_name() -> None:
    channel_id = -1_000_000_000_042
    conn = _db(channel_id)
    try:
        conn.execute("DELETE FROM entities WHERE id=?", (channel_id,))
        token = capture_channel_full_siblings_token(conn, channel_id)
        result = SimpleNamespace(
            full_chat=SimpleNamespace(participants_count=None, pinned_msg_id=None, about=None),
            chats=[
                types.Channel(
                    id=42,
                    title="Новый канал",
                    photo=types.ChatPhotoEmpty(),
                    date=datetime(2026, 1, 1, tzinfo=UTC),
                    username="new_channel",
                )
            ],
        )

        assert write_channel_full_siblings(conn, channel_id, result, token, observed_at=200)
        assert conn.execute(
            "SELECT type, name, username, name_normalized FROM entities WHERE id=?", (channel_id,)
        ).fetchone() == (
            "channel",
            "Новый канал",
            "new_channel",
            "novyy kanal",
        )
    finally:
        conn.close()


def test_uncached_channel_creates_parent_before_detail_with_foreign_keys_enabled() -> None:
    conn = sqlite3.connect(":memory:")
    _apply_migrations(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    channel_id = -1_000_000_000_042
    token = capture_channel_full_siblings_token(conn, channel_id)
    result = SimpleNamespace(
        full_chat=SimpleNamespace(participants_count=12, pinned_msg_id=None, about="About"),
        chats=[
            types.Channel(
                id=42,
                title="Uncached channel",
                photo=types.ChatPhotoEmpty(),
                date=datetime(2026, 1, 1, tzinfo=UTC),
                username="uncached_channel",
            )
        ],
    )

    with conn:
        assert write_channel_full_siblings(conn, channel_id, result, token, observed_at=200)

    assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert conn.execute("SELECT type, name FROM entities WHERE id=?", (channel_id,)).fetchone() == (
        "channel",
        "Uncached channel",
    )
    assert conn.execute("SELECT profile_revision FROM entity_details WHERE entity_id=?", (channel_id,)).fetchone() == (
        1,
    )
    conn.close()


def test_core_only_revision_seeds_first_detail_and_allows_successive_profile_commits() -> None:
    conn = sqlite3.connect(":memory:")
    _apply_migrations(conn)
    channel_id = -1_000_000_000_042
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO entities(id,type,name,updated_at) VALUES(?,'channel','Core only',100)",
        (channel_id,),
    )
    conn.execute(
        "INSERT INTO entity_profile_refresh_state(entity_id,status,retry_at,reason,updated_at,next_section,"
        "acquisition_cursor,generation,profile_revision) VALUES(?,'pending',NULL,'refresh_in_progress',100,"
        "'common_chats',1,0,2)",
        (channel_id,),
    )
    conn.commit()
    profiles = EntityProfileRepository(conn, section_ttl_seconds=300)
    cursor = profiles.next_due_refresh(now=100)
    assert cursor is not None and cursor.profile_revision == 2

    token = capture_channel_full_siblings_token(conn, channel_id)
    assert token.profile_revision == 2
    result = SimpleNamespace(
        full_chat=SimpleNamespace(participants_count=12, pinned_msg_id=None, about="About"),
        chats=[],
    )
    with conn:
        assert write_channel_full_siblings(conn, channel_id, result, token, observed_at=101)
    assert conn.execute("SELECT profile_revision FROM entity_details WHERE entity_id=?", (channel_id,)).fetchone() == (
        3,
    )
    assert conn.execute(
        "SELECT profile_revision FROM entity_profile_refresh_state WHERE entity_id=?", (channel_id,)
    ).fetchone() == (3,)

    for now, next_revision in ((102, 4), (103, 5)):
        cursor = profiles.next_due_refresh(now=now)
        assert cursor is not None and cursor.profile_revision == next_revision - 1
        assert profiles.commit_section(cursor, EntitySectionCommit({"about": f"Profile {now}"}), now=now)
        assert conn.execute(
            "SELECT profile_revision FROM entity_details WHERE entity_id=?", (channel_id,)
        ).fetchone() == (next_revision,)
    conn.close()
