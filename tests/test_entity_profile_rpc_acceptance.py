"""Acceptance checks for the enabled FullUser pair boundary."""

from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest
from jsonschema import validate  # type: ignore[import-untyped]

from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.full_user_normalization import (
    ProjectionStatus,
    TargetKind,
    normalize_full_user_response,
)
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
from mcp_telegram.entity_profile.repository import EntityProfileRepository
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.models import DialogType
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.telegram_demand import RpcAttemptBudget, demand_context
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope
from mcp_telegram.tools.entity_info import (
    GET_ENTITY_INFO_OUTPUT_SCHEMA,
    GetEntityInfo,
    _entity_structured_content,
    get_entity_info,
)
from tests.test_entity_profile_full_user_pair import _PairClient, _prepare


class _FailureClient(_PairClient):
    def __init__(self, *, failure: BaseException | None = None, response: object | None = None) -> None:
        super().__init__()
        self.failure = failure
        self.response = response

    async def __call__(self, request: object) -> object:
        scope = current_rpc_scope()
        assert scope.attempt_budget is not None
        scope.attempt_budget.debit()
        if self.failure is not None:
            raise self.failure
        return self.response


class _CompletePairClient(_FailureClient):
    def __init__(self) -> None:
        user = SimpleNamespace(
            id=42,
            first_name="Target",
            last_name=None,
            username="target",
            usernames=None,
            emoji_status=None,
            status=None,
            restriction_reason=None,
            phone=None,
            lang_code=None,
            contact=False,
            mutual_contact=False,
            close_friend=False,
            send_paid_messages_stars=None,
            verified=False,
            premium=False,
            bot=False,
            scam=False,
            fake=False,
            restricted=False,
        )
        full_user = SimpleNamespace(
            about="about",
            blocked=False,
            ttl_period=None,
            private_forward_name=None,
            folder_id=None,
            birthday=None,
            bot_info=None,
            business_location=None,
            business_intro=None,
            business_work_hours=None,
            note=None,
            personal_channel_id=123,
            personal_channel_message=9,
        )
        super().__init__(
            response=SimpleNamespace(
                full_user=full_user, users=[user], chats=[SimpleNamespace(id=123, title="Channel", username="channel")]
            )
        )


def _requeue_new_generation(conn: sqlite3.Connection) -> None:
    revision = cast(
        tuple[int], conn.execute("SELECT profile_revision FROM entity_details WHERE entity_id=42").fetchone()
    )[0]
    conn.execute(
        """
        UPDATE entity_profile_refresh_state
        SET status='pending', retry_at=NULL, reason='refresh_queued', next_section='full_profile',
            acquisition_cursor=0, generation=generation+1, started_at=200, pair_eligible=1,
            pair_mode='enabled', profile_revision=?, follow_up_required=0,
            pair_full_profile_outcome=NULL, pair_personal_channel_outcome=NULL,
            pair_ready_at=NULL, pair_readiness_latency_ms=NULL, pair_summary_watermark=NULL,
            pair_attempts=0, pair_retries=0, pair_full_profile_attempts=0,
            pair_personal_channel_attempts=0, pair_full_profile_retries=0,
            pair_personal_channel_retries=0, pair_measurement_complete=0
        WHERE entity_id=42
        """,
        (revision,),
    )
    conn.commit()


async def _baseline_then_fail(path: Path, client: _FailureClient) -> tuple[sqlite3.Connection, DaemonEntityInfoService]:
    conn, raw_service = _prepare(path, migrated=True)
    service = cast(DaemonEntityInfoService, raw_service)
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    baseline = service._profiles.read(42, now=100)  # type: ignore[attr-defined]
    assert baseline is not None and baseline.detail["about"] == "about"
    _requeue_new_generation(conn)
    service._deps = replace(service._deps, client=client)
    return conn, service


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "reason", "retry_at"),
    (
        (TimeoutError("timeout"), "timeouterror", 160),
        (TelegramRpcThrottled(retry_after_seconds=7), "flood_wait", 107),
        (ConnectionError("transport"), "connectionerror", 160),
    ),
)
async def test_enabled_pair_failures_preserve_prior_data_and_account_attempts(
    tmp_path: Path, failure: BaseException, reason: str, retry_at: int
) -> None:
    conn, service = await _baseline_then_fail(tmp_path / f"{reason}.sqlite", _FailureClient(failure=failure))
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))

    assert conn.execute(
        "SELECT status, retry_at, reason, next_section, pair_attempts, pair_retries "
        "FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == ("failed", retry_at, reason, "full_profile", 1, 0)
    assert conn.execute(
        "SELECT status FROM entity_detail_sections WHERE entity_id=42 AND section='full_profile'"
    ).fetchone() == ("stale",)
    assert conn.execute(
        "SELECT acquisition_generation, acquisition_outcome FROM entity_detail_sections "
        "WHERE entity_id=42 AND section='full_profile'"
    ).fetchone() == (1, "usable")
    assert conn.execute(
        "SELECT pair_full_profile_outcome, pair_personal_channel_outcome, pair_measurement_complete "
        "FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == (None, None, 0)
    detail = service._profiles.read(42, now=200)  # type: ignore[attr-defined]
    assert detail is not None and detail.detail["about"] == "about"
    await service.shutdown()
    conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    (
        object(),
        SimpleNamespace(full_user=SimpleNamespace(about="new"), users=None),
        SimpleNamespace(
            full_user=SimpleNamespace(about="new", personal_channel_id=None),
            users=[SimpleNamespace(id=43, first_name="Other", bot=False)],
        ),
    ),
)
async def test_invalid_full_user_envelopes_do_not_create_positive_receipts(tmp_path: Path, response: object) -> None:
    conn, service = await _baseline_then_fail(
        tmp_path / f"{type(response).__name__}.sqlite", _FailureClient(response=response)
    )
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))

    assert conn.execute(
        "SELECT status, reason, next_section, pair_attempts FROM entity_profile_refresh_state WHERE entity_id=42"
    ).fetchone() == ("failed", "valueerror", "full_profile", 1)
    assert conn.execute(
        "SELECT acquisition_generation, acquisition_outcome FROM entity_detail_sections "
        "WHERE entity_id=42 AND section='full_profile'"
    ).fetchone() == (1, "usable")
    assert service._profiles.read(42, now=200).detail["about"] == "about"  # type: ignore[union-attr]
    await service.shutdown()
    conn.close()


def test_normalization_rejects_target_mismatch_and_pair_is_only_user_or_bot() -> None:
    response = SimpleNamespace(
        full_user=SimpleNamespace(about="about", personal_channel_id=None),
        users=[SimpleNamespace(id=42, first_name="Target", bot=False)],
    )
    valid = normalize_full_user_response(response, target_id=42, target_kind=TargetKind.USER)
    assert valid.full_profile.status is ProjectionStatus.USABLE
    for target_id, kind in ((43, TargetKind.USER), (42, TargetKind.BOT)):
        invalid = normalize_full_user_response(response, target_id=target_id, target_kind=kind)
        assert invalid.full_profile.status is ProjectionStatus.UNAVAILABLE
        assert invalid.personal_channel.status is ProjectionStatus.UNAVAILABLE
    assert DaemonEntityInfoService._section_applies(DialogType.USER, "personal_channel")  # type: ignore[attr-defined]
    assert DaemonEntityInfoService._section_applies(DialogType.BOT, "personal_channel")  # type: ignore[attr-defined]
    assert not DaemonEntityInfoService._section_applies(DialogType.SERVICE, "personal_channel")  # type: ignore[attr-defined]
    assert not DaemonEntityInfoService._section_applies(DialogType.CHANNEL, "personal_channel")  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize("now", (109, 110, 111))
async def test_file_backed_pair_receipt_ttl_is_strict_across_reopen(tmp_path: Path, now: int) -> None:
    path = tmp_path / "ttl.sqlite"
    ensure_sync_schema(path)
    conn, raw_service = _prepare(path, migrated=True)
    service = cast(DaemonEntityInfoService, raw_service)
    service._deps = replace(service._deps, client=_CompletePairClient())
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    evidence = service._profiles.read_section_evidence(42, "full_profile")  # type: ignore[attr-defined]
    assert evidence is not None and isinstance(evidence.get("identity"), dict)
    identity = cast(dict[str, object], evidence["identity"])
    await service.shutdown()
    conn.close()

    reopened = sqlite3.connect(path)
    repo = EntityProfileRepository(reopened, section_ttl_seconds=10)
    assert repo.section_is_reusable(42, "full_profile", identity=identity, now=now) is (now < 110)
    reopened.close()


def _golden_data(entity_type: str) -> dict[str, object]:
    is_bot = entity_type == "bot"
    return {
        "id": 42,
        "type": entity_type,
        "name": "Target Bot" if is_bot else "Target User",
        "username": "target_bot" if is_bot else "target_user",
        "about": "A profile",
        "first_name": "Target",
        "last_name": "Bot" if is_bot else "User",
        "extra_usernames": [],
        "emoji_status_id": None,
        "status": {"type": "online"},
        "phone": None,
        "lang_code": "en",
        "contact": False,
        "mutual_contact": False,
        "close_friend": False,
        "blocked": False,
        "verified": True,
        "premium": is_bot,
        "bot": is_bot,
        "scam": False,
        "fake": False,
        "restricted": False,
        "restriction_reason": [],
        "my_membership": {"is_member": False},
        "bot_info": {"description": "Does useful work", "commands": []} if is_bot else None,
        "business_intro": None,
        "business_location": None,
        "business_work_hours": None,
        "note": None,
        "folder_id": None,
        "folder_name": None,
        "ttl_period": None,
        "private_forward_name": None,
        "birthday": None,
        "personal_channel_id": None,
        "common_chats": [],
        "dialog_placement": {"archived": False, "folders": []},
    }


@pytest.mark.parametrize("entity_type", ("user", "bot"))
def test_progressive_user_and_bot_projection_matches_legacy_public_golden(entity_type: str) -> None:
    legacy = _golden_data(entity_type)
    progressive = {
        **legacy,
        "completeness": "partial",
        "sections": {
            "full_profile": {"status": "fresh", "observed_at": 100},
            "personal_channel": {"status": "fresh", "observed_at": 100},
            "common_chats": {"status": "pending", "reason": "refresh_queued"},
        },
    }
    args = GetEntityInfo(entity="Target")
    display_name = cast(str, legacy["name"])
    old = _entity_structured_content(
        args=args, data=legacy, entity_id=42, display_name=display_name, resolution="resolver_match"
    )
    current = _entity_structured_content(
        args=args, data=progressive, entity_id=42, display_name=display_name, resolution="resolver_match"
    )
    assert {key: old[key] for key in old if key not in {"completeness", "sections"}} == {
        key: current[key] for key in current if key not in {"completeness", "sections"}
    }
    validate(instance=current, schema=GET_ENTITY_INFO_OUTPUT_SCHEMA)
    encoded = str(current)
    for internal in ("evidence", "auth_scope", "normalization_version", "acquisition_generation"):
        assert internal not in encoded


@pytest.mark.asyncio
@pytest.mark.parametrize(("entity_type", "bot"), (("user", False), ("bot", True)))
async def test_actual_entity_info_tool_renders_persisted_user_and_bot_profile(
    tmp_path: Path,
    entity_type: str,
    bot: bool,
) -> None:
    conn, raw_service = _prepare(
        tmp_path / f"tool-{entity_type}.sqlite", migrated=True, entity_type=entity_type, bot=bot
    )
    service = cast(DaemonEntityInfoService, raw_service)
    service._deps = replace(  # type: ignore[attr-defined]
        service._deps,
        client=_PairClient(bot=bot),
        refresh_limits=replace(service._deps.refresh_limits, foreground_refresh_wait_seconds=0.01),
    )
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))

    class ServiceConnection:
        async def get_entity_info(self, *, entity_id: int) -> dict[str, object]:
            with demand_context(DemandKind.FOREGROUND_ENTITY_FACTS):
                return await service.get_entity_info({"entity_id": entity_id})

    @asynccontextmanager
    async def connection(*, timeout_seconds: float | None = None):
        del timeout_seconds
        yield ServiceConnection()

    with patch("mcp_telegram.tools.entity_info.daemon_connection", connection):
        result = await get_entity_info(GetEntityInfo(exact_entity_id=42))

    assert result.content == ()
    payload = cast(dict[str, object], result.structured_content)
    assert payload["type"] == entity_type
    assert payload["completeness"] == "partial"
    encoded = json.dumps(payload, sort_keys=True)
    for internal in ("evidence", "auth_scope", "normalization_version", "acquisition_generation"):
        assert internal not in encoded
    await service.shutdown()
    conn.close()


@pytest.mark.asyncio
async def test_actual_entity_info_tool_renders_persisted_partial_on_target_kind_mismatch(tmp_path: Path) -> None:
    conn, raw_service = _prepare(tmp_path / "tool-mismatch.sqlite", migrated=True, entity_type="bot", bot=True)
    service = cast(DaemonEntityInfoService, raw_service)
    service._deps = replace(  # type: ignore[attr-defined]
        service._deps,
        client=_PairClient(bot=True),
        refresh_limits=replace(service._deps.refresh_limits, foreground_refresh_wait_seconds=0.01),
    )
    coordinator = service.refresh_coordinator
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    _requeue_new_generation(conn)
    mismatch_client = _PairClient(bot=False)
    service._deps = replace(service._deps, client=mismatch_client)  # type: ignore[attr-defined]
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    assert mismatch_client.full_user_calls == 1

    class ServiceConnection:
        async def get_entity_info(self, *, entity_id: int) -> dict[str, object]:
            with demand_context(DemandKind.FOREGROUND_ENTITY_FACTS):
                return await service.get_entity_info({"entity_id": entity_id})

    @asynccontextmanager
    async def connection(*, timeout_seconds: float | None = None):
        del timeout_seconds
        yield ServiceConnection()

    with patch("mcp_telegram.tools.entity_info.daemon_connection", connection):
        result = await get_entity_info(GetEntityInfo(exact_entity_id=42))

    assert result.content == ()
    payload = cast(dict[str, object], result.structured_content)
    assert payload["type"] == "bot"
    assert payload["completeness"] == "partial"
    encoded = json.dumps(payload, sort_keys=True)
    for internal in ("evidence", "auth_scope", "normalization_version", "acquisition_generation"):
        assert internal not in encoded
    await service.shutdown()
    conn.close()
