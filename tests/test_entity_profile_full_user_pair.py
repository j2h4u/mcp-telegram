"""Progressive integration checks for the opt-in FullUser pair."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest
from telethon.tl.types import User  # type: ignore[import-untyped]

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.daemon_entity_info import DaemonEntityInfoService, EntityInfoDeps
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter, RefreshLimits
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.telegram_demand import RpcAttemptBudget
from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope


def _fenced_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT, username TEXT,
            name_normalized TEXT, updated_at INTEGER NOT NULL
        );
        CREATE TABLE entity_details (
            entity_id INTEGER PRIMARY KEY, detail_json TEXT NOT NULL, fetched_at INTEGER NOT NULL,
            profile_revision INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE entity_detail_sections (
            entity_id INTEGER NOT NULL, section TEXT NOT NULL, status TEXT NOT NULL,
            observed_at INTEGER, reason TEXT, payload_json TEXT, retry_at INTEGER,
            acquisition_generation INTEGER, acquisition_outcome TEXT, provenance_json TEXT,
            normalization_version TEXT, observation_started_at INTEGER,
            observation_completed_at INTEGER, acquisition_identity_json TEXT,
            PRIMARY KEY(entity_id, section)
        );
        CREATE TABLE entity_profile_refresh_state (
            entity_id INTEGER PRIMARY KEY, status TEXT NOT NULL, retry_at INTEGER,
            reason TEXT, updated_at INTEGER NOT NULL, next_section TEXT NOT NULL,
            acquisition_cursor INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 0,
            started_at INTEGER, pair_eligible INTEGER NOT NULL DEFAULT 0,
            follow_up_required INTEGER NOT NULL DEFAULT 0, profile_revision INTEGER NOT NULL DEFAULT 0
        );
        """
    )


class _PairClient:
    def __init__(self, *, channel_id: int | None = 123, bot: bool = False, omit_channel_id: bool = False) -> None:
        self.channel_id = channel_id
        self.bot = bot
        self.omit_channel_id = omit_channel_id
        self.full_user_calls = 0

    async def __call__(self, request: object) -> object:
        scope = current_rpc_scope()
        assert scope.attempt_budget is not None
        scope.attempt_budget.debit()
        request_name = cast(tuple[str, object], request)[0]
        if request_name != "full_user":
            raise AssertionError(request_name)
        self.full_user_calls += 1
        user = User(id=42, first_name="Target", username="target", bot=self.bot)
        full_user_data = {"about": "about", "personal_channel_message": 9}
        if not self.omit_channel_id:
            full_user_data["personal_channel_id"] = self.channel_id
        full_user = SimpleNamespace(**full_user_data)
        return SimpleNamespace(
            full_user=full_user,
            users=[user],
            chats=[SimpleNamespace(id=123, title="Local Channel", username="local_channel")],
        )

    async def get_entity(self, entity_id: int) -> object:
        del entity_id
        raise AssertionError("the stored core must avoid resolution")

    async def get_messages(self, entity: object, ids: list[int]) -> object:
        del entity
        raise AssertionError(ids)

    def iter_participants(self, peer: object, limit: int = 0) -> AsyncIterator[object]:
        del peer
        raise AssertionError(limit)

    def iter_dialogs(self) -> AsyncIterator[object]:
        raise AssertionError("dialog traversal is not part of this test")


def _pair_service(conn: sqlite3.Connection, client: _PairClient, *, enabled: bool) -> object:
    def peer_id(value: object) -> int:
        raw_id = getattr(value, "id", None)
        if not isinstance(raw_id, int):
            raise TypeError("test peer has no integer id")
        return raw_id

    service = DaemonEntityInfoService(
        EntityInfoDeps(
            conn=conn,
            client=client,
            dm_peer_ids=lambda: set(),
            self_id=None,
            self_profile=None,
            get_peer_id=peer_id,
            rid=lambda: "",
            logger=logging.getLogger(__name__),
            now_provider=lambda: 100.0,
            detail_ttl_seconds=300,
            slow_stage_seconds=1.0,
            get_common_chats_request=lambda **kwargs: ("common_chats", kwargs),
            get_full_user_request=lambda **kwargs: ("full_user", kwargs),
            get_user_photos_request=lambda **kwargs: ("photos", kwargs),
            get_messages_search_request=lambda **kwargs: ("search", kwargs),
            get_full_channel_request=lambda **kwargs: ("full_channel", kwargs),
            get_participants_request=lambda **kwargs: ("participants", kwargs),
            channel_participants_contacts_request=lambda **kwargs: ("contacts", kwargs),
            get_full_chat_request=lambda **kwargs: ("full_chat", kwargs),
            input_messages_filter_chat_photos=object,
            message_action_chat_edit_photo=object,
            chat_reactions_all=object,
            chat_reactions_some=object,
            chat_reactions_none=object,
            channel_type=object,
            chat_type=object,
            refresh_limits=RefreshLimits(),
            enable_full_user_pair=enabled,
            full_user_auth_scope=lambda: TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99),
        )
    )
    service.bind_demand_sink(MagicMock())
    service._deps = replace(  # type: ignore[attr-defined]
        service._deps,
        client=client,
    )
    return service


def _prepare(
    path: Path,
    *,
    entity_type: str = "user",
    bot: bool = False,
    migrated: bool = False,
) -> tuple[sqlite3.Connection, object]:
    if migrated:
        ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    if not migrated:
        _fenced_schema(conn)
    conn.execute("INSERT INTO entities VALUES (?, ?, 'Target', 'target', NULL, 100)", (42, entity_type))
    conn.execute(
        "INSERT INTO entities VALUES (?, 'channel', 'Local Channel', 'local_channel', NULL, 100)", (-1000000000123,)
    )
    conn.commit()
    service = _pair_service(conn, _PairClient(bot=bot), enabled=True)
    service._profiles.save_core({"id": 42, "type": entity_type, "name": "Target"}, now=100)  # type: ignore[attr-defined]
    service._profiles.mark_pending(42, now=100)  # type: ignore[attr-defined]
    return conn, service


@pytest.mark.asyncio
async def test_enabled_pair_commits_two_projections_with_one_full_user_call(tmp_path: Path) -> None:
    conn, service = _prepare(tmp_path / "pair.sqlite")
    client = cast(_PairClient, service._deps.client)  # type: ignore[attr-defined]
    coordinator = service.refresh_coordinator  # type: ignore[attr-defined]
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))

    assert client.full_user_calls == 1
    assert conn.execute("SELECT next_section FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() == (
        "common_chats",
    )
    full_status, channel_status = cast(
        tuple[str, str],
        conn.execute(
            "SELECT status, (SELECT status FROM entity_detail_sections WHERE section='personal_channel' AND entity_id=42) "
            "FROM entity_detail_sections WHERE entity_id=42 AND section='full_profile'"
        ).fetchone(),
    )
    assert (full_status, channel_status) == ("fresh", "fresh")
    evidence = service._profiles.read_section_evidence(42, "personal_channel")  # type: ignore[attr-defined]
    assert evidence is not None and evidence["outcome"] == "usable"
    detail = service._profiles.read(42, now=100)  # type: ignore[attr-defined]
    assert detail is not None
    assert detail.detail["personal_channel"]["metadata_source"] == "local_entities"
    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("entity_type", "bot"), (("user", False), ("bot", True)))
async def test_enabled_pair_applies_to_user_and_bot(tmp_path: Path, entity_type: str, bot: bool) -> None:
    conn, service = _prepare(tmp_path / f"pair-{entity_type}.sqlite", entity_type=entity_type, bot=bot)
    service._deps = replace(service._deps, client=_PairClient(bot=bot))  # type: ignore[attr-defined]
    coordinator = service.refresh_coordinator  # type: ignore[attr-defined]
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    assert conn.execute(
        "SELECT status FROM entity_detail_sections WHERE entity_id=42 AND section='full_profile'"
    ).fetchone() == ("fresh",)
    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()


@pytest.mark.asyncio
async def test_pair_personal_channel_completion_is_local_and_does_not_repeat_full_user(tmp_path: Path) -> None:
    conn, service = _prepare(tmp_path / "pair-local.sqlite")
    client = cast(_PairClient, service._deps.client)  # type: ignore[attr-defined]
    coordinator = service.refresh_coordinator  # type: ignore[attr-defined]
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    generation, revision = cast(
        tuple[int, int],
        conn.execute(
            "SELECT generation, profile_revision FROM entity_profile_refresh_state WHERE entity_id=42"
        ).fetchone(),
    )
    conn.execute(
        "UPDATE entity_profile_refresh_state SET next_section='personal_channel', acquisition_cursor=0 "
        "WHERE entity_id=42"
    )
    conn.commit()
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))

    assert client.full_user_calls == 1
    assert conn.execute("SELECT status FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() == (
        "complete",
    )
    assert conn.execute(
        "SELECT generation, profile_revision FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == (
        generation,
        revision,
    )
    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("entity_type", "bot"), (("user", False), ("bot", True)))
async def test_personal_channel_pair_guard_requires_pair_eligibility(
    tmp_path: Path, entity_type: str, bot: bool
) -> None:
    conn, service = _prepare(tmp_path / f"pair-ineligible-{entity_type}.sqlite", entity_type=entity_type, bot=bot)
    client = cast(_PairClient, service._deps.client)  # type: ignore[attr-defined]
    coordinator = service.refresh_coordinator  # type: ignore[attr-defined]
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    conn.execute(
        "UPDATE entity_profile_refresh_state SET next_section='personal_channel', acquisition_cursor=0, "
        "pair_eligible=0 WHERE entity_id=42"
    )
    conn.commit()

    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))

    assert client.full_user_calls == 2
    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()


@pytest.mark.asyncio
async def test_disabled_switch_keeps_legacy_two_full_user_acquisitions(tmp_path: Path) -> None:
    conn, service = _prepare(tmp_path / "pair-disabled.sqlite")
    service._deps = replace(service._deps, enable_full_user_pair=False)  # type: ignore[attr-defined]
    client = cast(_PairClient, service._deps.client)  # type: ignore[attr-defined]
    coordinator = service.refresh_coordinator  # type: ignore[attr-defined]
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    conn.execute(
        "UPDATE entity_profile_refresh_state SET next_section='personal_channel', acquisition_cursor=0 "
        "WHERE entity_id=42"
    )
    conn.commit()
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    assert client.full_user_calls == 2
    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()


@pytest.mark.asyncio
async def test_disabled_pair_measurement_survives_restart_at_section_boundary(tmp_path: Path) -> None:
    path = tmp_path / "pair-disabled-restart.sqlite"
    conn, raw_service = _prepare(path, migrated=True)
    service = cast(DaemonEntityInfoService, raw_service)
    service._deps = replace(service._deps, enable_full_user_pair=False)
    coordinator = service.refresh_coordinator
    assert coordinator is not None

    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    assert conn.execute(
        "SELECT pair_full_profile_outcome, pair_personal_channel_outcome, pair_attempts "
        "FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == ("usable", None, 1)

    conn.execute(
        "UPDATE entity_profile_refresh_state SET next_section='personal_channel', acquisition_cursor=0 "
        "WHERE entity_id=42"
    )
    conn.commit()
    await service.shutdown()
    conn.close()

    reopened = sqlite3.connect(path)
    observer_rows: list[dict[str, object]] = []

    class _Observer:
        def observe_profile_pair(self, **values: object) -> None:
            observer_rows.append(values)
            raise RuntimeError("telemetry failed")

    restarted = cast(DaemonEntityInfoService, _pair_service(reopened, _PairClient(), enabled=False))
    restarted.bind_profile_observer(_Observer())
    coordinator = restarted.refresh_coordinator
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))

    assert reopened.execute(
        "SELECT pair_full_profile_outcome, pair_personal_channel_outcome, pair_attempts, "
        "pair_measurement_complete, pair_summary_watermark FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == ("usable", "usable", 2, 1, 100)
    assert len(observer_rows) == 1
    assert observer_rows[0]["actual_attempts"] == 2
    assert observer_rows[0]["full_profile_outcome"] == "usable"
    assert observer_rows[0]["personal_channel_outcome"] == "usable"
    await restarted.shutdown()
    reopened.close()


@pytest.mark.asyncio
async def test_partial_channel_preserves_usable_profile_and_non_reusable_channel(tmp_path: Path) -> None:
    conn, service = _prepare(tmp_path / "pair-partial.sqlite")
    client = _PairClient(channel_id=None)
    service._deps = replace(service._deps, client=client)  # type: ignore[attr-defined]
    coordinator = service.refresh_coordinator  # type: ignore[attr-defined]
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))

    statuses = dict(conn.execute("SELECT section, status FROM entity_detail_sections WHERE entity_id=42").fetchall())
    assert statuses["full_profile"] == "fresh"
    assert statuses["personal_channel"] == "fresh"
    evidence = service._profiles.read_section_evidence(42, "personal_channel")  # type: ignore[attr-defined]
    assert evidence is not None and evidence["outcome"] == "absent"
    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()


@pytest.mark.asyncio
async def test_missing_channel_field_is_partial_while_full_profile_is_usable(tmp_path: Path) -> None:
    conn, service = _prepare(tmp_path / "pair-missing.sqlite")
    service._deps = replace(service._deps, client=_PairClient(omit_channel_id=True))  # type: ignore[attr-defined]
    coordinator = service.refresh_coordinator  # type: ignore[attr-defined]
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    profile_evidence = service._profiles.read_section_evidence(42, "full_profile")  # type: ignore[attr-defined]
    channel_evidence = service._profiles.read_section_evidence(42, "personal_channel")  # type: ignore[attr-defined]
    assert profile_evidence is not None and profile_evidence["outcome"] == "usable"
    assert channel_evidence is not None and channel_evidence["outcome"] == "partial"
    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()
