"""SQLite repository for progressive entity profile data.

``entity_details`` remains the compatibility blob. Section rows are additive
metadata and payload storage; old databases and test fixtures without the
table continue to work through the blob fallback.
"""

# SQLite's dynamic row shape is guarded at runtime by the compatibility
# fallbacks below; static ``Any`` propagation from sqlite3 is not useful here.
# pyright: reportAny=false

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from ..entity_store import EntitySnapshot, upsert_entity_snapshots
from ..models import DialogType
from .contracts import (
    FULL_PROFILE_OWNED_FIELDS,
    FULL_USER_ENDPOINT,
    NORMALIZATION_VERSION,
    PERSONAL_CHANNEL_OWNED_FIELDS,
    PROFILE_SECTIONS,
    ProfileAcquisitionEvidence,
)

_DETAIL_SCHEMA = 1
_EVIDENCE_MAX_JSON_BYTES = 4096
_SECTION_STATUSES = {"fresh", "stale", "pending", "unavailable", "not_applicable"}


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True, slots=True)
class StoredProfile:
    detail: dict[str, object]
    observed_at: int | None
    sections: dict[str, dict[str, object]]
    profile_owner_account_id: int | None = None
    profile_observation_scope: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class EntityRefreshCursor:
    entity_id: int
    next_section: str
    acquisition_cursor: int
    retry_at: int | None
    generation: int = 0
    started_at: int | None = None
    pair_eligible: bool = False
    profile_revision: int = 0
    pair_mode: str | None = None


@dataclass(frozen=True, slots=True)
class EntitySectionCommit:
    detail_patch: Mapping[str, object]
    status: str = "fresh"
    reason: str | None = None
    payload: object | None = None
    evidence: ProfileAcquisitionEvidence | None = None
    observation_owner_account_id: int | None = None
    observation_auth_scope: Mapping[str, object] | None = None
    ownership_observed: bool = False


@dataclass(frozen=True, slots=True)
class _PairMeasurement:
    outcome: str
    actual_attempts: int
    ready_at: int
    readiness_latency_ms: float | None


@dataclass(frozen=True, slots=True)
class _PairSummaryContext:
    cursor: EntityRefreshCursor
    section: str
    mode: str
    current: tuple[object, ...]
    measurement: _PairMeasurement


@dataclass(frozen=True, slots=True)
class _EncodedDetail:
    entity_id: int
    encoded: str
    metadata_columns: tuple[str, ...]
    metadata_values: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class _ValidatedReceipt:
    """Receipt integrity shared by reuse and same-generation completion."""

    outcome: str
    generation: int
    observation_started_at: int
    observation_completed_at: int


class EntityProfileRepository:
    """Read and write entity profiles without making Telegram calls."""

    def __init__(self, conn: sqlite3.Connection, *, section_ttl_seconds: int) -> None:
        self._conn = conn
        self._section_ttl_seconds = max(1, int(section_ttl_seconds))
        self._column_cache: dict[str, set[str]] = {}
        self._emitted_pair_summaries: set[tuple[int, int]] = set()

    def _columns(self, table: str) -> set[str]:
        columns = self._column_cache.get(table)
        if columns is None:
            try:
                columns = {str(row[1]) for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            except sqlite3.OperationalError:
                columns = set()
            self._column_cache[table] = columns
        return columns

    def _refresh_has(self, column: str) -> bool:
        return column in self._columns("entity_profile_refresh_state")

    def _refresh_columns_or_empty(self) -> set[str]:
        return self._columns("entity_profile_refresh_state")

    def _section_has(self, column: str) -> bool:
        return column in self._columns("entity_detail_sections")

    def _detail_has(self, column: str) -> bool:
        return column in self._columns("entity_details")

    def read(self, entity_id: int, *, now: int) -> StoredProfile | None:
        detail, observed_at, owner_account_id, observation_scope = self._read_primary_detail(entity_id)
        if not detail:
            return None
        sections = self._read_sections(entity_id, detail, now=now, observed_at=observed_at)
        return StoredProfile(
            detail=detail,
            observed_at=observed_at,
            sections=sections,
            profile_owner_account_id=owner_account_id,
            profile_observation_scope=observation_scope,
        )

    def read_section_evidence(self, entity_id: int, section: str) -> dict[str, object] | None:
        """Return the bounded receipt projection, if a migration-created row has one."""
        return self._read_section_evidence(entity_id=entity_id, section=section)

    def section_is_reusable(
        self,
        entity_id: int,
        section: str,
        *,
        now: int,
        identity: Mapping[str, object],
        ttl_seconds: int | None = None,
    ) -> bool:
        """Check a positive receipt without renewing its observation boundary."""
        return self._reusable_receipt_is_valid(entity_id, section, identity=identity, now=now, ttl_seconds=ttl_seconds)

    def reuse_full_user_pair(
        self,
        cursor: EntityRefreshCursor,
        identity: Mapping[str, object],
        *,
        now: int,
    ) -> bool:
        """Reuse both positive pair receipts without renewing their age."""
        if cursor.next_section != "full_profile":
            return False
        with self._conn:
            if not self._cursor_matches(cursor):
                return False
            if not all(
                self._reusable_receipt_is_valid(
                    cursor.entity_id,
                    section,
                    identity=identity,
                    now=now,
                )
                for section in ("full_profile", "personal_channel")
            ):
                return False
            self._conn.execute(
                "UPDATE entity_detail_sections SET status='fresh', reason=NULL, retry_at=NULL "
                "WHERE entity_id=? AND section IN ('full_profile', 'personal_channel')",
                (cursor.entity_id,),
            )
            predicate, parameters = self._cursor_predicate(cursor)
            changed = self._conn.execute(
                "UPDATE entity_profile_refresh_state SET status='pending', retry_at=NULL, "
                "reason='refresh_queued', updated_at=?, next_section=?, acquisition_cursor=0 WHERE " + predicate,
                (now, PROFILE_SECTIONS[1], *parameters),
            ).rowcount
            return changed == 1

    def full_user_pair_reuse_rejection_reason(
        self,
        cursor: EntityRefreshCursor,
        identity: Mapping[str, object] | None,
        *,
        now: int,
    ) -> str | None:
        """Return a bounded reason why a pair receipt cannot suppress work."""
        if cursor.next_section != "full_profile":
            return "cursor_mismatch"
        if not cursor.pair_eligible:
            return "ineligible_pair"
        if identity is None:
            return "missing_identity"
        for section in ("full_profile", "personal_channel"):
            reason = self._pair_section_reuse_rejection_reason(
                cursor.entity_id,
                section,
                identity=identity,
                now=now,
            )
            if reason is not None:
                return reason
        return None

    def _pair_section_reuse_rejection_reason(
        self,
        entity_id: int,
        section: str,
        *,
        identity: Mapping[str, object],
        now: int,
    ) -> str | None:
        evidence = self.read_section_evidence(entity_id, section)
        if evidence is None:
            return "missing_receipt"
        reason = self._pair_evidence_rejection_reason(
            entity_id,
            section,
            evidence,
            identity=identity,
        )
        if reason is not None:
            return reason
        started_at, completed_at = _observation_bounds(evidence)
        if started_at is None or completed_at is None:
            return "invalid_observation"
        if completed_at < started_at or completed_at > now:
            return "invalid_observation"
        return self._pair_section_freshness_rejection(
            entity_id,
            section,
            started_at=started_at,
            now=now,
        )

    def _pair_section_freshness_rejection(
        self,
        entity_id: int,
        section: str,
        *,
        started_at: int,
        now: int,
    ) -> str | None:
        row = self._conn.execute(
            "SELECT status, observed_at FROM entity_detail_sections WHERE entity_id=? AND section=?",
            (entity_id, section),
        ).fetchone()
        if row is None or row[0] != "fresh":
            return "section_not_fresh"
        return "stale" if row[1] != started_at or now >= started_at + self._section_ttl_seconds else None

    def _pair_evidence_rejection_reason(
        self,
        entity_id: int,
        section: str,
        evidence: Mapping[str, object],
        *,
        identity: Mapping[str, object],
    ) -> str | None:
        if evidence.get("outcome") not in {"usable", "absent"}:
            return "outcome_not_reusable"
        stored_identity = evidence.get("identity")
        if not isinstance(stored_identity, dict) or dict(identity) != stored_identity:
            return "identity_mismatch"
        if not self._receipt_materialization_is_exact(entity_id, section, evidence):
            return "materialization_mismatch"
        return None

    def pair_receipts_require_new_scope(
        self,
        entity_id: int,
        identity: Mapping[str, object] | None,
        *,
        now: int,
    ) -> bool:
        """Detect a completed fresh pair whose authorization scope changed."""
        rows = self._conn.execute(
            "SELECT section, status FROM entity_detail_sections "
            "WHERE entity_id=? AND section IN ('full_profile', 'personal_channel')",
            (entity_id,),
        ).fetchall()
        statuses = {str(row[0]): str(row[1]) for row in rows}
        if statuses != {"full_profile": "fresh", "personal_channel": "fresh"}:
            return False
        if identity is None:
            return True
        return any(
            not self._reusable_receipt_is_valid(
                entity_id,
                section,
                identity=identity,
                now=now,
            )
            for section in ("full_profile", "personal_channel")
        )

    def complete_same_generation_section(
        self,
        cursor: EntityRefreshCursor,
        identity: Mapping[str, object] | None,
        *,
        now: int,
    ) -> bool:
        """Advance a paired channel cursor only under matching private evidence."""
        if cursor.next_section != "personal_channel":
            return False
        if identity is None:
            return False
        with self._conn:
            if not self._cursor_matches(cursor):
                return False
            if not self._same_generation_completion_receipt_is_valid(
                cursor.entity_id,
                "personal_channel",
                identity=identity,
                now=now,
                expected_generation=cursor.generation,
            ):
                return False
            return self._advance_completed_section(cursor, now=now)

    def _read_primary_detail(
        self, entity_id: int
    ) -> tuple[dict[str, object], int | None, int | None, dict[str, object] | None]:
        detail, observed_at, owner_account_id, observation_scope = self._read_profile_blob(entity_id)
        if detail:
            return detail, observed_at, owner_account_id, observation_scope
        detail = self._read_entity_stub(entity_id)
        return (detail, None, None, None) if detail else ({}, None, None, None)

    def _read_profile_blob(
        self, entity_id: int
    ) -> tuple[dict[str, object], int | None, int | None, dict[str, object] | None]:
        try:
            row = self._conn.execute(self._profile_blob_query(), (entity_id,)).fetchone()
        except sqlite3.OperationalError:
            return {}, None, None, None
        return _decode_profile_blob_row(row)

    def _profile_blob_query(self) -> str:
        columns = ["detail_json", "fetched_at"]
        columns.extend(
            column
            for column in ("profile_owner_account_id", "profile_observation_scope_json")
            if self._detail_has(column)
        )
        return f"SELECT {', '.join(columns)} FROM entity_details WHERE entity_id = ?"

    def _read_entity_stub(self, entity_id: int) -> dict[str, object]:
        entity_row = cast(
            tuple[str, str | None, str | None] | None,
            self._conn.execute("SELECT type, name, username FROM entities WHERE id = ?", (entity_id,)).fetchone(),
        )
        if entity_row is None:
            return {}
        entity_type, name, username = entity_row
        return {
            "id": entity_id,
            "type": _normalise_entity_type(entity_type),
            "name": name,
            "username": username,
        }

    def save_core(self, detail: Mapping[str, object], *, now: int) -> None:
        """Persist the mandatory core in the existing entities projection."""
        entity_id = detail.get("id")
        if not isinstance(entity_id, int):
            return
        try:
            upsert_entity_snapshots(
                self._conn,
                [
                    EntitySnapshot(
                        entity_id=entity_id,
                        entity_type=str(detail.get("type", "unknown")),
                        name=_optional_text(detail.get("name")),
                        username=_optional_text(detail.get("username")),
                        name_normalized=None,
                        updated_at=now,
                    )
                ],
            )
            if self._detail_has("profile_revision"):
                # Identity writes are canonical profile writes for fencing,
                # but they do not renew the detail blob's observation age.
                self._conn.execute(
                    "UPDATE entity_details SET profile_revision=profile_revision+1 WHERE entity_id=?",
                    (entity_id,),
                )
            self._conn.commit()
        except sqlite3.OperationalError:
            # Compatibility fixtures can expose only entity_details.
            return

    def refresh_state(self, entity_id: int, *, now: int) -> dict[str, object] | None:
        """Return a durable unresolved-refresh state while it is relevant."""
        try:
            columns = self._columns("entity_profile_refresh_state")
            selected = ["status", "retry_at", "reason"]
            selected.extend(
                column
                for column in (
                    "generation",
                    "started_at",
                    "pair_eligible",
                    "follow_up_required",
                    "profile_revision",
                    "pair_mode",
                )
                if column in columns
            )
            row = self._conn.execute(
                f"SELECT {', '.join(selected)} FROM entity_profile_refresh_state WHERE entity_id = ?",
                (entity_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None:
            return None
        state_values = dict(zip(selected, row, strict=False))
        status = state_values["status"]
        retry_at = state_values["retry_at"]
        reason = state_values["reason"]
        if str(status) == "rejected" or (isinstance(retry_at, int) and retry_at > now):
            state = {"status": str(status), "retry_at": retry_at, "reason": str(reason)}
            for column in selected[3:]:
                value = state_values[column]
                state[column] = bool(value) if column in {"pair_eligible", "follow_up_required"} else value
            return state
        return None

    def mark_pending(
        self,
        entity_id: int,
        *,
        now: int,
        reason: str = "refresh_queued",
        pair_eligible_override: bool | None = None,
        pair_mode_override: str | None = None,
    ) -> None:
        """Make pending explicit where the additive section table is present."""
        try:
            with self._conn:
                self._upsert_pending_refresh(
                    entity_id,
                    now=now,
                    reason=reason,
                    pair_eligible_override=pair_eligible_override,
                    pair_mode_override=pair_mode_override,
                )
                self._conn.executemany(
                    "INSERT INTO entity_detail_sections(entity_id, section, status, observed_at, reason, payload_json, retry_at) "
                    "VALUES (?, ?, 'pending', NULL, ?, NULL, NULL) "
                    "ON CONFLICT(entity_id, section) DO UPDATE SET "
                    "status=CASE WHEN entity_detail_sections.status='not_applicable' "
                    "THEN entity_detail_sections.status ELSE 'pending' END, "
                    "reason=CASE WHEN entity_detail_sections.status='not_applicable' "
                    "THEN entity_detail_sections.reason ELSE excluded.reason END, retry_at=NULL",
                    ((entity_id, section, reason) for section in PROFILE_SECTIONS),
                )
        except sqlite3.OperationalError:
            return

    def mark_refresh_queued(
        self,
        entity_id: int,
        *,
        reason: str = "refresh_queued",
        pair_mode_override: str | None = None,
    ) -> None:
        """Clear rejection state and restore an honest queued reason."""
        try:
            with self._conn:
                self._upsert_pending_refresh(
                    entity_id,
                    now=self._database_now(),
                    reason=reason,
                    pair_mode_override=pair_mode_override,
                )
                self._conn.execute(
                    "UPDATE entity_detail_sections SET status='pending', reason=?, retry_at=NULL "
                    "WHERE entity_id=? AND status IN ('pending', 'stale', 'unavailable')",
                    (reason, entity_id),
                )
        except sqlite3.OperationalError:
            return

    def _upsert_pending_refresh(
        self,
        entity_id: int,
        *,
        now: int,
        reason: str,
        pair_eligible_override: bool | None = None,
        pair_mode_override: str | None = None,
    ) -> None:
        if not self._refresh_has("generation"):
            self._upsert_legacy_pending_refresh(entity_id, now=now, reason=reason)
            return

        new_generation, optional_values = self._pending_refresh_state(
            entity_id,
            now=now,
            pair_eligible_override=pair_eligible_override,
            pair_mode_override=pair_mode_override,
        )
        columns = [
            "entity_id",
            "status",
            "retry_at",
            "reason",
            "updated_at",
            "next_section",
            "acquisition_cursor",
        ]
        values: list[object] = [entity_id, "pending", None, reason, now, PROFILE_SECTIONS[0], 0]
        for column, value in optional_values.items():
            if self._refresh_has(column):
                columns.append(column)
                values.append(value)
        query = _pending_refresh_upsert_query(columns)
        self._conn.execute(query, values)
        if new_generation:
            self._reset_pair_measurement(entity_id)

    def _pending_refresh_state(
        self,
        entity_id: int,
        *,
        now: int,
        pair_eligible_override: bool | None,
        pair_mode_override: str | None,
    ) -> tuple[bool, dict[str, object]]:
        existing = self._read_pending_refresh_state(entity_id)
        if _pending_refresh_is_active(existing):
            assert existing is not None
            return False, _active_pending_values(existing, pair_mode_override=pair_mode_override)
        return True, self._new_pending_values(
            entity_id,
            existing,
            now=now,
            pair_eligible_override=pair_eligible_override,
            pair_mode_override=pair_mode_override,
        )

    def _read_pending_refresh_state(self, entity_id: int) -> dict[str, object] | None:
        columns = [
            column
            for column in (
                "status",
                "generation",
                "started_at",
                "pair_eligible",
                "follow_up_required",
                "profile_revision",
                "pair_mode",
            )
            if self._refresh_has(column)
        ]
        row = self._conn.execute(
            f"SELECT {', '.join(columns)} FROM entity_profile_refresh_state WHERE entity_id=?",
            (entity_id,),
        ).fetchone()
        return dict(zip(columns, row, strict=False)) if row is not None else None

    def _new_pending_values(
        self,
        entity_id: int,
        existing: Mapping[str, object] | None,
        *,
        now: int,
        pair_eligible_override: bool | None,
        pair_mode_override: str | None,
    ) -> dict[str, object]:
        previous_generation = _as_int(existing.get("generation")) if existing is not None else 0
        pair_eligible = (
            self._pair_is_eligible(entity_id, now=now) if pair_eligible_override is None else pair_eligible_override
        )
        return {
            "generation": max(1, previous_generation + 1),
            "started_at": now,
            "pair_eligible": int(pair_eligible),
            "follow_up_required": 0,
            "profile_revision": self._profile_revision(entity_id),
            "pair_mode": pair_mode_override,
        }

    def _upsert_legacy_pending_refresh(self, entity_id: int, *, now: int, reason: str) -> None:
        self._conn.execute(
            """
            INSERT INTO entity_profile_refresh_state(
                entity_id, status, retry_at, reason, updated_at, next_section, acquisition_cursor
            ) VALUES (?, 'pending', NULL, ?, ?, ?, 0)
            ON CONFLICT(entity_id) DO UPDATE SET
                status='pending', retry_at=NULL, reason=excluded.reason, updated_at=excluded.updated_at,
                next_section=CASE
                    WHEN entity_profile_refresh_state.status='pending'
                     AND entity_profile_refresh_state.next_section IS NOT NULL
                    THEN entity_profile_refresh_state.next_section
                    ELSE excluded.next_section
                END,
                acquisition_cursor=CASE
                    WHEN entity_profile_refresh_state.status='pending'
                     AND entity_profile_refresh_state.next_section IS NOT NULL
                    THEN entity_profile_refresh_state.acquisition_cursor
                    ELSE 0
                END
            """,
            (entity_id, reason, now, PROFILE_SECTIONS[0]),
        )

    def _profile_revision(self, entity_id: int) -> int:
        if not self._detail_has("profile_revision"):
            return 0
        row = self._conn.execute(
            "SELECT profile_revision FROM entity_details WHERE entity_id=?", (entity_id,)
        ).fetchone()
        return int(row[0]) if row is not None and row[0] is not None else 0

    def _pair_is_eligible(self, entity_id: int, *, now: int) -> bool:
        detail, observed_at, _owner, _scope = self._read_primary_detail(entity_id)
        if _normalise_entity_type(str(detail.get("type", "unknown"))) not in {"user", "bot"}:
            return False
        stored = self._read_stored_sections(entity_id)
        return all(
            self._section_requires_acquisition(section, stored.get(section), detail, observed_at=observed_at, now=now)
            for section in ("full_profile", "personal_channel")
        )

    def _section_requires_acquisition(
        self,
        section: str,
        row: tuple[str, str, int | None, str | None, str | None] | None,
        detail: Mapping[str, object],
        *,
        observed_at: int | None,
        now: int,
    ) -> bool:
        if row is None:
            payload = _section_payload(detail, section)
            return payload is None or observed_at is None or now - observed_at >= self._section_ttl_seconds
        _section_name, raw_status, section_observed_at, _reason, _payload = row
        return _section_due(raw_status, section_observed_at, now=now, ttl=self._section_ttl_seconds)

    def _database_now(self) -> int:
        row = cast(tuple[int] | None, self._conn.execute("SELECT unixepoch()").fetchone())
        return row[0] if row is not None else 0

    def next_refresh_release_at(self) -> float | None:
        """Return the earliest durable pending or retryable refresh release."""
        try:
            row = cast(
                tuple[int | None] | None,
                self._conn.execute(
                    "SELECT MIN(COALESCE(retry_at, 0)) FROM entity_profile_refresh_state "
                    "WHERE status IN ('pending', 'failed')"
                ).fetchone(),
            )
        except sqlite3.OperationalError:
            return None
        return float(row[0]) if row is not None and row[0] is not None else None

    def next_due_refresh(self, *, now: int) -> EntityRefreshCursor | None:
        """Read the oldest due entity cursor without claiming or changing it."""
        try:
            self._recover_pair_measurements(now=now)
            columns = self._columns("entity_profile_refresh_state")
            selected = ["entity_id", "next_section", "acquisition_cursor", "retry_at"]
            selected.extend(
                column
                for column in (
                    "generation",
                    "started_at",
                    "pair_eligible",
                    "follow_up_required",
                    "profile_revision",
                    "pair_mode",
                )
                if column in columns
            )
            row = self._conn.execute(
                f"SELECT {', '.join(selected)} FROM entity_profile_refresh_state "
                "WHERE status IN ('pending', 'failed') AND (retry_at IS NULL OR retry_at <= ?) "
                "ORDER BY COALESCE(retry_at, 0), updated_at, entity_id LIMIT 1",
                (now,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None:
            return None
        values = dict(zip(selected, row, strict=False))
        return EntityRefreshCursor(
            entity_id=int(values["entity_id"]),
            next_section=str(values["next_section"]),
            acquisition_cursor=int(values["acquisition_cursor"]),
            retry_at=cast(int | None, values["retry_at"]),
            generation=int(values.get("generation") or 0),
            started_at=cast(int | None, values.get("started_at")),
            pair_eligible=bool(values.get("pair_eligible", 0)),
            profile_revision=int(values.get("profile_revision") or 0),
            pair_mode=(str(values["pair_mode"]) if values.get("pair_mode") in {"enabled", "disabled"} else None),
        )

    def _recover_pair_measurements(self, *, now: int) -> None:
        """Complete measurement rows left behind by a committed pair receipt."""
        required = {
            "pair_full_profile_outcome",
            "pair_personal_channel_outcome",
            "pair_measurement_complete",
            "pair_ready_at",
            "pair_summary_watermark",
        }
        if not required <= self._columns("entity_profile_refresh_state"):
            return
        with self._conn:
            self._conn.execute(
                "UPDATE entity_profile_refresh_state SET pair_measurement_complete=1, "
                "pair_ready_at=COALESCE(pair_ready_at, ?), "
                "pair_summary_watermark=COALESCE(pair_summary_watermark, ?) "
                "WHERE pair_full_profile_outcome IS NOT NULL "
                "AND pair_personal_channel_outcome IS NOT NULL "
                "AND pair_summary_watermark IS NULL",
                (now, now),
            )

    def capture_pair_mode(self, cursor: EntityRefreshCursor, mode: str) -> str:
        """Capture the feature mode once for a refresh generation.

        A switch change while a generation is in flight must not turn a
        disabled baseline into an enabled pair (or vice versa).  Legacy
        fixtures can start with a NULL mode; the first durable slice fills it
        using the current switch value.
        """
        if mode not in {"enabled", "disabled"}:
            raise ValueError("pair mode must be enabled or disabled")
        if not self._refresh_has("pair_mode"):
            return mode
        with self._conn:
            row = self._conn.execute(
                "SELECT pair_mode FROM entity_profile_refresh_state WHERE entity_id=? AND generation=?",
                (cursor.entity_id, cursor.generation),
            ).fetchone()
            if row is None:
                return mode
            stored = row[0] if row[0] in {"enabled", "disabled"} else None
            if stored is not None:
                return str(stored)
            self._conn.execute(
                "UPDATE entity_profile_refresh_state SET pair_mode=? WHERE entity_id=? AND generation=?",
                (mode, cursor.entity_id, cursor.generation),
            )
            return mode

    def record_pair_attempt(
        self,
        cursor: EntityRefreshCursor,
        section: str,
        *,
        actual_attempts: int,
    ) -> None:
        """Durably attribute dispatched pair requests, including retries."""
        with self._conn:
            self._record_pair_attempt_in_transaction(
                cursor,
                section,
                actual_attempts=actual_attempts,
            )

    def _record_pair_attempt_in_transaction(
        self,
        cursor: EntityRefreshCursor,
        section: str,
        *,
        actual_attempts: int,
    ) -> None:
        """Attribute a pair request while an outer transaction is active."""
        if section not in {"full_profile", "personal_channel"} or actual_attempts <= 0:
            return
        if not isinstance(actual_attempts, int) or isinstance(actual_attempts, bool):
            raise ValueError("actual_attempts must be a non-negative integer")
        if not {
            f"pair_{section}_attempts",
            f"pair_{section}_retries",
            "pair_attempts",
            "pair_retries",
        } <= self._columns("entity_profile_refresh_state"):
            return
        attempt_column = f"pair_{section}_attempts"
        retry_column = f"pair_{section}_retries"
        row = self._conn.execute(
            f"SELECT {attempt_column} FROM entity_profile_refresh_state WHERE entity_id=? AND generation=?",
            (cursor.entity_id, cursor.generation),
        ).fetchone()
        if row is None:
            return
        previous = int(row[0] or 0)
        retries = max(0, previous + actual_attempts - 1) - max(0, previous - 1)
        self._conn.execute(
            f"UPDATE entity_profile_refresh_state SET {attempt_column}={attempt_column}+?, "
            f"{retry_column}={retry_column}+?, pair_attempts=pair_attempts+?, "
            "pair_retries=pair_retries+? WHERE entity_id=? AND generation=?",
            (
                actual_attempts,
                retries,
                actual_attempts,
                retries,
                cursor.entity_id,
                cursor.generation,
            ),
        )

    def record_pair_section_outcome(  # noqa: PLR0913
        self,
        cursor: EntityRefreshCursor,
        section: str,
        *,
        outcome: str,
        actual_attempts: int,
        ready_at: int,
        readiness_latency_ms: float | None = None,
    ) -> dict[str, object] | None:
        """Commit one final projection outcome and atomically take its summary.

        The returned mapping is intentionally identifier-free and is emitted
        only once, when both pair outcomes belong to this generation.
        """
        if section not in {"full_profile", "personal_channel"}:
            return None
        if outcome not in {"usable", "partial", "absent", "unavailable"}:
            raise ValueError("invalid pair section outcome")
        attempts = max(0, actual_attempts)
        if not isinstance(attempts, int) or isinstance(attempts, bool):
            raise ValueError("actual_attempts must be a non-negative integer")
        required = {
            "pair_mode",
            "pair_eligible",
            "pair_full_profile_outcome",
            "pair_personal_channel_outcome",
            "pair_summary_watermark",
            "pair_ready_at",
            "pair_attempts",
            "pair_retries",
            "pair_readiness_latency_ms",
            "pair_measurement_complete",
        }
        if not required <= self._columns("entity_profile_refresh_state"):
            return None
        with self._conn:
            return self._record_pair_section_outcome_in_transaction(
                cursor,
                section,
                _PairMeasurement(outcome, attempts, ready_at, readiness_latency_ms),
            )

    def _record_pair_section_outcome_in_transaction(
        self,
        cursor: EntityRefreshCursor,
        section: str,
        measurement: _PairMeasurement,
    ) -> dict[str, object] | None:
        """Record one pair outcome while an outer transaction is active."""
        state = self._pair_outcome_state(cursor)
        if state is None:
            return None
        mode, eligible = state[0], state[1]
        if mode is None or not bool(eligible):
            return None
        self._record_pair_attempt_in_transaction(cursor, section, actual_attempts=measurement.actual_attempts)
        outcome_column = "pair_full_profile_outcome" if section == "full_profile" else "pair_personal_channel_outcome"
        self._conn.execute(
            f"UPDATE entity_profile_refresh_state SET {outcome_column}=? WHERE entity_id=? AND generation=?",
            (measurement.outcome, cursor.entity_id, cursor.generation),
        )
        current = self._conn.execute(
            "SELECT pair_full_profile_outcome, pair_personal_channel_outcome, pair_summary_watermark, "
            "pair_attempts, pair_retries, pair_ready_at, pair_readiness_latency_ms "
            "FROM entity_profile_refresh_state WHERE entity_id=? AND generation=?",
            (cursor.entity_id, cursor.generation),
        ).fetchone()
        if current is None or current[0] is None or current[1] is None:
            return None
        return self._pair_outcome_summary(_PairSummaryContext(cursor, section, str(mode), current, measurement))

    def _pair_outcome_state(self, cursor: EntityRefreshCursor) -> tuple[str | None, object] | None:
        state = self._conn.execute(
            "SELECT pair_mode, pair_eligible FROM entity_profile_refresh_state WHERE entity_id=? AND generation=?",
            (cursor.entity_id, cursor.generation),
        ).fetchone()
        if state is None:
            return None
        mode = state[0] if state[0] in {"enabled", "disabled"} else None
        return mode, state[1]

    def _pair_outcome_summary(self, context: _PairSummaryContext) -> dict[str, object] | None:
        cursor = context.cursor
        section = context.section
        mode = context.mode
        current = context.current
        measurement = context.measurement
        readiness_latency_ms = measurement.readiness_latency_ms
        summary_key = (cursor.entity_id, cursor.generation)
        if current[2] is not None:
            if section != "full_profile" or summary_key in self._emitted_pair_summaries:
                return None
            self._emitted_pair_summaries.add(summary_key)
            return _pair_summary_payload(mode, current, readiness_latency_ms=current[6], ready=True)
        self._conn.execute(
            "UPDATE entity_profile_refresh_state SET pair_measurement_complete=1, pair_ready_at=?, "
            "pair_readiness_latency_ms=?, pair_summary_watermark=? WHERE entity_id=? AND generation=? "
            "AND pair_summary_watermark IS NULL",
            (
                measurement.ready_at,
                readiness_latency_ms,
                measurement.ready_at,
                cursor.entity_id,
                cursor.generation,
            ),
        )
        if self._conn.execute("SELECT changes()").fetchone()[0] != 1:
            return None
        return _pair_summary_payload(mode, current, readiness_latency_ms=readiness_latency_ms, ready=True)

    def advance_acquisition_cursor(
        self,
        cursor: EntityRefreshCursor,
        *,
        next_acquisition_cursor: int,
        now: int,
    ) -> bool:
        """Commit acquisition progress that has no section payload of its own."""
        if next_acquisition_cursor <= cursor.acquisition_cursor:
            raise ValueError("next_acquisition_cursor must advance")
        with self._conn:
            predicate, parameters = self._cursor_predicate(cursor)
            assignments = (
                "status='pending', retry_at=NULL, reason='refresh_in_progress', updated_at=?, acquisition_cursor=?"
            )
            if self._refresh_has("profile_revision"):
                assignments += ", profile_revision=?"
                values: tuple[object, ...] = (now, next_acquisition_cursor, cursor.profile_revision)
            else:
                values = (now, next_acquisition_cursor)
            changed = self._conn.execute(
                f"UPDATE entity_profile_refresh_state SET {assignments} WHERE {predicate}",
                (*values, *parameters),
            ).rowcount
        return changed == 1

    def commit_section(
        self,
        cursor: EntityRefreshCursor,
        commit: EntitySectionCommit,
        *,
        now: int,
    ) -> bool:
        """Commit one section outcome and its next cursor in one transaction."""
        if commit.status not in {"fresh", "unavailable", "not_applicable"}:
            raise ValueError("invalid terminal section status")
        with self._conn:
            return self._commit_section_in_transaction(cursor, commit, now=now)

    def commit_section_with_pair_measurement(
        self,
        cursor: EntityRefreshCursor,
        commit: EntitySectionCommit,
        *,
        now: int,
        outcome: str,
        actual_attempts: int,
    ) -> tuple[bool, dict[str, object] | None]:
        """Commit a section and its disabled pair measurement atomically."""
        if commit.status not in {"fresh", "unavailable", "not_applicable"}:
            raise ValueError("invalid terminal section status")
        required = {
            "pair_mode",
            "pair_eligible",
            "pair_full_profile_outcome",
            "pair_personal_channel_outcome",
            "pair_summary_watermark",
            "pair_ready_at",
            "pair_attempts",
            "pair_retries",
            "pair_readiness_latency_ms",
            "pair_measurement_complete",
        }
        if not required <= self._columns("entity_profile_refresh_state"):
            return self.commit_section(cursor, commit, now=now), None
        with self._conn:
            committed = self._commit_section_in_transaction(cursor, commit, now=now)
            if not committed:
                return False, None
            summary = self._record_pair_section_outcome_in_transaction(
                cursor,
                cursor.next_section,
                _PairMeasurement(outcome, actual_attempts, now, None),
            )
            return True, summary

    def _commit_section_in_transaction(
        self,
        cursor: EntityRefreshCursor,
        commit: EntitySectionCommit,
        *,
        now: int,
    ) -> bool:
        """Commit a section while an outer transaction is active."""
        if not self._cursor_matches(cursor):
            return False
        detail = self._read_detail_blob(cursor.entity_id)
        if not detail:
            detail = self._read_entity_stub(cursor.entity_id)
        detail = _strip_schema(detail)
        detail.update(commit.detail_patch)
        if not self._write_detail(
            cursor.entity_id,
            detail,
            now=now,
            expected_revision=cursor.profile_revision,
            owner_account_id=commit.observation_owner_account_id,
            observation_scope=commit.observation_auth_scope,
            ownership_observed=commit.ownership_observed,
        ):
            return False
        section_payload = _section_payload(detail, cursor.next_section) if commit.payload is None else commit.payload
        self._write_section(
            cursor.entity_id,
            cursor.next_section,
            commit.status,
            commit.reason,
            section_payload,
            now=now,
            evidence=commit.evidence,
        )
        detail_revision = (
            cursor.profile_revision + 1
            if self._refresh_has("profile_revision") and self._detail_has("profile_revision")
            else None
        )
        changed = self._advance_after_section_write(cursor, now=now, detail_revision=detail_revision)
        if changed != 1:
            raise sqlite3.OperationalError("entity profile cursor advance was rejected")
        return True

    def _advance_after_section_write(
        self,
        cursor: EntityRefreshCursor,
        *,
        now: int,
        detail_revision: int | None,
    ) -> int:
        next_section = _next_profile_section(cursor.next_section)
        if next_section is None:
            return self._finish_section_generation(cursor, now=now, detail_revision=detail_revision)
        return self._queue_next_section(cursor, next_section=next_section, now=now, detail_revision=detail_revision)

    def _finish_section_generation(
        self,
        cursor: EntityRefreshCursor,
        *,
        now: int,
        detail_revision: int | None,
    ) -> int:
        if self._refresh_has("generation"):
            if self._refresh_follow_up_required(cursor):
                return self._start_follow_up_generation(cursor, now=now, profile_revision=detail_revision)
            predicate, parameters = self._cursor_predicate(cursor)
            revision_assignment = ", profile_revision=?" if detail_revision is not None else ""
            revision_value = (detail_revision,) if detail_revision is not None else ()
            return self._conn.execute(
                "UPDATE entity_profile_refresh_state SET status='complete', retry_at=NULL, "
                "reason='refresh_complete', updated_at=?" + revision_assignment + " WHERE " + predicate,
                (now, *revision_value, *parameters),
            ).rowcount
        return self._conn.execute(
            "DELETE FROM entity_profile_refresh_state WHERE entity_id=? AND next_section=? AND acquisition_cursor=?",
            (cursor.entity_id, cursor.next_section, cursor.acquisition_cursor),
        ).rowcount

    def _queue_next_section(
        self,
        cursor: EntityRefreshCursor,
        *,
        next_section: str,
        now: int,
        detail_revision: int | None,
    ) -> int:
        predicate, parameters = self._cursor_predicate(cursor)
        revision_assignment = ", profile_revision=?" if detail_revision is not None else ""
        revision_value = (detail_revision,) if detail_revision is not None else ()
        return self._conn.execute(
            "UPDATE entity_profile_refresh_state SET status='pending', retry_at=NULL, reason='refresh_queued', "
            "updated_at=?, next_section=?, acquisition_cursor=0" + revision_assignment + " WHERE " + predicate,
            (now, next_section, *revision_value, *parameters),
        ).rowcount

    def _write_detail(  # noqa: PLR0913
        self,
        entity_id: int,
        detail: Mapping[str, object],
        *,
        now: int,
        expected_revision: int,
        owner_account_id: int | None = None,
        observation_scope: Mapping[str, object] | None = None,
        ownership_observed: bool = False,
    ) -> bool:
        encoded_detail = json.dumps({"schema": _DETAIL_SCHEMA, **detail}, separators=(",", ":"))
        metadata_columns, metadata_values = self._detail_metadata(
            owner_account_id=owner_account_id,
            observation_scope=observation_scope,
            ownership_observed=ownership_observed,
        )
        encoded = _EncodedDetail(entity_id, encoded_detail, metadata_columns, metadata_values)
        if self._detail_has("profile_revision"):
            return self._write_fenced_detail(
                encoded,
                now=now,
                expected_revision=expected_revision,
            )
        return self._write_legacy_detail(entity_id, encoded_detail, metadata_columns, metadata_values, now=now)

    def _detail_metadata(
        self,
        *,
        owner_account_id: int | None,
        observation_scope: Mapping[str, object] | None,
        ownership_observed: bool,
    ) -> tuple[tuple[str, ...], tuple[object, ...]]:
        metadata: list[tuple[str, object]] = []
        if ownership_observed and self._detail_has("profile_owner_account_id"):
            metadata.append(("profile_owner_account_id", owner_account_id))
        if ownership_observed and self._detail_has("profile_observation_scope_json"):
            metadata.append(("profile_observation_scope_json", _encode_bounded_json(observation_scope)))
        return tuple(column for column, _value in metadata), tuple(value for _column, value in metadata)

    def _write_fenced_detail(
        self,
        detail: _EncodedDetail,
        *,
        now: int,
        expected_revision: int,
    ) -> bool:
        entity_id = detail.entity_id
        encoded_detail = detail.encoded
        metadata_columns = detail.metadata_columns
        metadata_values = detail.metadata_values
        assignments = "detail_json=?, fetched_at=?, profile_revision=profile_revision+1"
        if metadata_columns:
            assignments += ", " + ", ".join(f"{column}=?" for column in metadata_columns)
        changed = self._conn.execute(
            "UPDATE entity_details SET " + assignments + " WHERE entity_id=? AND profile_revision=?",
            (encoded_detail, now, *metadata_values, entity_id, expected_revision),
        ).rowcount
        if changed != 0:
            return True
        exists = self._conn.execute("SELECT 1 FROM entity_details WHERE entity_id=?", (entity_id,)).fetchone()
        if exists is not None:
            return False
        columns = ("entity_id", "detail_json", "fetched_at", "profile_revision", *metadata_columns)
        values: tuple[object, ...] = (entity_id, encoded_detail, now, 1, *metadata_values)
        self._conn.execute(
            f"INSERT INTO entity_details({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            values,
        )
        return True

    def _write_legacy_detail(
        self,
        entity_id: int,
        encoded_detail: str,
        metadata_columns: tuple[str, ...],
        metadata_values: tuple[object, ...],
        *,
        now: int,
    ) -> bool:
        columns = ("entity_id", "detail_json", "fetched_at", *metadata_columns)
        values = (entity_id, encoded_detail, now, *metadata_values)
        updates = "detail_json=excluded.detail_json, fetched_at=excluded.fetched_at"
        if metadata_columns:
            updates += ", " + ", ".join(f"{column}=excluded.{column}" for column in metadata_columns)
        self._conn.execute(
            f"INSERT INTO entity_details({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
            "ON CONFLICT(entity_id) DO UPDATE SET " + updates,
            values,
        )
        return True

    def _write_section(  # noqa: PLR0913
        self,
        entity_id: int,
        section: str,
        status: str,
        reason: str | None,
        payload: object | None,
        *,
        now: int,
        evidence: ProfileAcquisitionEvidence | None,
    ) -> bool:
        observed_at = self._section_observed_at(entity_id, section, status, evidence, now=now)
        columns = ["entity_id", "section", "status", "observed_at", "reason", "payload_json", "retry_at"]
        values: list[object] = [entity_id, section, status, observed_at, reason, _encode_payload(payload), None]
        evidence_columns, evidence_values = self._section_evidence_values(evidence)
        columns.extend(evidence_columns)
        values.extend(evidence_values)
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(
            f"{column}=excluded.{column}" for column in columns if column not in {"entity_id", "section"}
        )
        return (
            self._conn.execute(
                f"INSERT INTO entity_detail_sections({', '.join(columns)}) VALUES ({placeholders}) "
                f"ON CONFLICT(entity_id, section) DO UPDATE SET {updates}",
                values,
            ).rowcount
            == 1
        )

    def _section_observed_at(
        self,
        entity_id: int,
        section: str,
        status: str,
        evidence: ProfileAcquisitionEvidence | None,
        *,
        now: int,
    ) -> object | None:
        if evidence is not None and status in {"fresh", "not_applicable"}:
            observed_at: object | None = evidence.observation_at
        elif evidence is None and status in {"fresh", "not_applicable"}:
            observed_at = now
        else:
            observed_at = None
        if not _is_sparse_full_profile_evidence(section, evidence):
            return observed_at
        previous = self._conn.execute(
            "SELECT observed_at FROM entity_detail_sections WHERE entity_id=? AND section=?",
            (entity_id, section),
        ).fetchone()
        return previous[0] if previous is not None and previous[0] is not None else observed_at

    def _section_evidence_values(
        self, evidence: ProfileAcquisitionEvidence | None
    ) -> tuple[tuple[str, ...], tuple[object, ...]]:
        if not self._section_has("acquisition_generation"):
            return (), ()
        columns = (
            "acquisition_generation",
            "acquisition_outcome",
            "provenance_json",
            "normalization_version",
            "observation_started_at",
            "observation_completed_at",
            "acquisition_identity_json",
        )
        if evidence is None:
            return columns, (None,) * len(columns)
        return columns, (
            evidence.generation,
            evidence.outcome,
            _encode_bounded_json(evidence.provenance),
            evidence.normalization_version,
            evidence.observation_started_at,
            evidence.observation_completed_at,
            _encode_bounded_json(evidence.identity),
        )

    def commit_full_user_pair(
        self,
        cursor: EntityRefreshCursor,
        full_profile: EntitySectionCommit,
        personal_channel: EntitySectionCommit,
        *,
        now: int,
    ) -> bool:
        """Atomically commit the two ``GetFullUser`` projections.

        The refresh cursor advances only to ``common_chats``.  The personal
        channel row is already complete for this generation and is consumed
        locally when the ordered cursor reaches that section.
        """
        if cursor.next_section != "full_profile":
            return False
        self._validate_pair_commits(cursor, full_profile, personal_channel)
        with self._conn:
            if not self._cursor_matches(cursor):
                return False
            detail = self._pair_detail(cursor, full_profile, personal_channel, now=now)
            if detail is None:
                return False
            self._record_full_pair_measurement(cursor, full_profile, personal_channel, now=now)
            return self._advance_full_pair_cursor(cursor, now=now)

    def commit_group_full_chat_pair(
        self,
        cursor: EntityRefreshCursor,
        full_profile: EntitySectionCommit,
        contact_overlap: EntitySectionCommit,
        *,
        now: int,
    ) -> bool:
        """Atomically commit both legacy-group projections from one observation."""
        if cursor.next_section != "full_profile":
            return False
        self._validate_group_pair_commits(cursor, full_profile, contact_overlap)
        with self._conn:
            if not self._cursor_matches(cursor):
                return False
            detail = self._group_pair_detail(cursor, full_profile, contact_overlap, now=now)
            if detail is None:
                return False
            self._write_group_pair_sections(cursor, detail, full_profile, contact_overlap, now=now)
            advanced = self._advance_group_full_chat_cursor(cursor, now=now)
            if not advanced:
                raise sqlite3.OperationalError("legacy group cursor advance was rejected")
            return True

    def _group_pair_detail(
        self,
        cursor: EntityRefreshCursor,
        full_profile: EntitySectionCommit,
        contact_overlap: EntitySectionCommit,
        *,
        now: int,
    ) -> dict[str, object] | None:
        detail = self._read_detail_blob(cursor.entity_id) or self._read_entity_stub(cursor.entity_id)
        detail = _strip_schema(detail)
        detail.update(full_profile.detail_patch)
        detail.update({"common_chats": []})
        detail.update(contact_overlap.detail_patch)
        owner_account_id, observation_scope, ownership_observed = _commit_metadata(full_profile, contact_overlap)
        if not self._write_detail(
            cursor.entity_id,
            detail,
            now=now,
            expected_revision=cursor.profile_revision,
            owner_account_id=owner_account_id,
            observation_scope=observation_scope,
            ownership_observed=ownership_observed,
        ):
            return None
        return detail

    def _write_group_pair_sections(
        self,
        cursor: EntityRefreshCursor,
        detail: Mapping[str, object],
        full_profile: EntitySectionCommit,
        contact_overlap: EntitySectionCommit,
        *,
        now: int,
    ) -> None:
        writes: tuple[tuple[str, str, str | None, object | None, ProfileAcquisitionEvidence | None, str], ...] = (
            (
                "full_profile",
                full_profile.status,
                full_profile.reason,
                _section_payload(detail, "full_profile") if full_profile.payload is None else full_profile.payload,
                full_profile.evidence,
                "legacy group full profile write was rejected",
            ),
            (
                "common_chats",
                "not_applicable",
                "not_applicable",
                [],
                None,
                "legacy group common chats write was rejected",
            ),
            (
                "contact_overlap",
                contact_overlap.status,
                contact_overlap.reason,
                _section_payload(detail, "contact_overlap")
                if contact_overlap.payload is None
                else contact_overlap.payload,
                contact_overlap.evidence,
                "legacy group contact overlap write was rejected",
            ),
        )
        for section, status, reason, payload, evidence, error in writes:
            if not self._write_section(
                cursor.entity_id,
                section,
                status,
                reason,
                payload,
                now=now,
                evidence=evidence,
            ):
                raise sqlite3.OperationalError(error)

    @staticmethod
    def _validate_group_pair_commits(
        cursor: EntityRefreshCursor,
        full_profile: EntitySectionCommit,
        contact_overlap: EntitySectionCommit,
    ) -> None:
        for commit in (full_profile, contact_overlap):
            if commit.status not in {"fresh", "unavailable"}:
                raise ValueError("invalid legacy group section status")
            if commit.evidence is not None and commit.evidence.generation != cursor.generation:
                raise ValueError("legacy group evidence generation does not match refresh cursor")

    def _advance_group_full_chat_cursor(self, cursor: EntityRefreshCursor, *, now: int) -> bool:
        predicate, parameters = self._cursor_predicate(cursor)
        assignments = (
            "status='pending', retry_at=NULL, reason='refresh_queued', updated_at=?, "
            "next_section=?, acquisition_cursor=0"
        )
        values: tuple[object, ...] = (now, PROFILE_SECTIONS[3])
        if self._refresh_has("profile_revision") and self._detail_has("profile_revision"):
            assignments += ", profile_revision=?"
            values += (cursor.profile_revision + 1,)
        return (
            self._conn.execute(
                "UPDATE entity_profile_refresh_state SET " + assignments + " WHERE " + predicate,
                (*values, *parameters),
            ).rowcount
            == 1
        )

    def _validate_pair_commits(
        self,
        cursor: EntityRefreshCursor,
        full_profile: EntitySectionCommit,
        personal_channel: EntitySectionCommit,
    ) -> None:
        for commit in (full_profile, personal_channel):
            if commit.status not in {"fresh", "unavailable", "not_applicable"}:
                raise ValueError("invalid terminal section status")
            if commit.evidence is not None and commit.evidence.generation != cursor.generation:
                raise ValueError("pair evidence generation does not match refresh cursor")

    def _pair_detail(
        self,
        cursor: EntityRefreshCursor,
        full_profile: EntitySectionCommit,
        personal_channel: EntitySectionCommit,
        *,
        now: int,
    ) -> dict[str, object] | None:
        detail = self._read_detail_blob(cursor.entity_id) or self._read_entity_stub(cursor.entity_id)
        detail = _strip_schema(detail)
        detail.update(full_profile.detail_patch)
        detail.update(personal_channel.detail_patch)
        owner_account_id, observation_scope, ownership_observed = _commit_metadata(full_profile, personal_channel)
        if not self._write_detail(
            cursor.entity_id,
            detail,
            now=now,
            expected_revision=cursor.profile_revision,
            owner_account_id=owner_account_id,
            observation_scope=observation_scope,
            ownership_observed=ownership_observed,
        ):
            return None
        self._write_pair_section(cursor.entity_id, "full_profile", full_profile, detail, now=now)
        self._write_pair_section(cursor.entity_id, "personal_channel", personal_channel, detail, now=now)
        return detail

    def _write_pair_section(
        self,
        entity_id: int,
        section: str,
        commit: EntitySectionCommit,
        detail: Mapping[str, object],
        *,
        now: int,
    ) -> None:
        payload = _section_payload(detail, section) if commit.payload is None else commit.payload
        self._write_section(
            entity_id, section, commit.status, commit.reason, payload, now=now, evidence=commit.evidence
        )

    def _record_full_pair_measurement(
        self,
        cursor: EntityRefreshCursor,
        full_profile: EntitySectionCommit,
        personal_channel: EntitySectionCommit,
        *,
        now: int,
    ) -> None:
        required = {
            "pair_mode",
            "pair_eligible",
            "pair_full_profile_outcome",
            "pair_personal_channel_outcome",
            "pair_ready_at",
            "pair_readiness_latency_ms",
            "pair_measurement_complete",
            "pair_summary_watermark",
        }
        if not cursor.pair_eligible or not required <= self._refresh_columns_or_empty():
            return
        full_outcome = _pair_commit_outcome(full_profile)
        personal_outcome = _pair_commit_outcome(personal_channel)
        self._conn.execute(
            "UPDATE entity_profile_refresh_state SET pair_mode=COALESCE(pair_mode, 'enabled'), "
            "pair_full_profile_outcome=?, pair_personal_channel_outcome=?, pair_ready_at=?, "
            "pair_readiness_latency_ms=?, pair_measurement_complete=1, pair_summary_watermark=? "
            "WHERE entity_id=? AND generation=? AND pair_summary_watermark IS NULL",
            (
                full_outcome,
                personal_outcome,
                now,
                _pair_readiness_latency_ms(full_profile, personal_channel),
                now,
                cursor.entity_id,
                cursor.generation,
            ),
        )

    def _advance_full_pair_cursor(self, cursor: EntityRefreshCursor, *, now: int) -> bool:
        predicate, parameters = self._cursor_predicate(cursor)
        assignments = (
            "status='pending', retry_at=NULL, reason='refresh_queued', updated_at=?, "
            "next_section=?, acquisition_cursor=0"
        )
        values: tuple[object, ...] = (now, PROFILE_SECTIONS[1])
        if self._refresh_has("profile_revision") and self._detail_has("profile_revision"):
            assignments += ", profile_revision=?"
            values += (cursor.profile_revision + 1,)
        changed = self._conn.execute(
            "UPDATE entity_profile_refresh_state SET " + assignments + " WHERE " + predicate,
            (*values, *parameters),
        ).rowcount
        return changed == 1

    def request_follow_up(self, entity_id: int, *, now: int) -> bool:
        """Record a new freshness demand without losing the active generation."""
        if not self._refresh_has("follow_up_required"):
            return False
        with self._conn:
            changed = self._conn.execute(
                "UPDATE entity_profile_refresh_state SET follow_up_required=1, updated_at=? "
                "WHERE entity_id=? AND status IN ('pending', 'failed')",
                (now, entity_id),
            ).rowcount
            if changed == 1:
                return True
            # A completed state is retained by v61 so the next demand can
            # start a fresh, never-reused generation immediately.
            changed = self._conn.execute(
                "UPDATE entity_profile_refresh_state SET status='pending', retry_at=NULL, "
                "reason='refresh_follow_up', updated_at=?, follow_up_required=0, "
                "generation=generation+1, started_at=?, pair_eligible=?, next_section=?, acquisition_cursor=0 "
                "WHERE entity_id=? AND status='complete'",
                (now, now, int(self._pair_is_eligible(entity_id, now=now)), PROFILE_SECTIONS[0], entity_id),
            ).rowcount
            if changed == 1:
                self._reset_pair_measurement(entity_id)
            return changed == 1

    def _refresh_follow_up_required(self, cursor: EntityRefreshCursor) -> bool:
        row = self._conn.execute(
            "SELECT follow_up_required FROM entity_profile_refresh_state WHERE entity_id=?",
            (cursor.entity_id,),
        ).fetchone()
        return bool(row and row[0])

    def _start_follow_up_generation(
        self,
        cursor: EntityRefreshCursor,
        *,
        now: int,
        profile_revision: int | None = None,
    ) -> int:
        predicate, parameters = self._cursor_predicate(cursor)
        revision_assignment = ""
        revision_value: tuple[object, ...] = ()
        if (
            profile_revision is not None
            and self._refresh_has("profile_revision")
            and self._detail_has("profile_revision")
        ):
            revision_assignment = ", profile_revision=?"
            revision_value = (profile_revision,)
        changed = self._conn.execute(
            "UPDATE entity_profile_refresh_state SET status='pending', retry_at=NULL, "
            "reason='refresh_follow_up', updated_at=?, generation=generation+1, started_at=?, "
            "pair_eligible=?, follow_up_required=0, next_section=?, acquisition_cursor=0" + revision_assignment + " "
            "WHERE " + predicate,
            (
                now,
                now,
                int(self._pair_is_eligible(cursor.entity_id, now=now)),
                PROFILE_SECTIONS[0],
                *revision_value,
                *parameters,
            ),
        ).rowcount
        if changed == 1:
            self._reset_pair_measurement(cursor.entity_id)
        return changed

    def _reset_pair_measurement(self, entity_id: int) -> None:
        columns = self._columns("entity_profile_refresh_state")
        assignments: list[str] = []
        assignments.extend(
            f"{column}=NULL"
            for column in (
                "pair_full_profile_outcome",
                "pair_personal_channel_outcome",
                "pair_ready_at",
                "pair_readiness_latency_ms",
                "pair_summary_watermark",
            )
            if column in columns
        )
        assignments.extend(
            f"{column}=0"
            for column in (
                "pair_full_profile_attempts",
                "pair_personal_channel_attempts",
                "pair_full_profile_retries",
                "pair_personal_channel_retries",
                "pair_attempts",
                "pair_retries",
                "pair_measurement_complete",
            )
            if column in columns
        )
        if assignments:
            self._conn.execute(
                "UPDATE entity_profile_refresh_state SET " + ", ".join(assignments) + " WHERE entity_id=?",
                (entity_id,),
            )

    def _advance_completed_section(self, cursor: EntityRefreshCursor, *, now: int) -> bool:
        next_section = _next_profile_section(cursor.next_section)
        predicate, parameters = self._cursor_predicate(cursor)
        if next_section is None:
            assignments = "status='complete', retry_at=NULL, reason='refresh_complete', updated_at=?"
            values: tuple[object, ...] = (now,)
        else:
            assignments = "status='pending', retry_at=NULL, reason='refresh_queued', updated_at=?, next_section=?, acquisition_cursor=0"
            values = (now, next_section)
        if self._refresh_has("profile_revision") and next_section is not None:
            # No canonical write happened while reusing same-generation
            # evidence, so the revision fence remains unchanged.
            pass
        return (
            self._conn.execute(
                "UPDATE entity_profile_refresh_state SET " + assignments + " WHERE " + predicate,
                (*values, *parameters),
            ).rowcount
            == 1
        )

    def _cursor_matches(self, cursor: EntityRefreshCursor) -> bool:
        predicate, parameters = self._cursor_predicate(cursor)
        row = self._conn.execute(f"SELECT 1 FROM entity_profile_refresh_state WHERE {predicate}", parameters).fetchone()
        return row is not None

    def _cursor_predicate(self, cursor: EntityRefreshCursor) -> tuple[str, tuple[object, ...]]:
        clauses = [
            "entity_id=?",
            "status IN ('pending', 'failed')",
            "next_section=?",
            "acquisition_cursor=?",
        ]
        parameters: list[object] = [cursor.entity_id, cursor.next_section, cursor.acquisition_cursor]
        if self._refresh_has("generation"):
            clauses.append("generation=?")
            parameters.append(cursor.generation)
        if self._refresh_has("profile_revision"):
            clauses.append("profile_revision=?")
            parameters.append(cursor.profile_revision)
        return " AND ".join(clauses), tuple(parameters)

    def mark_refresh_rejected(self, entity_id: int, *, now: int, reason: str = "refresh_rejected") -> None:
        """Persist queue rejection without claiming that work was queued."""
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO entity_profile_refresh_state(entity_id, status, retry_at, reason, updated_at) "
                    "VALUES (?, 'rejected', NULL, ?, ?) ON CONFLICT(entity_id) DO UPDATE SET status=excluded.status, "
                    "retry_at=NULL, reason=excluded.reason, updated_at=excluded.updated_at",
                    (entity_id, reason, now),
                )
                self._conn.executemany(
                    "INSERT OR IGNORE INTO entity_detail_sections(entity_id, section, status, observed_at, reason, payload_json, retry_at) "
                    "VALUES (?, ?, 'unavailable', NULL, ?, NULL, NULL)",
                    ((entity_id, section, reason) for section in PROFILE_SECTIONS),
                )
                self._conn.execute(
                    "UPDATE entity_detail_sections SET status='unavailable', reason=?, retry_at=NULL "
                    "WHERE entity_id=? AND status <> 'not_applicable'",
                    (reason, entity_id),
                )
        except sqlite3.OperationalError:
            return

    def mark_section_failure(
        self,
        cursor: EntityRefreshCursor,
        *,
        now: int,
        reason: str,
        retry_at: int,
    ) -> bool:
        """Atomically defer only the section owned by the current durable cursor."""
        with self._conn:
            predicate, parameters = self._cursor_predicate(cursor)
            changed = self._conn.execute(
                "UPDATE entity_profile_refresh_state SET status='failed', retry_at=?, reason=?, updated_at=? "
                "WHERE " + predicate,
                (retry_at, reason, now, *parameters),
            ).rowcount
            if changed != 1:
                return False
            self._conn.execute(
                """
                INSERT INTO entity_detail_sections(
                    entity_id, section, status, observed_at, reason, payload_json, retry_at
                ) VALUES (?, ?, 'unavailable', NULL, ?, NULL, ?)
                ON CONFLICT(entity_id, section) DO UPDATE SET
                    status=CASE WHEN entity_detail_sections.observed_at IS NULL THEN 'unavailable' ELSE 'stale' END,
                    reason=excluded.reason, retry_at=excluded.retry_at
                """,
                (cursor.entity_id, cursor.next_section, reason, retry_at),
            )
        return True

    def mark_refresh_failure(
        self,
        entity_id: int,
        *,
        now: int,
        reason: str,
        retry_at: int | None = None,
    ) -> None:
        """Record failure while preserving payload and its last successful timestamp."""
        try:
            try:
                self._conn.execute(
                    "INSERT INTO entity_profile_refresh_state(entity_id, status, retry_at, reason, updated_at) "
                    "VALUES (?, 'failed', ?, ?, ?) ON CONFLICT(entity_id) DO UPDATE SET status=excluded.status, "
                    "retry_at=excluded.retry_at, reason=excluded.reason, updated_at=excluded.updated_at",
                    (entity_id, retry_at, reason, now),
                )
            except sqlite3.OperationalError:
                pass
            self._conn.execute(
                "UPDATE entity_detail_sections SET status=CASE WHEN observed_at IS NULL "
                "THEN 'unavailable' ELSE 'stale' END, reason=?, retry_at=? "
                "WHERE entity_id=? AND status <> 'not_applicable'",
                (reason, retry_at, entity_id),
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            return

    def _read_sections(
        self,
        entity_id: int,
        detail: Mapping[str, object],
        *,
        now: int,
        observed_at: int | None,
    ) -> dict[str, dict[str, object]]:
        by_name = self._read_stored_sections(entity_id)
        output: dict[str, dict[str, object]] = {}
        for section in PROFILE_SECTIONS:
            section_name, value = self._read_section(
                entity_id, section, by_name, detail, now=now, observed_at=observed_at
            )
            output[section_name] = value
        return output

    def _read_stored_sections(self, entity_id: int) -> dict[str, tuple[str, str, int | None, str | None, str | None]]:
        try:
            rows = cast(
                list[tuple[str, str, int | None, str | None, str | None]],
                self._conn.execute(
                    "SELECT section, status, observed_at, reason, payload_json "
                    "FROM entity_detail_sections WHERE entity_id = ?",
                    (entity_id,),
                ).fetchall(),
            )
        except sqlite3.OperationalError:
            rows = []
        return {row[0]: row for row in rows}

    def _read_section(  # noqa: PLR0913
        self,
        entity_id: int,
        section: str,
        stored: Mapping[str, tuple[str, str, int | None, str | None, str | None]],
        detail: Mapping[str, object],
        *,
        now: int,
        observed_at: int | None,
    ) -> tuple[str, dict[str, object]]:
        row = stored.get(section)
        if row is None:
            payload = _section_payload(detail, section)
            status = self._legacy_section_status(detail, section, payload, observed_at, now=now)
            return section, {
                "status": status,
                "observed_at": observed_at if payload is not None else None,
                "reason": None if payload is not None else "refresh_queued",
                "data": payload,
            }
        section_name, raw_status, section_observed_at, reason, payload_json = row
        payload = _decode_payload(payload_json)
        status = self._stored_section_status(raw_status, section_observed_at, now=now)
        result: dict[str, object] = {
            "status": status,
            "observed_at": section_observed_at,
            "reason": reason,
            "data": payload,
        }
        evidence = self._read_section_evidence(entity_id=entity_id, section=section_name)
        if evidence:
            result["evidence"] = evidence
        return section_name, result

    def _read_section_evidence(self, *, entity_id: int, section: str) -> dict[str, object] | None:
        if not self._section_has("acquisition_generation") or entity_id == 0:
            return None
        row = self._conn.execute(
            "SELECT acquisition_generation, acquisition_outcome, provenance_json, normalization_version, "
            "observation_started_at, observation_completed_at, acquisition_identity_json "
            "FROM entity_detail_sections WHERE entity_id=? AND section=?",
            (entity_id, section),
        ).fetchone()
        if row is None or not _is_nonnegative_int(row[0]):
            return None
        return {
            "generation": row[0],
            "outcome": row[1],
            "provenance": _decode_payload(row[2]),
            "normalization_version": row[3],
            "observation_started_at": row[4],
            "observation_completed_at": row[5],
            "identity": _decode_payload(row[6]),
        }

    def _parse_validated_receipt(
        self,
        entity_id: int,
        section: str,
        *,
        identity: Mapping[str, object],
        now: int,
        expected_generation: int | None = None,
    ) -> _ValidatedReceipt | None:
        """Parse one receipt and validate its immutable evidence and payload."""
        if not isinstance(identity, Mapping) or not identity:
            return None
        evidence = self.read_section_evidence(entity_id, section)
        if evidence is None:
            return None
        fields = _validated_receipt_fields(
            evidence,
            identity=identity,
            expected_generation=expected_generation,
            now=now,
        )
        if fields is None:
            return None
        generation, observation, outcome = fields
        if not self._receipt_materialization_is_exact(entity_id, section, evidence):
            return None
        started_at, completed_at = observation
        return _ValidatedReceipt(outcome, generation, started_at, completed_at)

    def _reusable_receipt_is_valid(
        self,
        entity_id: int,
        section: str,
        *,
        identity: Mapping[str, object],
        now: int,
        ttl_seconds: int | None = None,
    ) -> bool:
        """Accept only a usable/absent fresh receipt within its original TTL."""
        receipt = self._parse_validated_receipt(entity_id, section, identity=identity, now=now)
        if receipt is None or receipt.outcome not in {"usable", "absent"}:
            return False
        row = self._receipt_section_state(entity_id, section)
        if row is None or row[0] != "fresh" or row[1] != receipt.observation_started_at:
            return False
        ttl = self._section_ttl_seconds if ttl_seconds is None else max(1, int(ttl_seconds))
        return now < receipt.observation_started_at + ttl

    def _same_generation_completion_receipt_is_valid(
        self,
        entity_id: int,
        section: str,
        *,
        identity: Mapping[str, object],
        now: int,
        expected_generation: int,
    ) -> bool:
        """Accept any terminal outcome from the cursor's generation without TTL."""
        receipt = self._parse_validated_receipt(
            entity_id,
            section,
            identity=identity,
            now=now,
            expected_generation=expected_generation,
        )
        if receipt is None:
            return False
        row = self._receipt_section_state(entity_id, section)
        return row is not None and row[0] in {"fresh", "stale", "unavailable"}

    def _receipt_section_state(self, entity_id: int, section: str) -> tuple[object, ...] | None:
        return self._conn.execute(
            "SELECT status, observed_at FROM entity_detail_sections WHERE entity_id=? AND section=?",
            (entity_id, section),
        ).fetchone()

    def _receipt_materialization_is_exact(
        self,
        entity_id: int,
        section: str,
        evidence: Mapping[str, object],
    ) -> bool:
        expected_fields = {
            "full_profile": FULL_PROFILE_OWNED_FIELDS,
            "personal_channel": PERSONAL_CHANNEL_OWNED_FIELDS,
        }.get(section)
        if expected_fields is None:
            return False
        provenance = evidence.get("provenance")
        if not self._receipt_provenance_is_exact(section, expected_fields, evidence, provenance):
            return False
        if not isinstance(provenance, dict):
            return False
        payload_row = self._conn.execute(
            "SELECT payload_json FROM entity_detail_sections WHERE entity_id=? AND section=?",
            (entity_id, section),
        ).fetchone()
        if payload_row is None:
            return False
        if section == "personal_channel" and evidence.get("outcome") == "absent":
            return provenance.get("authoritative") is True and payload_row[0] is None
        return isinstance(_decode_payload(payload_row[0]), dict)

    def _receipt_provenance_is_exact(
        self,
        section: str,
        expected_fields: tuple[str, ...] | list[str],
        evidence: Mapping[str, object],
        provenance: object,
    ) -> bool:
        if evidence.get("normalization_version") != NORMALIZATION_VERSION:
            return False
        if not isinstance(provenance, dict) or provenance.get("endpoint") != FULL_USER_ENDPOINT:
            return False
        if not _provenance_fields_are_exact(provenance, expected_fields):
            return False
        authoritative = provenance.get("authoritative")
        if not isinstance(authoritative, bool):
            return False
        return not (
            section == "full_profile" and authoritative is not True and self._detail_has("profile_owner_account_id")
        )

    def _legacy_section_status(
        self,
        detail: Mapping[str, object],
        section: str,
        payload: object,
        observed_at: int | None,
        *,
        now: int,
    ) -> str:
        if not _section_applicable(detail, section):
            return "not_applicable"
        if payload is None or observed_at is None:
            return "pending"
        return "stale" if now - observed_at >= self._section_ttl_seconds else "fresh"

    def _stored_section_status(self, raw_status: str, observed_at: int | None, *, now: int) -> str:
        status = raw_status if raw_status in _SECTION_STATUSES else "pending"
        if status == "fresh" and observed_at is not None and now - observed_at >= self._section_ttl_seconds:
            return "stale"
        return status

    def _read_detail_blob(self, entity_id: int) -> dict[str, object]:
        try:
            row = cast(
                tuple[object, ...] | None,
                self._conn.execute(
                    "SELECT detail_json FROM entity_details WHERE entity_id = ?",
                    (entity_id,),
                ).fetchone(),
            )
        except sqlite3.OperationalError:
            return {}
        if row is None:
            return {}
        try:
            value = cast(object, json.loads(str(row[0])))
        except TypeError, json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}


def _observation_bounds(evidence: Mapping[str, object]) -> tuple[int | None, int | None]:
    started_at = evidence.get("observation_started_at")
    completed_at = evidence.get("observation_completed_at")
    if not _is_nonnegative_int(started_at) or not _is_nonnegative_int(completed_at):
        return None, None
    return cast(int, started_at), cast(int, completed_at)


def _receipt_generation(evidence: Mapping[str, object], *, expected_generation: int | None) -> int | None:
    generation = evidence.get("generation")
    if not _is_nonnegative_int(generation):
        return None
    if expected_generation is not None and generation != expected_generation:
        return None
    return cast(int, generation)


def _validated_receipt_fields(
    evidence: Mapping[str, object],
    *,
    identity: Mapping[str, object],
    expected_generation: int | None,
    now: int,
) -> tuple[int, tuple[int, int], str] | None:
    if not _receipt_identity_matches(evidence, identity):
        return None
    generation = _receipt_generation(evidence, expected_generation=expected_generation)
    if generation is None:
        return None
    observation = _receipt_observation(evidence, now=now)
    if observation is None:
        return None
    outcome = _receipt_outcome(evidence)
    if outcome is None:
        return None
    return generation, observation, outcome


def _receipt_identity_matches(evidence: Mapping[str, object], identity: Mapping[str, object]) -> bool:
    stored_identity = evidence.get("identity")
    return isinstance(stored_identity, dict) and dict(identity) == stored_identity


def _receipt_observation(evidence: Mapping[str, object], *, now: int) -> tuple[int, int] | None:
    started_at, completed_at = _observation_bounds(evidence)
    if started_at is None or completed_at is None or completed_at < started_at or completed_at > now:
        return None
    return started_at, completed_at


def _receipt_outcome(evidence: Mapping[str, object]) -> str | None:
    outcome = evidence.get("outcome")
    valid_outcomes = {"usable", "partial", "absent", "unavailable"}
    return outcome if isinstance(outcome, str) and outcome in valid_outcomes else None


def _decode_profile_blob_row(
    row: tuple[object, ...] | None,
) -> tuple[dict[str, object], int | None, int | None, dict[str, object] | None]:
    if row is None:
        return {}, None, None, None
    raw_json, fetched_at, *metadata = row
    if not isinstance(raw_json, str):
        return {}, None, None, None
    try:
        parsed = cast(object, json.loads(raw_json))
    except TypeError, json.JSONDecodeError:
        return {}, None, None, None
    if not isinstance(parsed, dict) or parsed.get("schema") != _DETAIL_SCHEMA:
        return {}, None, None, None
    if not _is_nonnegative_int(fetched_at):
        return {}, None, None, None
    owner = _profile_owner(metadata)
    scope = _decode_mapping(metadata[1]) if len(metadata) > 1 else None
    return {str(key): value for key, value in parsed.items() if key != "schema"}, cast(int, fetched_at), owner, scope


def _profile_owner(metadata: list[object]) -> int | None:
    if not metadata or not _is_nonnegative_int(metadata[0]) or cast(int, metadata[0]) <= 0:
        return None
    return cast(int, metadata[0])


def _as_int(value: object | None) -> int:
    return int(cast(int | str, value or 0))


def _pending_refresh_is_active(existing: Mapping[str, object] | None) -> bool:
    return existing is not None and str(existing.get("status")) == "pending" and _as_int(existing.get("generation")) > 0


def _active_pending_values(
    existing: Mapping[str, object],
    *,
    pair_mode_override: str | None,
) -> dict[str, object]:
    return {
        "generation": _as_int(existing["generation"]),
        "started_at": existing.get("started_at"),
        "pair_eligible": _as_int(existing.get("pair_eligible")),
        "follow_up_required": _as_int(existing.get("follow_up_required")),
        "profile_revision": _as_int(existing.get("profile_revision")),
        "pair_mode": existing.get("pair_mode") if pair_mode_override is None else pair_mode_override,
    }


def _provenance_fields_are_exact(
    provenance: Mapping[str, object], expected_fields: tuple[str, ...] | list[str]
) -> bool:
    declared = provenance.get("declared_fields")
    materialized = provenance.get("materialized_fields")
    if declared != list(expected_fields) or not isinstance(materialized, list):
        return False
    if any(not isinstance(value, str) for value in materialized):
        return False
    return len(set(materialized)) == len(materialized) and set(materialized) <= set(expected_fields)


def _pending_refresh_upsert_query(columns: list[str]) -> str:
    placeholders = ", ".join("?" for _ in columns)
    updates = [
        "status='pending'",
        "retry_at=NULL",
        "reason=excluded.reason",
        "updated_at=excluded.updated_at",
        "next_section=CASE WHEN entity_profile_refresh_state.status='pending' AND entity_profile_refresh_state.next_section IS NOT NULL THEN entity_profile_refresh_state.next_section ELSE excluded.next_section END",
        "acquisition_cursor=CASE WHEN entity_profile_refresh_state.status='pending' AND entity_profile_refresh_state.next_section IS NOT NULL THEN entity_profile_refresh_state.acquisition_cursor ELSE 0 END",
    ]
    updates.extend(
        [
            (
                "pair_mode=CASE WHEN entity_profile_refresh_state.status='pending' "
                "AND entity_profile_refresh_state.generation=excluded.generation "
                "THEN COALESCE(entity_profile_refresh_state.pair_mode, excluded.pair_mode) "
                "ELSE excluded.pair_mode END"
                if column == "pair_mode"
                else f"{column}=excluded.{column}"
            )
            for column in columns[7:]
        ]
    )
    return (
        f"INSERT INTO entity_profile_refresh_state({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(entity_id) DO UPDATE SET {', '.join(updates)}"
    )


def _pair_summary_payload(
    mode: str,
    current: tuple[object, ...],
    *,
    readiness_latency_ms: object,
    ready: bool,
) -> dict[str, object]:
    attempts = int(cast(int | str, current[3] or 0))
    retries = int(cast(int | str, current[4] or 0))
    return {
        "mode": mode,
        "eligible_pair": True,
        "outcome": "committed",
        "actual_attempts": attempts,
        "retries": retries,
        "full_profile_outcome": str(current[0]),
        "personal_channel_outcome": str(current[1]),
        "pair_ready": ready,
        "pair_readiness_latency_ms": readiness_latency_ms,
    }


def _pair_commit_outcome(commit: EntitySectionCommit) -> str:
    return (
        commit.evidence.outcome
        if commit.evidence is not None and commit.evidence.outcome in {"usable", "partial", "absent", "unavailable"}
        else "unavailable"
    )


def _is_sparse_full_profile_evidence(
    section: str,
    evidence: ProfileAcquisitionEvidence | None,
) -> bool:
    return (
        evidence is not None
        and section == "full_profile"
        and isinstance(evidence.provenance, Mapping)
        and evidence.provenance.get("authoritative") is False
    )


def _normalise_entity_type(value: str) -> str:
    return DialogType.parse(value).value


def _next_profile_section(section: str) -> str | None:
    try:
        index = PROFILE_SECTIONS.index(section)
    except ValueError:
        return PROFILE_SECTIONS[0]
    next_index = index + 1
    return PROFILE_SECTIONS[next_index] if next_index < len(PROFILE_SECTIONS) else None


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _section_payload(detail: Mapping[str, object], section: str) -> object | None:
    if section == "common_chats":
        return detail.get("common_chats")
    if section == "contact_overlap":
        return detail.get("contacts_subscribed")
    if section == "avatar_history":
        return detail.get("avatar_history")
    if section == "personal_channel":
        return detail.get("personal_channel")
    return dict(detail)


def _strip_schema(detail: Mapping[str, object]) -> dict[str, object]:
    return {str(key): value for key, value in detail.items() if key != "schema"}


def _section_applicable(detail: Mapping[str, object], section: str) -> bool:
    raw_type = detail.get("type")
    entity_type = DialogType.parse(raw_type if isinstance(raw_type, str) else None)
    if section == "personal_channel":
        return entity_type in {DialogType.USER, DialogType.BOT}
    if section == "common_chats":
        return entity_type in {DialogType.USER, DialogType.BOT, DialogType.SERVICE}
    if section == "contact_overlap":
        return entity_type in {DialogType.CHANNEL, DialogType.SUPERGROUP, DialogType.FORUM, DialogType.GROUP}
    if section == "avatar_history":
        return entity_type in {
            DialogType.USER,
            DialogType.BOT,
            DialogType.CHANNEL,
            DialogType.SUPERGROUP,
            DialogType.FORUM,
            DialogType.GROUP,
        }
    return True


def _encode_payload(value: object | None) -> str | None:
    return None if value is None else json.dumps(value, separators=(",", ":"))


def _decode_payload(value: str | None) -> object | None:
    if value is None:
        return None
    try:
        return cast(object, json.loads(value))
    except json.JSONDecodeError:
        return None


def _decode_mapping(value: object) -> dict[str, object] | None:
    decoded = _decode_payload(value if isinstance(value, str) else None)
    return {str(key): item for key, item in decoded.items()} if isinstance(decoded, dict) else None


def _section_due(status: str, observed_at: int | None, *, now: int, ttl: int) -> bool:
    if status in {"pending", "stale", "unavailable"}:
        return True
    return status == "fresh" and (observed_at is None or now - observed_at >= ttl)


def _encode_bounded_json(value: Mapping[str, object] | None) -> str | None:
    if value is None:
        return None
    encoded = json.dumps(dict(value), separators=(",", ":"), sort_keys=True)
    if len(encoded) > _EVIDENCE_MAX_JSON_BYTES:
        raise ValueError("profile acquisition evidence exceeds bounded size")
    return encoded


def _commit_metadata(
    *commits: EntitySectionCommit,
) -> tuple[int | None, Mapping[str, object] | None, bool]:
    observed = [commit for commit in commits if commit.ownership_observed]
    if not observed:
        return None, None, False
    first = observed[0]
    return first.observation_owner_account_id, first.observation_auth_scope, True


def _pair_readiness_latency_ms(
    full_profile: EntitySectionCommit,
    personal_channel: EntitySectionCommit,
) -> float | None:
    evidences = [commit.evidence for commit in (full_profile, personal_channel) if commit.evidence is not None]
    starts = [evidence.observation_started_at for evidence in evidences]
    completes = [evidence.observation_completed_at for evidence in evidences]
    if not starts or any(value is None for value in starts + completes):
        return None
    return float(max(cast(list[int], completes)) - min(cast(list[int], starts))) * 1000.0
