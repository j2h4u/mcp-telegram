from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.config import RuntimeObservationConfig
from mcp_telegram.entity_profile.contracts import ProfileAcquisitionEvidence
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
from mcp_telegram.entity_profile.repository import EntitySectionCommit, pair_readiness_latency_ms
from mcp_telegram.rpc_admission_observations import RpcAdmissionObservationAggregator
from tests.demand_adapter_helpers import run_ready_slice
from tests.test_entity_profile_full_user_pair import _PairClient, _prepare


class _Recorder:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    def record(self, **values: object) -> None:
        self.rows.append(values)


def test_pair_summary_preserves_denominators_and_independent_outcomes() -> None:
    recorder = _Recorder()
    aggregator = RpcAdmissionObservationAggregator(
        recorder,
        policy=RuntimeObservationConfig(),
        clock=lambda: 0.0,
    )
    aggregator.observe_profile_pair(
        mode="enabled",
        eligible_pair=True,
        outcome="committed",
        actual_attempts=1,
        full_profile_outcome="usable",
        personal_channel_outcome="partial",
        pair_ready=True,
        pair_readiness_latency_ms=40.0,
    )
    aggregator.observe_profile_pair(
        mode="enabled",
        eligible_pair=True,
        outcome="reused",
        local_satisfaction=True,
        prevented_request=True,
        reused_age_ms=250.0,
    )
    aggregator.flush(now=300.0)

    assert len(recorder.rows) == 2
    committed = next(row for row in recorder.rows if row["outcome"] == "committed")
    assert committed["kind"] == "entity_profile.pair"
    payload = committed["payload"]
    assert isinstance(payload, dict)
    assert payload["event_count"] == 1
    assert payload["actual_attempts"] == 1
    assert payload["pair_ready_count"] == 1
    assert payload["full_profile_outcome"] == "usable"
    assert payload["personal_channel_outcome"] == "partial"
    reused = next(row for row in recorder.rows if row["outcome"] == "reused")
    reused_payload = reused["payload"]
    assert isinstance(reused_payload, dict)
    assert reused_payload["actual_attempts"] == 0
    assert reused_payload["prevented_request"] is True
    assert reused_payload["reused_age_ms"] == 250.0


def test_profile_telemetry_rejects_identifier_shaped_values_recursively() -> None:
    recorder = _Recorder()
    aggregator = RpcAdmissionObservationAggregator(
        recorder,
        policy=RuntimeObservationConfig(),
        clock=lambda: 0.0,
    )
    aggregator.observe_profile_pair(
        mode="enabled",
        eligible_pair=True,
        outcome="committed",
        full_profile_outcome="<telegram id=123>",
    )
    aggregator.observe_profile_pair(
        mode="enabled",
        eligible_pair=True,
        outcome="committed",
        reuse_rejection_reason="account_id=123",  # type: ignore[arg-type]
    )
    aggregator.flush(now=300.0)
    assert recorder.rows == []


def test_profile_observer_failure_is_isolated_from_aggregation() -> None:
    class _BrokenRecorder:
        def record(self, **_values: object) -> None:
            raise RuntimeError("telemetry is unavailable")

    aggregator = RpcAdmissionObservationAggregator(
        _BrokenRecorder(),
        policy=RuntimeObservationConfig(),
        clock=lambda: 0.0,
    )
    aggregator.observe_profile_pair(
        mode="enabled",
        eligible_pair=True,
        outcome="committed",
        actual_attempts=1,
        full_profile_outcome="usable",
        personal_channel_outcome="absent",
        pair_ready=True,
    )
    aggregator.flush(now=300.0)


@pytest.mark.asyncio
async def test_profile_observation_runs_after_pair_commit(tmp_path: Path) -> None:
    conn, service = _prepare(tmp_path / "pair-telemetry.sqlite")
    rows: list[tuple[str, str]] = []

    class _Observer:
        def observe_profile_pair(self, **values: object) -> None:
            rows.extend(
                conn.execute(
                    "SELECT section, status FROM entity_detail_sections WHERE entity_id=42 ORDER BY section"
                ).fetchall()
            )
            assert values["outcome"] == "committed"

    service._deps = replace(service._deps, profile_observer=_Observer())  # type: ignore[attr-defined]
    coordinator = service.refresh_coordinator  # type: ignore[attr-defined]
    assert coordinator is not None
    await run_ready_slice(EntityProfileDemandAdapter(coordinator))
    assert rows == [
        ("avatar_history", "pending"),
        ("common_chats", "pending"),
        ("contact_overlap", "pending"),
        ("current_stories", "pending"),
        ("full_profile", "fresh"),
        ("personal_channel", "fresh"),
        ("pinned_stories", "pending"),
        ("saved_gifts", "pending"),
        ("saved_music", "pending"),
    ]
    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()


@pytest.mark.asyncio
async def test_broken_profile_observer_does_not_change_pair_commit(tmp_path: Path) -> None:
    conn, service = _prepare(tmp_path / "pair-telemetry-failure.sqlite")

    class _BrokenObserver:
        def observe_profile_pair(self, **_values: object) -> None:
            raise RuntimeError("telemetry failed")

    service._deps = replace(service._deps, profile_observer=_BrokenObserver())  # type: ignore[attr-defined]
    coordinator = service.refresh_coordinator  # type: ignore[attr-defined]
    assert coordinator is not None
    await run_ready_slice(EntityProfileDemandAdapter(coordinator))
    assert conn.execute(
        "SELECT status FROM entity_detail_sections WHERE entity_id=42 AND section='full_profile'"
    ).fetchone() == ("fresh",)
    assert cast(_PairClient, service._deps.client).full_user_calls == 1  # type: ignore[attr-defined]
    await service.shutdown()  # type: ignore[attr-defined]
    conn.close()


@pytest.mark.parametrize(
    ("first_bounds", "second_bounds", "expected"),
    [
        (None, None, None),
        ((None, 11), (10, 12), None),
        ((10, None), (10, 12), None),
        ((10, 11), (None, 12), None),
        ((10, 11), (10, None), None),
        ((10, 12), None, 2000.0),
        (None, (10, 12), 2000.0),
        ((0, 0), (0, 0), 0.0),
        ((12, 13), (10, 11), 3000.0),
        ((2**60, 2**60 + 1), (2**60 + 2, 2**60 + 3), 3000.0),
        ((0, 2**53 + 1), None, float((2**53 + 1) * 1000)),
    ],
)
def test_pair_readiness_latency_uses_all_available_evidence_bounds(
    first_bounds: tuple[int | None, int | None] | None,
    second_bounds: tuple[int | None, int | None] | None,
    expected: float | None,
) -> None:
    commits = [
        EntitySectionCommit(
            detail_patch={},
            evidence=None
            if bounds is None
            else ProfileAcquisitionEvidence(
                generation=1,
                outcome="usable",
                observation_started_at=bounds[0],
                observation_completed_at=bounds[1],
            ),
        )
        for bounds in (first_bounds, second_bounds)
    ]
    latency = pair_readiness_latency_ms(*commits)
    assert latency == expected
    if latency is not None:
        assert isinstance(latency, float)
