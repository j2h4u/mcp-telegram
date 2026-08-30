"""Strict contract tests for the canonical sync read model."""

from __future__ import annotations

from typing import cast

import pytest

from mcp_telegram.realtime_history_policy import realtime_history_coverage
from mcp_telegram.sync_read_model import (
    SyncReadModel,
    SyncReadModelContractError,
    build_sync_read_model,
    decode_sync_read_model,
)

_HISTORY_BY_STATUS = {
    "synced": ("full", "complete", "complete_as_of_last_sync"),
    "syncing": ("full", "partial", "syncing"),
    "own_only": ("own_only", "partial", "own_messages_only"),
    "fragment": ("fragment", "partial", "fragment_only"),
    "access_lost": ("access_lost", "partial", "access_lost_archive"),
    "not_synced": ("none", "none", "not_synced"),
}
_REALTIME_WIRE_BY_POLICY_VALUE = {
    "full_history": "full",
    "own_outgoing": "own_only",
    "no_realtime_history": "none",
}
_WIRE_KEYS = {
    "sync_status",
    "enrollment_enabled",
    "last_synced_at",
    "last_event_at",
    "last_delta_checked_at",
    "saved_message_count",
    "total_messages",
    "synced",
    "is_syncing",
    "realtime_history",
    "history_scope",
    "history_depth_state",
    "history_sync_state",
    "history_complete_at",
    "sync_coverage_pct",
    "coverage_state",
    "local_knowledge_at",
    "local_knowledge_age_seconds",
    "observed_at",
    "action",
}


def _build(**overrides: object) -> SyncReadModel:
    facts: dict[str, object] = {
        "persisted_status": "synced",
        "enrollment_enabled": True,
        "last_synced_at": 90,
        "last_event_at": 95,
        "last_delta_checked_at": 80,
        "saved_message_count": 5,
        "total_messages": 10,
        "now": 100,
    }
    facts.update(overrides)
    return build_sync_read_model(
        persisted_status=cast(str | None, facts["persisted_status"]),
        enrollment_enabled=cast(bool | None, facts["enrollment_enabled"]),
        last_synced_at=cast(int | None, facts["last_synced_at"]),
        last_event_at=cast(int | None, facts["last_event_at"]),
        last_delta_checked_at=cast(int | None, facts["last_delta_checked_at"]),
        saved_message_count=cast(int, facts["saved_message_count"]),
        total_messages=cast(int | None, facts["total_messages"]),
        now=cast(int, facts["now"]),
    )


@pytest.mark.parametrize("persisted_status", [None, *_HISTORY_BY_STATUS])
@pytest.mark.parametrize("enrollment_enabled", [False, True, None])
def test_status_and_enrollment_matrix_matches_realtime_policy(
    persisted_status: str | None,
    enrollment_enabled: bool | None,
) -> None:
    wire = _build(
        persisted_status=persisted_status,
        enrollment_enabled=enrollment_enabled,
    ).to_wire()
    effective_status = "not_synced" if persisted_status is None else persisted_status

    assert wire["sync_status"] == effective_status
    assert wire["enrollment_enabled"] is enrollment_enabled
    assert wire["synced"] is (effective_status == "synced")
    assert wire["is_syncing"] is (effective_status == "syncing")
    policy_value = realtime_history_coverage(effective_status, bool(enrollment_enabled)).value
    assert wire["realtime_history"] == _REALTIME_WIRE_BY_POLICY_VALUE[policy_value]
    assert (
        wire["history_scope"],
        wire["history_depth_state"],
        wire["history_sync_state"],
    ) == _HISTORY_BY_STATUS[effective_status]
    assert wire["history_complete_at"] == (90 if effective_status == "synced" else None)


@pytest.mark.parametrize("persisted_status", ["unknown", "SYNCED", "synced "])
def test_builder_rejects_noncanonical_status(persisted_status: str) -> None:
    with pytest.raises(SyncReadModelContractError):
        _build(persisted_status=persisted_status)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("saved_message_count", -1),
        ("saved_message_count", True),
        ("last_event_at", "95"),
        ("now", "100"),
    ],
)
def test_builder_rejects_representative_malformed_facts(field: str, value: object) -> None:
    with pytest.raises(SyncReadModelContractError):
        _build(**{field: value})


@pytest.mark.parametrize("field", ["last_synced_at", "last_event_at", "last_delta_checked_at"])
@pytest.mark.parametrize("value", [-1, 101])
def test_builder_rejects_negative_or_future_source_timestamp(field: str, value: int) -> None:
    with pytest.raises(SyncReadModelContractError):
        _build(**{field: value})


def test_builder_rejects_negative_observation_timestamp() -> None:
    with pytest.raises(SyncReadModelContractError):
        _build(
            last_synced_at=None,
            last_event_at=None,
            last_delta_checked_at=None,
            now=-1,
        )


@pytest.mark.parametrize(
    ("total_messages", "saved_message_count", "expected_pct", "expected_state"),
    [
        (None, 5, None, "telegram_total_unknown"),
        (-1, 5, None, "telegram_total_invalid"),
        (4, 5, None, "telegram_total_not_comparable"),
        (0, 0, 100, "telegram_total_comparable"),
    ],
)
def test_coverage_edge_cases_remain_explicit(
    total_messages: int | None,
    saved_message_count: int,
    expected_pct: int | None,
    expected_state: str,
) -> None:
    wire = _build(
        total_messages=total_messages,
        saved_message_count=saved_message_count,
    ).to_wire()

    assert wire["saved_message_count"] == saved_message_count
    assert wire["total_messages"] == total_messages
    assert wire["sync_coverage_pct"] == expected_pct
    assert wire["coverage_state"] == expected_state


def test_access_lost_preserves_archived_count_without_inventing_telegram_total() -> None:
    wire = _build(
        persisted_status="access_lost",
        saved_message_count=12,
        total_messages=None,
    ).to_wire()

    assert wire["history_scope"] == "access_lost"
    assert wire["history_sync_state"] == "access_lost_archive"
    assert wire["saved_message_count"] == 12
    assert wire["sync_coverage_pct"] is None
    assert wire["coverage_state"] == "telegram_total_unknown"


def test_freshness_uses_latest_timestamp_and_exact_observation_age() -> None:
    wire = _build().to_wire()

    assert wire["local_knowledge_at"] == 95
    assert wire["observed_at"] == 100
    assert wire["local_knowledge_age_seconds"] == 5


def test_freshness_accepts_explicit_null_timestamp_pair() -> None:
    wire = _build(
        last_synced_at=None,
        last_event_at=None,
        last_delta_checked_at=None,
    ).to_wire()

    assert wire["local_knowledge_at"] is None
    assert wire["local_knowledge_age_seconds"] is None


def test_canonical_wire_key_set_and_round_trip() -> None:
    model = _build()
    wire = model.to_wire()

    assert set(wire) == _WIRE_KEYS
    assert decode_sync_read_model(wire) == model


def test_decoder_rejects_missing_required_wire_field() -> None:
    wire = _build().to_wire()
    del wire["observed_at"]

    with pytest.raises(SyncReadModelContractError):
        decode_sync_read_model(wire)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("observed_at", 94),
        ("observed_at", -1),
        ("local_knowledge_age_seconds", 6),
        ("local_knowledge_age_seconds", None),
        ("history_scope", "complete"),
        ("action", "invented delivery action"),
    ],
)
def test_decoder_rejects_inconsistent_canonical_derivation(field: str, value: object) -> None:
    wire = _build().to_wire()
    wire[field] = value

    with pytest.raises(SyncReadModelContractError):
        decode_sync_read_model(wire)


def test_decoder_rejects_nonnull_age_without_local_knowledge_timestamp() -> None:
    wire = _build(
        last_synced_at=None,
        last_event_at=None,
        last_delta_checked_at=None,
    ).to_wire()
    wire["local_knowledge_age_seconds"] = 0

    with pytest.raises(SyncReadModelContractError):
        decode_sync_read_model(wire)
