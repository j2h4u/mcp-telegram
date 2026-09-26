from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from telethon.tl import types

from mcp_telegram.daemon_entity_info import _fresh_entity_identity_observation
from mcp_telegram.dialog_identity import capture_identity_baseline, publish_dialog_identity, read_dialog_identities
from mcp_telegram.dialog_identity_contracts import IDENTITY_OMITTED, DialogIdentityObservation
from mcp_telegram.entity_profile.contracts import ProfileAcquisitionEvidence
from mcp_telegram.entity_profile.repository import EntityProfileRepository, EntitySectionCommit
from mcp_telegram.models import DialogType
from mcp_telegram.sync_db import ensure_sync_schema


def _database(path: Path) -> tuple[sqlite3.Connection, EntityProfileRepository]:
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO entities(id,type,name,username,updated_at) VALUES (42,'user','Old','old',1)")
    conn.execute("INSERT INTO dialogs(dialog_id,name,type,username) VALUES (42,'Old','user','old')")
    conn.commit()
    return conn, EntityProfileRepository(conn, section_ttl_seconds=10)


def _complete(dialog_id: int, name: str, username: str | None, *, at: int) -> DialogIdentityObservation:
    return DialogIdentityObservation(dialog_id, name, username, DialogType.USER, True, "profile", at)


def test_fresh_core_profile_is_visible_without_changing_presence(tmp_path: Path) -> None:
    conn, repo = _database(tmp_path / "fresh.sqlite")
    baseline = capture_identity_baseline(conn, 42)
    repo.save_core(
        {"id": 42, "type": "user", "name": "Fresh", "username": "fresh"},
        now=20,
        dialog_identity_observation=_complete(42, "Fresh", "fresh", at=18),
        dialog_identity_baseline_revision=baseline,
    )
    identity = read_dialog_identities(conn, [42])[42]
    assert (identity.name, identity.username, identity.observed_at, identity.source, identity.complete) == (
        "Fresh",
        "fresh",
        18,
        "profile",
        True,
    )
    assert conn.execute("SELECT identity_revision,revision,hidden FROM dialogs WHERE dialog_id=42").fetchone() == (
        1,
        0,
        0,
    )
    repo.save_core({"id": 42, "type": "user", "name": "Cached", "username": "cached"}, now=21)
    assert read_dialog_identities(conn, [42])[42].name == "Fresh"
    assert conn.execute("SELECT identity_revision FROM dialogs WHERE dialog_id=42").fetchone() == (1,)
    conn.close()


def test_stale_equal_time_profile_keeps_facts_but_loses_identity_cas(tmp_path: Path) -> None:
    conn, repo = _database(tmp_path / "stale.sqlite")
    baseline = capture_identity_baseline(conn, 42)
    assert baseline == 0
    assert publish_dialog_identity(
        conn,
        42,
        DialogIdentityObservation(42, "Realtime", "current", "user", True, "realtime", 18),
        baseline,
    )
    repo.save_core(
        {"id": 42, "type": "user", "name": "Stale", "username": "stale"},
        now=20,
        dialog_identity_observation=_complete(42, "Stale", "stale", at=18),
        dialog_identity_baseline_revision=baseline,
    )
    assert read_dialog_identities(conn, [42])[42].name == "Realtime"
    assert conn.execute("SELECT detail_json FROM entity_details WHERE entity_id=42").fetchone() is None
    assert conn.execute(
        "SELECT name,username,identity_revision,revision FROM dialogs WHERE dialog_id=42"
    ).fetchone() == (
        "Realtime",
        "current",
        1,
        0,
    )
    assert conn.execute("SELECT name,username FROM entities WHERE id=42").fetchone() == ("Stale", "stale")
    conn.close()


def test_one_paired_observation_publishes_once_and_rolls_back_with_profile(tmp_path: Path) -> None:
    conn, repo = _database(tmp_path / "pair.sqlite")
    repo.mark_pending(42, now=10, pair_mode_override="enabled")
    cursor = repo.next_due_refresh(now=10)
    assert cursor is not None
    baseline = capture_identity_baseline(conn, 42)
    conn.execute(
        "CREATE TRIGGER fail_second_profile_section BEFORE INSERT ON entity_detail_sections "
        "WHEN NEW.section='personal_channel' BEGIN SELECT RAISE(ABORT,'rollback pair'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="rollback pair"):
        repo.commit_full_user_pair(
            cursor,
            EntitySectionCommit(
                {"about": "fresh"},
                evidence=ProfileAcquisitionEvidence(cursor.generation, "usable", observation_started_at=18),
                identity_patch={"name": "Fresh", "username": None, "type": "user"},
                dialog_identity_observation=_complete(42, "Fresh", None, at=18),
                dialog_identity_baseline_revision=baseline,
            ),
            EntitySectionCommit({"personal_channel": None}, status="unavailable", reason="absent"),
            now=20,
        )
    assert conn.execute(
        "SELECT name,username,identity_revision,revision FROM dialogs WHERE dialog_id=42"
    ).fetchone() == (
        "Old",
        "old",
        0,
        0,
    )
    assert conn.execute("SELECT COUNT(*) FROM entity_details WHERE entity_id=42").fetchone() == (0,)
    assert conn.execute("SELECT next_section FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() == (
        "full_profile",
    )
    conn.execute("DROP TRIGGER fail_second_profile_section")
    assert repo.commit_full_user_pair(
        cursor,
        EntitySectionCommit(
            {"about": "fresh"},
            evidence=ProfileAcquisitionEvidence(cursor.generation, "usable", observation_started_at=18),
            identity_patch={"name": "Fresh", "username": None, "type": "user"},
            dialog_identity_observation=_complete(42, "Fresh", None, at=18),
            dialog_identity_baseline_revision=baseline,
        ),
        EntitySectionCommit({"personal_channel": None}, status="unavailable", reason="absent"),
        now=20,
    )
    assert conn.execute(
        "SELECT name,username,identity_revision,revision FROM dialogs WHERE dialog_id=42"
    ).fetchone() == (
        "Fresh",
        None,
        1,
        0,
    )
    conn.close()


def test_partial_clear_preserves_omitted_and_profile_lookup_never_creates_membership(tmp_path: Path) -> None:
    conn, repo = _database(tmp_path / "partial.sqlite")
    baseline = capture_identity_baseline(conn, 42)
    partial = DialogIdentityObservation(42, IDENTITY_OMITTED, None, IDENTITY_OMITTED, False, "profile", 18)
    repo.save_core(
        {"id": 42, "type": "user", "name": "Old", "username": "old"},
        now=20,
        dialog_identity_observation=partial,
        dialog_identity_baseline_revision=baseline,
    )
    identity = read_dialog_identities(conn, [42])[42]
    assert (identity.name, identity.username, identity.dialog_type, identity.complete, identity.source) == (
        "Old",
        None,
        DialogType.USER,
        False,
        "mixed",
    )
    repo.save_core({"id": 43, "type": "user", "name": "Self", "username": "self"}, now=20)
    assert conn.execute("SELECT 1 FROM dialogs WHERE dialog_id=43").fetchone() is None
    assert conn.execute("SELECT identity_revision FROM dialogs WHERE dialog_id=42").fetchone() == (1,)
    conn.close()


def test_fresh_entity_identity_uses_collectible_username_and_omits_min_and_unknown_type(
    tmp_path: Path,
) -> None:
    conn, _repo = _database(tmp_path / "raw-identity.sqlite")
    baseline = capture_identity_baseline(conn, 42)
    channel = types.Channel(
        id=42,
        title="Fresh channel",
        photo=types.ChatPhotoEmpty(),
        date=None,
        broadcast=True,
        username=None,
        usernames=[types.Username("collectible", active=True)],
    )
    observed = _fresh_entity_identity_observation(channel, 42, observed_at=18)
    assert (observed.name, observed.username, observed.dialog_type, observed.complete) == (
        "Fresh channel",
        "collectible",
        DialogType.CHANNEL,
        True,
    )
    cleared_channel = types.Channel(
        id=42,
        title="Fresh channel",
        photo=types.ChatPhotoEmpty(),
        date=None,
        broadcast=True,
        username=None,
        usernames=[],
    )
    cleared_observation = _fresh_entity_identity_observation(cleared_channel, 42, observed_at=18)
    assert cleared_observation.username is None and cleared_observation.complete

    min_user = types.User(id=42, min=True, first_name="", last_name="")
    assert _fresh_entity_identity_observation(min_user, 42, observed_at=19) is None

    min_channel = types.Channel(
        id=42,
        title="",
        photo=types.ChatPhotoEmpty(),
        date=None,
        megagroup=False,
        min=True,
        username=None,
    )
    min_observation = _fresh_entity_identity_observation(min_channel, 42, observed_at=19)
    assert min_observation is None
    assert (read_dialog_identities(conn, [42])[42].name, read_dialog_identities(conn, [42])[42].dialog_type) == (
        "Old",
        DialogType.USER,
    )

    positive_min_channel = types.Channel(
        id=42,
        title="Partial channel",
        photo=types.ChatPhotoEmpty(),
        date=None,
        megagroup=True,
        min=True,
        username=None,
    )
    min_observation = _fresh_entity_identity_observation(positive_min_channel, 42, observed_at=19)
    assert min_observation is not None
    assert (
        min_observation.name,
        min_observation.username,
        min_observation.dialog_type,
        min_observation.complete,
    ) == ("Partial channel", IDENTITY_OMITTED, IDENTITY_OMITTED, False)
    assert publish_dialog_identity(conn, 42, min_observation, baseline)
    assert (
        read_dialog_identities(conn, [42])[42].name,
        read_dialog_identities(conn, [42])[42].username,
        read_dialog_identities(conn, [42])[42].dialog_type,
    ) == (
        "Partial channel",
        "old",
        DialogType.USER,
    )

    forbidden = types.ChannelForbidden(id=42, access_hash=9, title="")
    forbidden_observation = _fresh_entity_identity_observation(forbidden, 42, observed_at=20)
    assert forbidden_observation is None
    assert (
        read_dialog_identities(conn, [42])[42].name,
        read_dialog_identities(conn, [42])[42].username,
        read_dialog_identities(conn, [42])[42].dialog_type,
    ) == ("Partial channel", "old", DialogType.USER)
    conn.close()
