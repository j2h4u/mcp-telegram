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
from .contracts import PROFILE_SECTIONS, ProfileAcquisitionEvidence
from .full_user_normalization import (
    FULL_PROFILE_OWNED_FIELDS,
    FULL_USER_ENDPOINT,
    NORMALIZATION_VERSION,
    PERSONAL_CHANNEL_OWNED_FIELDS,
)

_DETAIL_SCHEMA = 1
_BASE_CURSOR_FIELD_COUNT = 4
_EVIDENCE_MAX_JSON_BYTES = 4096
_SECTION_STATUSES = {"fresh", "stale", "pending", "unavailable", "not_applicable"}


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True, slots=True)
class StoredProfile:
    detail: dict[str, object]
    observed_at: int | None
    sections: dict[str, dict[str, object]]


@dataclass(frozen=True, slots=True)
class EntityRefreshCursor:
    entity_id: int
    next_section: str
    acquisition_cursor: int
    retry_at: int | None
    generation: int = 0
    started_at: int | None = None
    pair_eligible: bool = False
    follow_up_required: bool = False
    profile_revision: int = 0


@dataclass(frozen=True, slots=True)
class EntitySectionCommit:
    detail_patch: Mapping[str, object]
    status: str = "fresh"
    reason: str | None = None
    payload: object | None = None
    evidence: ProfileAcquisitionEvidence | None = None


class EntityProfileRepository:
    """Read and write entity profiles without making Telegram calls."""

    def __init__(self, conn: sqlite3.Connection, *, section_ttl_seconds: int) -> None:
        self._conn = conn
        self._section_ttl_seconds = max(1, int(section_ttl_seconds))
        self._refresh_columns: set[str] | None = None
        self._section_columns: set[str] | None = None
        self._detail_columns: set[str] | None = None

    def _columns(self, table: str, attribute: str) -> set[str]:
        columns = getattr(self, attribute)
        if columns is None:
            try:
                columns = {
                    str(row[1])
                    for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
            except sqlite3.OperationalError:
                columns = set()
            setattr(self, attribute, columns)
        return columns

    def _refresh_has(self, column: str) -> bool:
        return column in self._columns("entity_profile_refresh_state", "_refresh_columns")

    def _section_has(self, column: str) -> bool:
        return column in self._columns("entity_detail_sections", "_section_columns")

    def _detail_has(self, column: str) -> bool:
        return column in self._columns("entity_details", "_detail_columns")

    def read(self, entity_id: int, *, now: int) -> StoredProfile | None:
        detail, observed_at = self._read_primary_detail(entity_id)
        if not detail:
            return None
        sections = self._read_sections(entity_id, detail, now=now, observed_at=observed_at)
        return StoredProfile(detail=detail, observed_at=observed_at, sections=sections)

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
        return self._receipt_is_valid(
            entity_id,
            section,
            identity=identity,
            now=now,
            ttl_seconds=ttl_seconds,
            positive_only=True,
        )

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
                self._receipt_is_valid(
                    cursor.entity_id,
                    section,
                    identity=identity,
                    now=now,
                    positive_only=True,
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

    def full_user_pair_reuse_rejection_reason(  # noqa: PLR0911, PLR0912
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
            evidence = self.read_section_evidence(cursor.entity_id, section)
            if evidence is None:
                return "missing_receipt"
            if evidence.get("outcome") not in {"usable", "absent"}:
                return "outcome_not_reusable"
            stored_identity = evidence.get("identity")
            if not isinstance(stored_identity, dict) or dict(identity) != stored_identity:
                return "identity_mismatch"
            if not self._receipt_materialization_is_exact(cursor.entity_id, section, evidence):
                return "materialization_mismatch"
            started_at = evidence.get("observation_started_at")
            completed_at = evidence.get("observation_completed_at")
            if not _is_nonnegative_int(started_at) or not _is_nonnegative_int(completed_at):
                return "invalid_observation"
            started_at_int = cast(int, started_at)
            completed_at_int = cast(int, completed_at)
            if completed_at_int < started_at_int or completed_at_int > now:
                return "invalid_observation"
            row = self._conn.execute(
                "SELECT status, observed_at FROM entity_detail_sections WHERE entity_id=? AND section=?",
                (cursor.entity_id, section),
            ).fetchone()
            if row is None or row[0] != "fresh":
                return "section_not_fresh"
            if row[1] != started_at_int or now >= started_at_int + self._section_ttl_seconds:
                return "stale"
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
            not self._receipt_is_valid(
                entity_id,
                section,
                identity=identity,
                now=now,
                positive_only=True,
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
            return self._complete_same_generation_without_scope(cursor, now=now)
        with self._conn:
            if not self._cursor_matches(cursor):
                return False
            if not self._receipt_is_valid(
                cursor.entity_id,
                "personal_channel",
                identity=identity,
                now=now,
                positive_only=False,
                expected_generation=cursor.generation,
            ):
                return False
            return self._advance_completed_section(cursor, now=now)

    def _complete_same_generation_without_scope(self, cursor: EntityRefreshCursor, *, now: int) -> bool:
        """Compatibility path for pre-scope fixtures; production always supplies scope."""
        with self._conn:
            if not self._cursor_matches(cursor):
                return False
            evidence = self.read_section_evidence(cursor.entity_id, "personal_channel")
            if evidence is None or evidence.get("generation") != cursor.generation:
                return False
            return self._advance_completed_section(cursor, now=now)

    def _read_primary_detail(self, entity_id: int) -> tuple[dict[str, object], int | None]:
        detail, observed_at = self._read_profile_blob(entity_id)
        if detail:
            return detail, observed_at
        detail = self._read_entity_stub(entity_id)
        return (detail, None) if detail else ({}, None)

    def _read_profile_blob(self, entity_id: int) -> tuple[dict[str, object], int | None]:
        try:
            row = cast(
                tuple[str, int] | None,
                self._conn.execute(
                    "SELECT detail_json, fetched_at FROM entity_details WHERE entity_id = ?",
                    (entity_id,),
                ).fetchone(),
            )
        except sqlite3.OperationalError:
            return {}, None
        if row is None:
            return {}, None
        raw_json, fetched_at = row
        try:
            parsed = cast(object, json.loads(raw_json))
        except TypeError, json.JSONDecodeError:
            return {}, None
        if not isinstance(parsed, dict) or parsed.get("schema") != _DETAIL_SCHEMA:
            return {}, None
        return {str(key): value for key, value in parsed.items() if key != "schema"}, int(fetched_at)

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
            columns = self._columns("entity_profile_refresh_state", "_refresh_columns")
            selected = ["status", "retry_at", "reason"]
            selected.extend(
                column
                for column in ("generation", "started_at", "pair_eligible", "follow_up_required", "profile_revision")
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
        status, retry_at, reason, *extra = row
        if str(status) == "rejected" or (isinstance(retry_at, int) and retry_at > now):
            state: dict[str, object] = {"status": str(status), "retry_at": retry_at, "reason": str(reason)}
            for column, value in zip(
                ("generation", "started_at", "pair_eligible", "follow_up_required", "profile_revision"), extra,
                strict=False,
            ):
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
    ) -> None:
        """Make pending explicit where the additive section table is present."""
        try:
            with self._conn:
                self._upsert_pending_refresh(
                    entity_id,
                    now=now,
                    reason=reason,
                    pair_eligible_override=pair_eligible_override,
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

    def mark_refresh_queued(self, entity_id: int, *, reason: str = "refresh_queued") -> None:
        """Clear rejection state and restore an honest queued reason."""
        try:
            with self._conn:
                self._upsert_pending_refresh(entity_id, now=self._database_now(), reason=reason)
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
    ) -> None:
        if self._refresh_has("generation"):
            existing = self._conn.execute(
                "SELECT status, generation, started_at, pair_eligible, follow_up_required, profile_revision "
                "FROM entity_profile_refresh_state WHERE entity_id=?",
                (entity_id,),
            ).fetchone()
            if existing is not None and str(existing[0]) == "pending" and int(existing[1] or 0) > 0:
                generation = int(existing[1])
                started_at = existing[2]
                pair_eligible = int(existing[3] or 0) if pair_eligible_override is None else int(pair_eligible_override)
                follow_up_required = int(existing[4] or 0)
                profile_revision = int(existing[5] or 0)
            else:
                previous_generation = int(existing[1] or 0) if existing is not None else 0
                generation = max(1, previous_generation + 1)
                started_at = now
                pair_eligible = int(
                    self._pair_is_eligible(entity_id, now=now)
                    if pair_eligible_override is None
                    else pair_eligible_override
                )
                follow_up_required = 0
                profile_revision = self._profile_revision(entity_id)
            self._conn.execute(
                """
                INSERT INTO entity_profile_refresh_state(
                    entity_id, status, retry_at, reason, updated_at, next_section,
                    acquisition_cursor, generation, started_at, pair_eligible,
                    follow_up_required, profile_revision
                ) VALUES (?, 'pending', NULL, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                ON CONFLICT(entity_id) DO UPDATE SET
                    status='pending', retry_at=NULL, reason=excluded.reason,
                    updated_at=excluded.updated_at, next_section=CASE
                        WHEN entity_profile_refresh_state.status='pending'
                         AND entity_profile_refresh_state.next_section IS NOT NULL
                        THEN entity_profile_refresh_state.next_section
                        ELSE excluded.next_section END,
                    acquisition_cursor=CASE
                        WHEN entity_profile_refresh_state.status='pending'
                         AND entity_profile_refresh_state.next_section IS NOT NULL
                        THEN entity_profile_refresh_state.acquisition_cursor
                        ELSE 0 END,
                    generation=excluded.generation,
                    started_at=excluded.started_at,
                    pair_eligible=excluded.pair_eligible,
                    follow_up_required=excluded.follow_up_required,
                    profile_revision=excluded.profile_revision
                """,
                (
                    entity_id,
                    reason,
                    now,
                    PROFILE_SECTIONS[0],
                    generation,
                    started_at,
                    pair_eligible,
                    follow_up_required,
                    profile_revision,
                ),
            )
            return
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
        detail, observed_at = self._read_primary_detail(entity_id)
        if _normalise_entity_type(str(detail.get("type", "unknown"))) not in {"user", "bot"}:
            return False
        stored = self._read_stored_sections(entity_id)
        return all(
            self._section_requires_acquisition(
                section, stored.get(section), detail, observed_at=observed_at, now=now
            )
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
            columns = self._columns("entity_profile_refresh_state", "_refresh_columns")
            selected = ["entity_id", "next_section", "acquisition_cursor", "retry_at"]
            selected.extend(
                column
                for column in ("generation", "started_at", "pair_eligible", "follow_up_required", "profile_revision")
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
        values = list(row)
        if len(values) > _BASE_CURSOR_FIELD_COUNT + 2:
            values[6] = bool(values[6])
        if len(values) > _BASE_CURSOR_FIELD_COUNT + 3:
            values[7] = bool(values[7])
        return EntityRefreshCursor(*values)

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
            assignments = "status='pending', retry_at=NULL, reason='refresh_in_progress', updated_at=?, acquisition_cursor=?"
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
            if not self._cursor_matches(cursor):
                return False
            detail = self._read_detail_blob(cursor.entity_id)
            if not detail:
                detail = self._read_entity_stub(cursor.entity_id)
            detail = _strip_schema(detail)
            detail.update(commit.detail_patch)
            if not self._write_detail(cursor.entity_id, detail, now=now, expected_revision=cursor.profile_revision):
                return False
            section_payload = (
                _section_payload(detail, cursor.next_section) if commit.payload is None else commit.payload
            )
            self._write_section(
                cursor.entity_id,
                cursor.next_section,
                commit.status,
                commit.reason,
                section_payload,
                now=now,
                evidence=commit.evidence,
            )
            next_section = _next_profile_section(cursor.next_section)
            if next_section is None:
                if self._refresh_has("generation"):
                    follow_up = self._refresh_follow_up_required(cursor)
                    if follow_up:
                        changed = self._start_follow_up_generation(cursor, now=now)
                    else:
                        predicate, parameters = self._cursor_predicate(cursor)
                        changed = self._conn.execute(
                            "UPDATE entity_profile_refresh_state SET status='complete', retry_at=NULL, "
                            "reason='refresh_complete', updated_at=? WHERE " + predicate,
                            (now, *parameters),
                        ).rowcount
                else:
                    changed = self._conn.execute(
                        "DELETE FROM entity_profile_refresh_state "
                        "WHERE entity_id=? AND next_section=? AND acquisition_cursor=?",
                        (cursor.entity_id, cursor.next_section, cursor.acquisition_cursor),
                    ).rowcount
            else:
                predicate, parameters = self._cursor_predicate(cursor)
                revision_assignment = ""
                revision_value: tuple[object, ...] = ()
                if self._refresh_has("profile_revision") and self._detail_has("profile_revision"):
                    revision_assignment = ", profile_revision=?"
                    revision_value = (cursor.profile_revision + 1,)
                changed = self._conn.execute(
                    "UPDATE entity_profile_refresh_state SET status='pending', retry_at=NULL, reason='refresh_queued', "
                    "updated_at=?, next_section=?, acquisition_cursor=0" + revision_assignment + " WHERE " + predicate,
                    (now, next_section, *revision_value, *parameters),
                ).rowcount
        return changed == 1

    def _write_detail(
        self,
        entity_id: int,
        detail: Mapping[str, object],
        *,
        now: int,
        expected_revision: int,
    ) -> bool:
        encoded_detail = json.dumps({"schema": _DETAIL_SCHEMA, **detail}, separators=(",", ":"))
        if self._detail_has("profile_revision"):
            changed = self._conn.execute(
                "UPDATE entity_details SET detail_json=?, fetched_at=?, profile_revision=profile_revision+1 "
                "WHERE entity_id=? AND profile_revision=?",
                (encoded_detail, now, entity_id, expected_revision),
            ).rowcount
            if changed == 0:
                exists = self._conn.execute(
                    "SELECT 1 FROM entity_details WHERE entity_id=?", (entity_id,)
                ).fetchone()
                if exists is not None:
                    return False
                self._conn.execute(
                    "INSERT INTO entity_details(entity_id, detail_json, fetched_at, profile_revision) "
                    "VALUES (?, ?, ?, 1)",
                    (entity_id, encoded_detail, now),
                )
            return True
        self._conn.execute(
            "INSERT INTO entity_details(entity_id, detail_json, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(entity_id) DO UPDATE SET detail_json=excluded.detail_json, fetched_at=excluded.fetched_at",
            (entity_id, encoded_detail, now),
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
    ) -> None:
        observed_at = (
            evidence.observation_at
            if evidence is not None and status in {"fresh", "not_applicable"}
            else now if evidence is None and status in {"fresh", "not_applicable"} else None
        )
        columns = ["entity_id", "section", "status", "observed_at", "reason", "payload_json", "retry_at"]
        values: list[object] = [entity_id, section, status, observed_at, reason, _encode_payload(payload), None]
        if evidence is not None and self._section_has("acquisition_generation"):
            encoded_provenance = _encode_bounded_json(evidence.provenance)
            encoded_identity = _encode_bounded_json(evidence.identity)
            columns.extend(
                [
                    "acquisition_generation",
                    "acquisition_outcome",
                    "provenance_json",
                    "normalization_version",
                    "observation_started_at",
                    "observation_completed_at",
                    "acquisition_identity_json",
                ]
            )
            values.extend(
                [
                    evidence.generation,
                    evidence.outcome,
                    encoded_provenance,
                    evidence.normalization_version,
                    evidence.observation_started_at,
                    evidence.observation_completed_at,
                    encoded_identity,
                ]
            )
        elif evidence is None and self._section_has("acquisition_generation"):
            columns.extend(
                [
                    "acquisition_generation",
                    "acquisition_outcome",
                    "provenance_json",
                    "normalization_version",
                    "observation_started_at",
                    "observation_completed_at",
                    "acquisition_identity_json",
                ]
            )
            values.extend([None] * 7)
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{column}=excluded.{column}" for column in columns if column not in {"entity_id", "section"})
        self._conn.execute(
            f"INSERT INTO entity_detail_sections({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(entity_id, section) DO UPDATE SET {updates}",
            values,
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
        for commit in (full_profile, personal_channel):
            if commit.status not in {"fresh", "unavailable", "not_applicable"}:
                raise ValueError("invalid terminal section status")
            if commit.evidence is not None and commit.evidence.generation != cursor.generation:
                raise ValueError("pair evidence generation does not match refresh cursor")
        with self._conn:
            if not self._cursor_matches(cursor):
                return False
            detail = self._read_detail_blob(cursor.entity_id)
            if not detail:
                detail = self._read_entity_stub(cursor.entity_id)
            detail = _strip_schema(detail)
            detail.update(full_profile.detail_patch)
            detail.update(personal_channel.detail_patch)
            if not self._write_detail(cursor.entity_id, detail, now=now, expected_revision=cursor.profile_revision):
                return False
            self._write_section(
                cursor.entity_id,
                "full_profile",
                full_profile.status,
                full_profile.reason,
                _section_payload(detail, "full_profile") if full_profile.payload is None else full_profile.payload,
                now=now,
                evidence=full_profile.evidence,
            )
            self._write_section(
                cursor.entity_id,
                "personal_channel",
                personal_channel.status,
                personal_channel.reason,
                _section_payload(detail, "personal_channel")
                if personal_channel.payload is None
                else personal_channel.payload,
                now=now,
                evidence=personal_channel.evidence,
            )
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
            return changed == 1

    def _refresh_follow_up_required(self, cursor: EntityRefreshCursor) -> bool:
        row = self._conn.execute(
            "SELECT follow_up_required FROM entity_profile_refresh_state WHERE entity_id=?",
            (cursor.entity_id,),
        ).fetchone()
        return bool(row and row[0])

    def _start_follow_up_generation(self, cursor: EntityRefreshCursor, *, now: int) -> int:
        predicate, parameters = self._cursor_predicate(cursor)
        return self._conn.execute(
            "UPDATE entity_profile_refresh_state SET status='pending', retry_at=NULL, "
            "reason='refresh_follow_up', updated_at=?, generation=generation+1, started_at=?, "
            "pair_eligible=?, follow_up_required=0, next_section=?, acquisition_cursor=0 "
            "WHERE " + predicate,
            (
                now,
                now,
                int(self._pair_is_eligible(cursor.entity_id, now=now)),
                PROFILE_SECTIONS[0],
                *parameters,
            ),
        ).rowcount

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
        return self._conn.execute(
            "UPDATE entity_profile_refresh_state SET " + assignments + " WHERE " + predicate,
            (*values, *parameters),
        ).rowcount == 1

    def _cursor_matches(self, cursor: EntityRefreshCursor) -> bool:
        predicate, parameters = self._cursor_predicate(cursor)
        row = self._conn.execute(
            f"SELECT 1 FROM entity_profile_refresh_state WHERE {predicate}", parameters
        ).fetchone()
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
        if row is None or row[0] is None or isinstance(row[0], bool):
            return None
        try:
            generation = int(row[0])
        except (TypeError, ValueError):
            return None
        return {
            "generation": generation,
            "outcome": row[1],
            "provenance": _decode_payload(row[2]),
            "normalization_version": row[3],
            "observation_started_at": row[4],
            "observation_completed_at": row[5],
            "identity": _decode_payload(row[6]),
        }

    def _receipt_is_valid(  # noqa: PLR0911, PLR0913
        self,
        entity_id: int,
        section: str,
        *,
        identity: Mapping[str, object],
        now: int,
        ttl_seconds: int | None = None,
        positive_only: bool,
        expected_generation: int | None = None,
    ) -> bool:
        evidence = self.read_section_evidence(entity_id, section)
        if evidence is None:
            return False
        outcome = evidence.get("outcome")
        allowed = {"usable", "absent"} if positive_only else {"usable", "absent", "partial", "unavailable"}
        if outcome not in allowed:
            return False
        generation = evidence.get("generation")
        if expected_generation is not None and generation != expected_generation:
            return False
        stored_identity = evidence.get("identity")
        if identity is not None and (not isinstance(stored_identity, dict) or dict(identity) != stored_identity):
            return False
        if not self._receipt_materialization_is_exact(entity_id, section, evidence):
            return False
        started_at = evidence.get("observation_started_at")
        completed_at = evidence.get("observation_completed_at")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (started_at, completed_at)
        ):
            return False
        started_at = cast(int, started_at)
        completed_at = cast(int, completed_at)
        if completed_at < started_at or completed_at > now:
            return False
        row = self._conn.execute(
            "SELECT status, observed_at FROM entity_detail_sections WHERE entity_id=? AND section=?",
            (entity_id, section),
        ).fetchone()
        if row is None or row[0] not in {"fresh", "unavailable", "stale"}:
            return False
        if positive_only and row[1] != started_at:
            return False
        if positive_only and row[0] != "fresh":
            return False
        ttl = self._section_ttl_seconds if ttl_seconds is None else max(1, int(ttl_seconds))
        return now < started_at + ttl

    def _receipt_materialization_is_exact(  # noqa: PLR0911
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
        if evidence.get("normalization_version") != NORMALIZATION_VERSION:
            return False
        provenance = evidence.get("provenance")
        if not isinstance(provenance, dict):
            return False
        if provenance.get("endpoint") != FULL_USER_ENDPOINT:
            return False
        declared = provenance.get("declared_fields")
        materialized = provenance.get("materialized_fields")
        if declared != list(expected_fields) or not isinstance(materialized, list):
            return False
        if any(not isinstance(value, str) for value in materialized):
            return False
        if len(set(materialized)) != len(materialized) or not set(materialized) <= set(expected_fields):
            return False
        if not isinstance(provenance.get("authoritative"), bool):
            return False
        payload_row = self._conn.execute(
            "SELECT payload_json FROM entity_detail_sections WHERE entity_id=? AND section=?",
            (entity_id, section),
        ).fetchone()
        if payload_row is None:
            return False
        if section == "personal_channel" and evidence.get("outcome") == "absent":
            return provenance.get("authoritative") is True
        return isinstance(_decode_payload(payload_row[0]), dict)

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
