"""Canonical sync lifecycle, coverage, and freshness read model."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from .realtime_history_policy import RealtimeHistoryCoverage, realtime_history_coverage


class SyncReadModelContractError(ValueError):
    """Raised when canonical sync facts cannot be built or decoded safely."""


class SyncStatus(StrEnum):
    NOT_SYNCED = "not_synced"
    SYNCING = "syncing"
    SYNCED = "synced"
    OWN_ONLY = "own_only"
    FRAGMENT = "fragment"
    ACCESS_LOST = "access_lost"


class RealtimeHistory(StrEnum):
    FULL = "full"
    OWN_ONLY = "own_only"
    NONE = "none"


class HistoryScope(StrEnum):
    FULL = "full"
    OWN_ONLY = "own_only"
    FRAGMENT = "fragment"
    ACCESS_LOST = "access_lost"
    NONE = "none"


class HistoryDepthState(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    NONE = "none"


class HistorySyncState(StrEnum):
    COMPLETE_AS_OF_LAST_SYNC = "complete_as_of_last_sync"
    SYNCING = "syncing"
    OWN_MESSAGES_ONLY = "own_messages_only"
    FRAGMENT_ONLY = "fragment_only"
    ACCESS_LOST_ARCHIVE = "access_lost_archive"
    NOT_SYNCED = "not_synced"


class CoverageState(StrEnum):
    TELEGRAM_TOTAL_UNKNOWN = "telegram_total_unknown"
    TELEGRAM_TOTAL_INVALID = "telegram_total_invalid"
    TELEGRAM_TOTAL_NOT_COMPARABLE = "telegram_total_not_comparable"
    TELEGRAM_TOTAL_COMPARABLE = "telegram_total_comparable"


@dataclass(frozen=True, slots=True)
class _StatusProfile:
    history_scope: HistoryScope
    history_depth_state: HistoryDepthState
    history_sync_state: HistorySyncState
    advisory: str


_STATUS_PROFILES: Mapping[SyncStatus, _StatusProfile] = MappingProxyType(
    {
        SyncStatus.SYNCED: _StatusProfile(
            HistoryScope.FULL,
            HistoryDepthState.COMPLETE,
            HistorySyncState.COMPLETE_AS_OF_LAST_SYNC,
            "Full history was fetched as of last_synced_at; ongoing freshness is represented by local_knowledge_at.",
        ),
        SyncStatus.SYNCING: _StatusProfile(
            HistoryScope.FULL,
            HistoryDepthState.PARTIAL,
            HistorySyncState.SYNCING,
            "History sync state is represented by history_sync_state.",
        ),
        SyncStatus.OWN_ONLY: _StatusProfile(
            HistoryScope.OWN_ONLY,
            HistoryDepthState.PARTIAL,
            HistorySyncState.OWN_MESSAGES_ONLY,
            "Only own-message-related history is stored for this dialog.",
        ),
        SyncStatus.FRAGMENT: _StatusProfile(
            HistoryScope.FRAGMENT,
            HistoryDepthState.PARTIAL,
            HistorySyncState.FRAGMENT_ONLY,
            "History sync state is represented by history_sync_state.",
        ),
        SyncStatus.ACCESS_LOST: _StatusProfile(
            HistoryScope.ACCESS_LOST,
            HistoryDepthState.PARTIAL,
            HistorySyncState.ACCESS_LOST_ARCHIVE,
            "This dialog is an access-lost local archive, not a current live mirror.",
        ),
        SyncStatus.NOT_SYNCED: _StatusProfile(
            HistoryScope.NONE,
            HistoryDepthState.NONE,
            HistorySyncState.NOT_SYNCED,
            "This dialog is not enrolled for history sync.",
        ),
    }
)
_REALTIME_HISTORY_WIRE = {
    RealtimeHistoryCoverage.FULL_HISTORY: RealtimeHistory.FULL,
    RealtimeHistoryCoverage.OWN_OUTGOING: RealtimeHistory.OWN_ONLY,
    RealtimeHistoryCoverage.NO_REALTIME_HISTORY: RealtimeHistory.NONE,
}


@dataclass(frozen=True, slots=True)
class SyncReadModel:
    """Complete canonical facts shared by sync-status and dialog-list reads."""

    sync_status: SyncStatus
    enrollment_enabled: bool | None
    last_synced_at: int | None
    last_event_at: int | None
    last_delta_checked_at: int | None
    saved_message_count: int
    total_messages: int | None
    synced: bool
    is_syncing: bool
    realtime_history: RealtimeHistory
    history_scope: HistoryScope
    history_depth_state: HistoryDepthState
    history_sync_state: HistorySyncState
    history_complete_at: int | None
    sync_coverage_pct: int | None
    coverage_state: CoverageState
    local_knowledge_at: int | None
    local_knowledge_age_seconds: int | None
    observed_at: int
    action: str

    def to_wire(self) -> dict[str, object]:
        """Serialize the complete canonical daemon-to-delivery contract."""
        return {
            "sync_status": self.sync_status.value,
            "enrollment_enabled": self.enrollment_enabled,
            "last_synced_at": self.last_synced_at,
            "last_event_at": self.last_event_at,
            "last_delta_checked_at": self.last_delta_checked_at,
            "saved_message_count": self.saved_message_count,
            "total_messages": self.total_messages,
            "synced": self.synced,
            "is_syncing": self.is_syncing,
            "realtime_history": self.realtime_history.value,
            "history_scope": self.history_scope.value,
            "history_depth_state": self.history_depth_state.value,
            "history_sync_state": self.history_sync_state.value,
            "history_complete_at": self.history_complete_at,
            "sync_coverage_pct": self.sync_coverage_pct,
            "coverage_state": self.coverage_state.value,
            "local_knowledge_at": self.local_knowledge_at,
            "local_knowledge_age_seconds": self.local_knowledge_age_seconds,
            "observed_at": self.observed_at,
            "action": self.action,
        }


def compute_sync_coverage(total_messages: int | None, local_count: int) -> int | None:
    """Return the diagnostic local-to-Telegram percentage when comparable."""
    if total_messages is None or total_messages < 0 or local_count > total_messages:
        return None
    if total_messages == 0:
        return 100
    return round(local_count / total_messages * 100)


def _status(value: str | None) -> SyncStatus:
    if value is None:
        return SyncStatus.NOT_SYNCED
    if not isinstance(value, str):
        raise SyncReadModelContractError(f"persisted_status must be a string or null, got {type(value).__name__}")
    try:
        return SyncStatus(value)
    except ValueError as exc:
        raise SyncReadModelContractError(f"unsupported persisted sync status: {value!r}") from exc


def _strict_optional_int(name: str, value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise SyncReadModelContractError(f"{name} must be an integer or null, got {type(value).__name__}")
    return value


def _strict_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SyncReadModelContractError(f"{name} must be an integer, got {type(value).__name__}")
    return value


def _strict_optional_bool(name: str, value: object) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    raise SyncReadModelContractError(f"{name} must be a boolean or null, got {type(value).__name__}")


def _coverage_state(total_messages: int | None, saved_message_count: int) -> CoverageState:
    if total_messages is None:
        return CoverageState.TELEGRAM_TOTAL_UNKNOWN
    if total_messages < 0:
        return CoverageState.TELEGRAM_TOTAL_INVALID
    if saved_message_count > total_messages:
        return CoverageState.TELEGRAM_TOTAL_NOT_COMPARABLE
    return CoverageState.TELEGRAM_TOTAL_COMPARABLE


def _realtime_history(status: SyncStatus, enrollment_enabled: bool | None) -> RealtimeHistory:
    coverage = realtime_history_coverage(status.value, enrollment_enabled is True)
    return _REALTIME_HISTORY_WIRE[coverage]


def _coverage_advisory(total_messages: int | None, saved_message_count: int) -> str:
    if total_messages is None:
        return "Coverage is unknown without Telegram total_messages."
    if saved_message_count > total_messages:
        return "Local message_count exceeds Telegram total_messages, so coverage is not comparable."
    if total_messages == 0:
        return "Empty dialogs are complete; non-empty local counts would be inconsistent."
    return "Treat sync_coverage_pct as an approximate local-vs-Telegram ratio."


def _action(
    profile: _StatusProfile,
    total_messages: int | None,
    saved_message_count: int,
    coverage_state: CoverageState,
) -> str:
    parts = [
        profile.advisory,
        "sync_progress is a message_id offset, not a count.",
        _coverage_advisory(total_messages, saved_message_count),
    ]
    if coverage_state is CoverageState.TELEGRAM_TOTAL_NOT_COMPARABLE:
        parts.append("Stored Telegram total_messages is suspect; percentage coverage is diagnostic-only.")
    return " ".join(parts)


def build_sync_read_model(  # noqa: PLR0913
    *,
    persisted_status: str | None,
    enrollment_enabled: bool | None,
    last_synced_at: int | None,
    last_event_at: int | None,
    last_delta_checked_at: int | None,
    saved_message_count: int,
    total_messages: int | None,
    now: int,
) -> SyncReadModel:
    """Build canonical facts from persisted coverage, enrollment, and counts."""
    status = _status(persisted_status)
    enrollment = _strict_optional_bool("enrollment_enabled", enrollment_enabled)
    last_synced = _strict_optional_int("last_synced_at", last_synced_at)
    last_event = _strict_optional_int("last_event_at", last_event_at)
    last_delta = _strict_optional_int("last_delta_checked_at", last_delta_checked_at)
    saved_count = _strict_int("saved_message_count", saved_message_count)
    telegram_total = _strict_optional_int("total_messages", total_messages)
    timestamp = _strict_int("now", now)
    if saved_count < 0:
        raise SyncReadModelContractError("saved_message_count must be non-negative")
    if timestamp < 0:
        raise SyncReadModelContractError("now must be non-negative")
    for name, value in (
        ("last_synced_at", last_synced),
        ("last_event_at", last_event),
        ("last_delta_checked_at", last_delta),
    ):
        if value is not None and value < 0:
            raise SyncReadModelContractError(f"{name} must be non-negative or null")
        if value is not None and value > timestamp:
            raise SyncReadModelContractError(f"{name} cannot be later than observed_at")

    local_knowledge_at = max(
        (value for value in (last_synced, last_event, last_delta) if value is not None),
        default=None,
    )
    profile = _STATUS_PROFILES[status]
    coverage_state = _coverage_state(telegram_total, saved_count)
    return SyncReadModel(
        sync_status=status,
        enrollment_enabled=enrollment,
        last_synced_at=last_synced,
        last_event_at=last_event,
        last_delta_checked_at=last_delta,
        saved_message_count=saved_count,
        total_messages=telegram_total,
        synced=status is SyncStatus.SYNCED,
        is_syncing=status is SyncStatus.SYNCING,
        realtime_history=_realtime_history(status, enrollment),
        history_scope=profile.history_scope,
        history_depth_state=profile.history_depth_state,
        history_sync_state=profile.history_sync_state,
        history_complete_at=last_synced if status is SyncStatus.SYNCED else None,
        sync_coverage_pct=compute_sync_coverage(telegram_total, saved_count),
        coverage_state=coverage_state,
        local_knowledge_at=local_knowledge_at,
        local_knowledge_age_seconds=(None if local_knowledge_at is None else timestamp - local_knowledge_at),
        observed_at=timestamp,
        action=_action(profile, telegram_total, saved_count, coverage_state),
    )


def _required(data: Mapping[str, object], name: str) -> object:
    if name not in data:
        raise SyncReadModelContractError(f"missing canonical sync field: {name}")
    return data[name]


def _enum_field[T: StrEnum](data: Mapping[str, object], name: str, enum_type: type[T]) -> T:
    value = _required(data, name)
    if not isinstance(value, str):
        raise SyncReadModelContractError(f"{name} must be a string, got {type(value).__name__}")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise SyncReadModelContractError(f"unsupported {name}: {value!r}") from exc


def _bool_field(data: Mapping[str, object], name: str) -> bool:
    value = _required(data, name)
    if not isinstance(value, bool):
        raise SyncReadModelContractError(f"{name} must be a boolean, got {type(value).__name__}")
    return value


def _string_field(data: Mapping[str, object], name: str) -> str:
    value = _required(data, name)
    if not isinstance(value, str):
        raise SyncReadModelContractError(f"{name} must be a string, got {type(value).__name__}")
    return value


def _optional_bool_field(data: Mapping[str, object], name: str) -> bool | None:
    return _strict_optional_bool(name, _required(data, name))


def _int_field(data: Mapping[str, object], name: str) -> int:
    return _strict_int(name, _required(data, name))


def _optional_int_field(data: Mapping[str, object], name: str) -> int | None:
    return _strict_optional_int(name, _required(data, name))


def _validate_decoded(model: SyncReadModel) -> None:
    expected = build_sync_read_model(
        persisted_status=model.sync_status.value,
        enrollment_enabled=model.enrollment_enabled,
        last_synced_at=model.last_synced_at,
        last_event_at=model.last_event_at,
        last_delta_checked_at=model.last_delta_checked_at,
        saved_message_count=model.saved_message_count,
        total_messages=model.total_messages,
        now=model.observed_at,
    )
    expected_wire = expected.to_wire()
    model_wire = model.to_wire()
    for name, expected_value in expected_wire.items():
        if model_wire[name] != expected_value:
            raise SyncReadModelContractError(f"inconsistent canonical sync field: {name}")


def decode_sync_read_model(data: Mapping[str, object]) -> SyncReadModel:
    """Strictly decode canonical daemon facts without fabricating defaults."""
    model = SyncReadModel(
        sync_status=_enum_field(data, "sync_status", SyncStatus),
        enrollment_enabled=_optional_bool_field(data, "enrollment_enabled"),
        last_synced_at=_optional_int_field(data, "last_synced_at"),
        last_event_at=_optional_int_field(data, "last_event_at"),
        last_delta_checked_at=_optional_int_field(data, "last_delta_checked_at"),
        saved_message_count=_int_field(data, "saved_message_count"),
        total_messages=_optional_int_field(data, "total_messages"),
        synced=_bool_field(data, "synced"),
        is_syncing=_bool_field(data, "is_syncing"),
        realtime_history=_enum_field(data, "realtime_history", RealtimeHistory),
        history_scope=_enum_field(data, "history_scope", HistoryScope),
        history_depth_state=_enum_field(data, "history_depth_state", HistoryDepthState),
        history_sync_state=_enum_field(data, "history_sync_state", HistorySyncState),
        history_complete_at=_optional_int_field(data, "history_complete_at"),
        sync_coverage_pct=_optional_int_field(data, "sync_coverage_pct"),
        coverage_state=_enum_field(data, "coverage_state", CoverageState),
        local_knowledge_at=_optional_int_field(data, "local_knowledge_at"),
        local_knowledge_age_seconds=_optional_int_field(data, "local_knowledge_age_seconds"),
        observed_at=_int_field(data, "observed_at"),
        action=_string_field(data, "action"),
    )
    _validate_decoded(model)
    return model
