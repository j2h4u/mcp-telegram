"""SQLite repository for progressive entity profile data.

``entity_details`` stores canonical collected facts. Section rows record
acquisition coverage and payloads; per-field observations order incremental
updates. Older test fixtures retain a compatibility blob fallback.
"""

# SQLite's dynamic row shape is guarded at runtime by the compatibility
# fallbacks below; static ``Any`` propagation from sqlite3 is not useful here.
# pyright: reportAny=false

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import cast

from ..dialog_fact_observations import claim_dialog_fact
from ..dialog_identity import capture_identity_baseline, publish_dialog_identity
from ..dialog_identity_contracts import IDENTITY_OMITTED, DialogIdentityObservation, ObservedText
from ..entity_store import EntitySnapshot, ensure_entity_stub, upsert_entity_snapshots
from ..models import DialogType
from ..resolver import latinize
from ..sync_transactions import require_write_transaction, write_savepoint
from .collection_repository import (
    EffectiveProfileMerge,
    collection_progress,
    encode,
    patch_embedded_story,
    reconcile_embedded_stories,
    store_collection_page,
    store_semantic_patch,
    store_semantic_snapshot,
    store_story_update,
)
from .contracts import (
    FULL_PROFILE_OWNED_FIELDS,
    FULL_USER_ENDPOINT,
    NORMALIZATION_VERSION,
    PERSONAL_CHANNEL_OWNED_FIELDS,
    PROFILE_COLLECTION_SECTIONS,
    PROFILE_SECTIONS,
    ObservationBoundary,
    ProfileAcquisitionEvidence,
    ProfileCollectionItem,
    ProfileCollectionPage,
    ProfileFieldObservation,
    ProfileSemanticSnapshot,
    SuppliedProfileUser,
)
from .profile_history import (
    CollectionHistoryMerge,
    HistoryObservation,
    append_collection_history,
    append_explicit_collection_clear,
    collection_history_watermark,
    effective_collection_membership,
)
from .profile_history_store import append_effective_history
from .supplied_profile import authorized_legacy_user_fields, summary_fingerprint, supplied_current_stories

_DETAIL_SCHEMA = 1
_EVIDENCE_MAX_JSON_BYTES = 4096
_SECTION_STATUSES = {"fresh", "stale", "pending", "unavailable", "not_applicable"}


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class _CoreAcquisitionRejectedError(Exception):
    """Rollback a rejected core CAS without disturbing its caller's write unit."""


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
    page_cursor_json: str | None = None


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
    identity_patch: Mapping[str, object] | None = None
    dialog_created: int | None = None
    dialog_identity_observation: DialogIdentityObservation | None = None
    dialog_identity_baseline_revision: int | None = None
    field_observation: ProfileFieldObservation | None = None
    semantic_snapshot: ProfileSemanticSnapshot | None = None
    collection_page: ProfileCollectionPage | None = None


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

    def _profile_sections(self, entity_id: int) -> tuple[str, ...]:
        kind = str(self._read_entity_stub(entity_id).get("type", "")).lower()
        return PROFILE_SECTIONS if kind in {"user", "bot"} else PROFILE_SECTIONS[:5]

    def _next_section(self, cursor: EntityRefreshCursor) -> str | None:
        sections = self._profile_sections(cursor.entity_id)
        index = sections.index(cursor.next_section) + 1
        return sections[index] if index < len(sections) else None

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

    def read_identity(self, entity_id: int) -> dict[str, object]:
        """Read only canonical directory identity, never private profile data."""
        return self._read_entity_stub(entity_id)

    def apply_profile_update(
        self, entity_id: int, fields: Mapping[str, object], *, observation: ProfileFieldObservation
    ) -> frozenset[str]:
        """Persist supplied facts and their ordering without refreshing whole sections."""
        if set(fields) - (set(FULL_PROFILE_OWNED_FIELDS) | {"current_photo"}):
            raise ValueError("unsupported pushed profile field")
        if not fields or not self._columns("entity_profile_field_observations"):
            return frozenset()
        with write_savepoint(self._conn):
            base, reset_owner = self._observation_profile_base(entity_id, observation)
            detail, accepted = self._merge_observed_fields(entity_id, base, fields, observation)
            if not accepted:
                return frozenset()
            changed = frozenset(field for field in accepted if field not in base or detail[field] != base[field])
            if reset_owner:
                self._reset_unowned_sections(entity_id)
            self._conn.execute(
                "INSERT INTO entity_details(entity_id,detail_json,fetched_at,profile_owner_account_id) VALUES (?,?,?,?) "
                "ON CONFLICT(entity_id) DO UPDATE SET detail_json=excluded.detail_json,"
                "profile_owner_account_id=excluded.profile_owner_account_id",
                (
                    entity_id,
                    json.dumps({"schema": _DETAIL_SCHEMA, **detail}),
                    observation.observed_at,
                    observation.owner_account_id,
                ),
            )
            self._store_field_observations(entity_id, accepted, observation)
            self._publish_updated_identity(entity_id, detail, changed, observation.observed_at)
            return changed

    def apply_supplied_user(
        self,
        user: SuppliedProfileUser,
        *,
        observation: ProfileFieldObservation,
        boundary: ObservationBoundary | None = None,
    ) -> frozenset[str]:
        """One authority path for delivered Users and finite collection sidecars."""
        with write_savepoint(self._conn):
            supplied = replace(observation, source="min") if user.minimal else observation
            ordinary = user
            if user.authoritative_photo:
                ordinary = replace(
                    user,
                    fields={key: value for key, value in user.fields.items() if key != "user.photo"},
                    legacy_fields={key: value for key, value in user.legacy_fields.items() if key != "current_photo"},
                )
            changed = self._apply_supplied_part(ordinary, supplied, boundary)
            if user.authoritative_photo:
                photo = replace(
                    user,
                    fields={"user.photo": user.fields.get("user.photo")},
                    legacy_fields={"current_photo": user.legacy_fields.get("current_photo")},
                    minimal=False,
                )
                changed |= self._apply_supplied_part(
                    photo,
                    replace(observation, source="update") if observation.source == "min" else observation,
                    boundary,
                )
            return changed

    def _apply_supplied_part(
        self, user: SuppliedProfileUser, observation: ProfileFieldObservation, boundary: ObservationBoundary | None
    ) -> frozenset[str]:
        base, legacy_evidence = self._owned_history_base(user.entity_id, observation)
        changed = self.apply_profile_update(
            user.entity_id, authorized_legacy_user_fields(user), observation=observation
        )
        if user.fields:
            self._observation_profile_base(user.entity_id, observation)
            incoming = ProfileSemanticSnapshot(
                user.entity_id,
                observation.owner_account_id,
                user.fields,
                user.coverage,
                boundary or ObservationBoundary(),
                endpoint="supplied.User",
            )
            merged = store_semantic_patch(self._conn, incoming, observation)
            append_effective_history(self._conn, merged, observation, base, legacy_evidence)
            if observation.source != "min":
                changed |= self._changed_semantic_roots(merged)
        return changed

    @staticmethod
    def _changed_semantic_roots(merged: EffectiveProfileMerge) -> frozenset[str]:
        return frozenset(
            root.split(".", 1)[-1]
            for root in merged.after
            if summary_fingerprint(root, merged.before.get(root)) != summary_fingerprint(root, merged.after[root])
            and any(path == root or path.startswith((root + ".", root + "[")) for path in merged.accepted_paths)
        )

    def apply_story_update(
        self, entity_id: int, item: ProfileCollectionItem, *, observation: ProfileFieldObservation
    ) -> frozenset[str]:
        self._observation_profile_base(entity_id, observation)
        prior = self._conn.execute(
            "SELECT MAX(observation_order) FROM entity_profile_collection_facts WHERE owner_account_id=? AND entity_id=? AND item_key=?",
            (observation.owner_account_id, entity_id, item.key),
        ).fetchone()
        if prior is not None and prior[0] is not None and prior[0] >= observation.order:
            return frozenset()
        store_story_update(self._conn, entity_id, item, observation)
        patch_embedded_story(self._conn, entity_id, item, observation)
        if item.fields.get("constructor") != "StoryItemDeleted":
            return frozenset({"current_stories", "pinned_stories"})
        for section in ("current_stories", "pinned_stories"):
            self._journal_story_deletion(entity_id, section, item.key, observation)
        return frozenset({"current_stories", "pinned_stories"})

    def _published_membership(self, entity_id: int, section: str, published: Mapping[str, object]) -> frozenset[str]:
        return frozenset(
            str(row[0])
            for row in self._conn.execute(
                "SELECT item_key FROM entity_profile_collection_membership WHERE owner_account_id=? "
                "AND entity_id=? AND section=? AND generation=? AND scan=? AND identity_kind='remote'",
                (published["owner_account_id"], entity_id, section, published["generation"], published["scan"]),
            )
        )

    def _journal_story_deletion(
        self, entity_id: int, section: str, key: str, observation: ProfileFieldObservation
    ) -> None:
        payload = self.read_section_payload(entity_id, section)
        published = payload.get("published") if isinstance(payload, dict) else None
        if (
            not isinstance(published, dict)
            or not published.get("identifiable")
            or published.get("owner_account_id") != observation.owner_account_id
        ):
            return
        context = HistoryObservation(entity_id, observation, ObservationBoundary())
        members = effective_collection_membership(
            self._conn, context, section, published, self._published_membership(entity_id, section, published)
        )
        if members is not None and key in members:
            append_explicit_collection_clear(
                self._conn, HistoryObservation(entity_id, observation, ObservationBoundary()), section, key, published
            )

    def _observation_profile_base(
        self, entity_id: int, observation: ProfileFieldObservation
    ) -> tuple[dict[str, object], bool]:
        detail, _observed_at, owner, _scope = self._read_profile_blob(entity_id)
        if owner is not None and owner != observation.owner_account_id:
            raise ValueError("profile observation account does not match stored owner")
        if owner is None:
            return self._read_entity_stub(entity_id), True
        return self._merge_canonical_identity(entity_id, detail), False

    def _merge_observed_fields(
        self,
        entity_id: int,
        base: Mapping[str, object],
        fields: Mapping[str, object],
        observation: ProfileFieldObservation,
    ) -> tuple[dict[str, object], frozenset[str]]:
        """Resolve field ordering before a canonical write; do not publish metadata yet."""
        rows = self._conn.execute(
            "SELECT field,owner_account_id,observation_order,source FROM entity_profile_field_observations WHERE entity_id=?",
            (entity_id,),
        ).fetchall()
        previous = {str(row[0]): (row[1], row[2], row[3]) for row in rows}
        accepted = {
            field: value
            for field, value in fields.items()
            if _accept_field_observation(field, base, previous.get(field), observation)
        }
        detail = {**base, **accepted}
        if "current_photo" in accepted:
            detail["current_photo"] = _merge_current_photo(base.get("current_photo"), accepted["current_photo"])
        if set(accepted) & {"contact", "mutual_contact", "close_friend", "blocked", "my_membership"}:
            _recompose_profile_membership(detail)
        return detail, frozenset(accepted)

    def _store_field_observations(
        self, entity_id: int, fields: frozenset[str], observation: ProfileFieldObservation
    ) -> None:
        self._conn.executemany(
            "INSERT INTO entity_profile_field_observations(entity_id,field,owner_account_id,observation_order,observed_at,source) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(entity_id,field) DO UPDATE SET "
            "owner_account_id=excluded.owner_account_id,observation_order=excluded.observation_order,"
            "observed_at=excluded.observed_at,source=excluded.source",
            (
                (
                    entity_id,
                    field,
                    observation.owner_account_id,
                    observation.order,
                    observation.observed_at,
                    observation.source,
                )
                for field in fields
            ),
        )

    def _reset_unowned_sections(self, entity_id: int) -> None:
        assignments = [
            "status=CASE WHEN status='not_applicable' THEN status ELSE 'pending' END",
            "observed_at=NULL",
            "payload_json=NULL",
            "reason='profile_owner_unverified'",
            "retry_at=NULL",
        ]
        assignments.extend(
            f"{column}=NULL"
            for column in (
                "acquisition_generation",
                "acquisition_outcome",
                "provenance_json",
                "normalization_version",
                "observation_started_at",
                "observation_completed_at",
                "acquisition_identity_json",
            )
            if self._section_has(column)
        )
        self._conn.execute(
            "UPDATE entity_detail_sections SET " + ",".join(assignments) + " WHERE entity_id=?", (entity_id,)
        )

    def _publish_updated_identity(
        self, entity_id: int, detail: Mapping[str, object], changed: frozenset[str], observed_at: int
    ) -> None:
        if not changed & {"name", "username"}:
            return
        baseline = capture_identity_baseline(self._conn, entity_id)
        self._update_canonical_identity(entity_id, detail, now=observed_at)
        self._bump_identity_fence(entity_id)
        publish_dialog_identity(
            self._conn,
            entity_id,
            DialogIdentityObservation(
                entity_id,
                name=cast(ObservedText, detail.get("name")) if "name" in changed else IDENTITY_OMITTED,
                username=cast(ObservedText, detail.get("username")) if "username" in changed else IDENTITY_OMITTED,
                source="realtime",
                observed_at=observed_at,
            ),
            baseline,
        )

    def reconcile_history_profiles(self, *, now: int, limit: int, pair_mode: str, self_id: int | None) -> int:
        """Advance the finite personal-peer admission checkpoint."""
        if isinstance(limit, bool) or limit < 1:
            raise ValueError("profile reconciliation limit must be positive")
        if self_id is None or not self._history_profile_schema_available():
            return 0
        return _reconcile_profile_admission(self._conn, self_id, now=now, limit=limit, pair_mode=pair_mode)

    def _history_profile_schema_available(self) -> bool:
        return all(
            self._columns(table)
            for table in ("full_history_enrollment", "dialogs", "entity_details", "entity_detail_sections")
        ) and self._refresh_has("generation")

    def _missing_profile_sections(self, entity_id: int) -> tuple[str, ...]:
        stored = self._read_stored_sections(entity_id)
        missing = []
        for section in self._profile_sections(entity_id):
            row = stored.get(section)
            if row is None or _profile_section_missing(row):
                missing.append(section)
        if not self._read_detail_blob(entity_id) and "full_profile" not in missing:
            missing.insert(0, "full_profile")
        return tuple(missing)

    def request_section_refresh(self, entity_id: int, sections: tuple[str, ...], *, now: int, pair_mode: str) -> bool:
        """Invalidate only requested sections and fence an older in-flight result."""
        if any(section not in self._profile_sections(entity_id) for section in sections):
            raise ValueError("unknown profile section")
        if pair_mode not in {"enabled", "disabled"}:
            raise ValueError("invalid profile pair mode")
        if not sections or not self._refresh_has("generation"):
            return False
        with write_savepoint(self._conn):
            self._capture_legacy_section_evidence(entity_id, sections)
            if not self.ensure_unknown_refresh_parent(entity_id, now=now):
                return False
            active = self._read_pending_refresh_state(entity_id)
            previous = self._conn.execute(
                "SELECT next_section FROM entity_profile_refresh_state WHERE entity_id=? AND status IN ('pending','failed')",
                (entity_id,),
            ).fetchone()
            self._upsert_pending_refresh(entity_id, now=now, reason="profile_changed", pair_mode_override=pair_mode)
            self._conn.executemany(
                "INSERT INTO entity_detail_sections(entity_id,section,status,reason) VALUES (?,?,'pending','profile_changed') "
                "ON CONFLICT(entity_id,section) DO UPDATE SET status='pending',reason='profile_changed'",
                ((entity_id, section) for section in set(sections)),
            )
            first = _first_dirty_section(sections, previous)
            self._conn.execute(
                "UPDATE entity_profile_refresh_state SET generation=generation+?, started_at=?, "
                "next_section=?,acquisition_cursor=0,follow_up_required=0,pair_eligible=? "
                + (",page_cursor_json=NULL " if self._refresh_has("page_cursor_json") else "")
                + "WHERE entity_id=?",
                (
                    int(_pending_refresh_is_active(active)),
                    now,
                    first,
                    int(self._pair_is_eligible(entity_id, now=now)),
                    entity_id,
                ),
            )
            self._reset_pair_measurement(entity_id)
            return True

    def invalidate_profile_sections(
        self,
        entity_id: int,
        sections: tuple[str, ...],
        *,
        observation: ProfileFieldObservation,
        reason: str,
        pair_mode: str,
    ) -> bool:
        """Persist the original invalidation order so replay cannot revive finished work."""
        require_write_transaction(self._conn)
        if reason != "user" and reason != "supplied:" + ",".join(sorted(set(sections))):
            raise ValueError("invalid profile invalidation kind")
        self._observation_profile_base(entity_id, observation)
        previous = self._conn.execute(
            "SELECT observation_order FROM entity_profile_field_observations "
            "WHERE owner_account_id=? AND entity_id=? AND field=?",
            (observation.owner_account_id, entity_id, "__invalidation__:" + reason),
        ).fetchone()
        if previous is not None and previous[0] >= observation.order:
            return False
        if not self.request_section_refresh(entity_id, sections, now=observation.observed_at, pair_mode=pair_mode):
            return False
        self._conn.execute(
            "INSERT INTO entity_profile_field_observations(entity_id,field,owner_account_id,observation_order,observed_at,source) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(entity_id,field) DO UPDATE SET owner_account_id=excluded.owner_account_id,"
            "observation_order=excluded.observation_order,observed_at=excluded.observed_at,source=excluded.source",
            (
                entity_id,
                "__invalidation__:" + reason,
                observation.owner_account_id,
                observation.order,
                observation.observed_at,
                observation.source,
            ),
        )
        return True

    def _capture_legacy_section_evidence(self, entity_id: int, sections: tuple[str, ...]) -> None:
        """Keep the original legacy section age before invalidation or v2 overwrite."""
        detail, _observed, owner, _scope = self._read_profile_blob(entity_id)
        if owner is None:
            return
        for section in set(sections) & {"avatar_history", "common_chats"}:
            if not isinstance(detail.get(section), list):
                continue
            row = self._conn.execute(
                "SELECT status,observed_at,reason,payload_json FROM entity_detail_sections WHERE entity_id=? AND section=?",
                (entity_id, section),
            ).fetchone()
            if row is None:
                continue
            previous = json.loads(row[3]) if row[3] else None
            if not isinstance(previous, list):
                continue
            receipt = {
                "owner_account_id": owner,
                "section": section,
                "observed_at": row[1],
                "stored_status": row[0],
                "stored_reason": row[2],
                "traversal_coverage": "unknown",
                "original_status": row[0] if row[0] not in {"pending", "stale"} else None,
            }
            self._conn.execute(
                "UPDATE entity_detail_sections SET payload_json=? WHERE entity_id=? AND section=?",
                (encode({"legacy_evidence": receipt}), entity_id, section),
            )

    def complete_unchanged_section(self, cursor: EntityRefreshCursor, *, now: int) -> bool:
        """Advance retained terminal knowledge without rewriting its observation."""
        with write_savepoint(self._conn):
            row = self._conn.execute(
                "SELECT status,observed_at,reason,retry_at FROM entity_detail_sections WHERE entity_id=? AND section=?",
                (cursor.entity_id, cursor.next_section),
            ).fetchone()
            if row is None or cursor.retry_at is not None or row[3] is not None or not self._cursor_matches(cursor):
                return False
            return self._advance_completed_section(cursor, now=now) if self._section_collected(cursor, row) else False

    def _section_collected(self, cursor: EntityRefreshCursor, row: tuple[object, ...]) -> bool:
        if row[0] in {"not_applicable", "unavailable"}:
            return row[0] == "not_applicable" or row[2] != "refresh_rejected"
        if row[0] != "fresh" or row[1] is None:
            return False
        if str(self._read_entity_stub(cursor.entity_id).get("type", "")).lower() not in {"user", "bot"}:
            return True
        if cursor.next_section == "full_profile":
            return self._has_semantic_snapshot(cursor.entity_id)
        if cursor.next_section in PROFILE_COLLECTION_SECTIONS:
            payload = self.read_section_payload(cursor.entity_id, cursor.next_section)
            return isinstance(payload, dict) and payload.get("complete") is True
        return True

    def _has_semantic_snapshot(self, entity_id: int) -> bool:
        row = self._conn.execute(
            "SELECT source,fields_json FROM entity_profile_semantic_snapshots s JOIN entity_details p "
            "ON p.entity_id=s.entity_id AND p.profile_owner_account_id=s.owner_account_id "
            "WHERE s.entity_id=? AND s.normalization_version=? AND s.source='rpc' "
            "AND s.observation_order>0 AND s.observation_started_at IS NOT NULL AND s.observation_completed_at IS NOT NULL",
            (entity_id, NORMALIZATION_VERSION),
        ).fetchone()
        return row is not None and row[0] != "legacy" and row[1] != "{}"

    def read_section_evidence(self, entity_id: int, section: str) -> dict[str, object] | None:
        """Return the bounded receipt projection, if a migration-created row has one."""
        return self._read_section_evidence(entity_id=entity_id, section=section)

    def read_section_payload(self, entity_id: int, section: str) -> object | None:
        """Read one private section payload without projecting public detail."""
        if not isinstance(entity_id, int) or not isinstance(section, str) or not section:
            return None
        try:
            row = self._conn.execute(
                "SELECT payload_json FROM entity_detail_sections WHERE entity_id=? AND section=?",
                (entity_id, section),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return None if row is None else _decode_payload(row[0])

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
        with write_savepoint(self._conn):
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
        with write_savepoint(self._conn):
            if not self._cursor_matches(cursor):
                return False
            personal_receipt = self._parse_validated_receipt(
                cursor.entity_id,
                "personal_channel",
                identity=identity,
                now=now,
                expected_generation=cursor.generation,
            )
            personal_row = self._receipt_section_state(cursor.entity_id, "personal_channel")
            if (
                personal_receipt is None
                or personal_row is None
                or personal_row[0] not in {"fresh", "stale", "unavailable"}
            ):
                # A stale, corrupt, or scope-fenced receipt cannot be retried
                # at this cursor: the next local slice would otherwise repeat
                # forever without reacquiring the pair.
                self._start_follow_up_generation(cursor, now=now)
                return False

            follow_up_required = self._pair_predecessors_require_follow_up(
                cursor.entity_id,
                generation=cursor.generation,
            )
            if follow_up_required and self._refresh_has("follow_up_required"):
                self._conn.execute(
                    "UPDATE entity_profile_refresh_state SET follow_up_required=1 WHERE entity_id=? AND generation=?",
                    (cursor.entity_id, cursor.generation),
                )
            return self._advance_completed_section(cursor, now=now)

    def _pair_predecessors_require_follow_up(
        self,
        entity_id: int,
        *,
        generation: int,
    ) -> bool:
        """Flag missing or pending pair predecessors and scope repairs."""
        refresh_reason = self._conn.execute(
            "SELECT reason FROM entity_profile_refresh_state WHERE entity_id=? AND generation=?",
            (entity_id, generation),
        ).fetchone()
        scope_repair = refresh_reason is not None and refresh_reason[0] == "auth_scope_changed"
        follow_up_required = False
        full_row = self._receipt_section_state(entity_id, "full_profile")
        if full_row is None or full_row[0] == "pending":
            follow_up_required = True
        if scope_repair:
            follow_up_required = follow_up_required or any(
                (row := self._receipt_section_state(entity_id, section)) is None or row[0] == "pending"
                for section in PROFILE_SECTIONS[:4]
            )
        return follow_up_required

    def _read_primary_detail(
        self, entity_id: int
    ) -> tuple[dict[str, object], int | None, int | None, dict[str, object] | None]:
        detail, observed_at, owner_account_id, observation_scope = self._read_profile_blob(entity_id)
        detail = self._merge_canonical_identity(entity_id, detail)
        if detail:
            return detail, observed_at, owner_account_id, observation_scope
        return (detail, None, None, None) if detail else ({}, None, None, None)

    def _merge_canonical_identity(self, entity_id: int, detail: Mapping[str, object]) -> dict[str, object]:
        """Overlay canonical identity, using the blob only for unknown stubs."""
        canonical = self._read_entity_stub(entity_id)
        if not canonical:
            return dict(detail)
        merged = dict(detail)
        merged["id"] = canonical["id"]
        canonical_raw_type = canonical.get("type")
        detail_raw_type = detail.get("type")
        canonical_type = DialogType.parse(canonical_raw_type if isinstance(canonical_raw_type, str) else None)
        detail_type = DialogType.parse(detail_raw_type if isinstance(detail_raw_type, str) else None)
        merged["type"] = canonical_type.value if canonical_type is not DialogType.UNKNOWN else detail_type.value
        if canonical_type is not DialogType.UNKNOWN:
            merged["name"] = canonical.get("name")
            merged["username"] = canonical.get("username")
            return merged
        for field in ("name", "username"):
            canonical_value = canonical.get(field)
            if (isinstance(canonical_value, str) and canonical_value.strip()) or field not in merged:
                merged[field] = canonical_value
        return merged

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
        has_normalized_name = "name_normalized" in self._columns("entities")
        selected = "type, name, username" + (", name_normalized" if has_normalized_name else "")
        entity_row = cast(
            tuple[object, ...] | None,
            self._conn.execute(f"SELECT {selected} FROM entities WHERE id = ?", (entity_id,)).fetchone(),
        )
        if entity_row is None:
            return {}
        entity_type, name, username = entity_row[:3]
        result: dict[str, object] = {
            "id": entity_id,
            "type": _normalise_entity_type(str(entity_type)),
            "name": name,
            "username": username,
        }
        if has_normalized_name:
            result["name_normalized"] = entity_row[3]
        return result

    def _update_canonical_identity(
        self,
        entity_id: int,
        detail: Mapping[str, object],
        *,
        now: int,
        canonical: Mapping[str, object] | None = None,
    ) -> None:
        """Materialize fresh identity observations in the canonical entity row."""
        if canonical is None:
            canonical = self._read_entity_stub(entity_id)
        if not canonical:
            return
        updated = _apply_identity_patch(canonical, detail)
        if all(updated[field] == canonical[field] for field in ("type", "name", "username")):
            return
        upsert_entity_snapshots(
            self._conn,
            [
                EntitySnapshot(
                    entity_id=entity_id,
                    entity_type=str(updated["type"]),
                    name=_optional_text(updated["name"]),
                    username=_optional_text(updated["username"]),
                    name_normalized=_identity_name_normalized(canonical, updated),
                    updated_at=now,
                )
            ],
        )

    def save_core(
        self,
        detail: Mapping[str, object],
        *,
        now: int,
        dialog_identity_observation: DialogIdentityObservation | None = None,
        dialog_identity_baseline_revision: int | None = None,
    ) -> None:
        """Persist the mandatory core in the existing entities projection."""
        entity_id = detail.get("id")
        if not isinstance(entity_id, int):
            return
        with write_savepoint(self._conn):
            canonical = self._read_entity_stub(entity_id)
            identity = _apply_identity_patch(canonical or {"id": entity_id}, detail, allow_deletions=False)
            upsert_entity_snapshots(
                self._conn,
                [
                    EntitySnapshot(
                        entity_id=entity_id,
                        entity_type=str(identity.get("type", "unknown")),
                        name=_optional_text(identity.get("name")),
                        username=_optional_text(identity.get("username")),
                        name_normalized=_identity_name_normalized(canonical, identity),
                        updated_at=now,
                    )
                ],
            )
            # Identity writes are canonical profile writes for fencing, but
            # they do not renew the detail blob's observation age.
            self._bump_identity_fence(entity_id)
            self._publish_dialog_identity(dialog_identity_observation, dialog_identity_baseline_revision)

    def persist_group_created(self, entity_id: int, created: int | None) -> None:
        """Ratchet a legacy group's canonical creation time when the table exists."""
        if created is None or not {"dialog_id", "created"} <= self._columns("dialogs"):
            return
        with write_savepoint(self._conn):
            self._conn.execute(
                "UPDATE dialogs SET created=? WHERE dialog_id=? AND created IS NULL",
                (created, entity_id),
            )

    def commit_core_acquisition(  # noqa: PLR0913
        self,
        cursor: EntityRefreshCursor,
        core: Mapping[str, object],
        *,
        next_acquisition_cursor: int,
        now: int,
        dialog_identity_observation: DialogIdentityObservation | None = None,
        dialog_identity_baseline_revision: int | None = None,
    ) -> bool:
        """Atomically commit canonical core identity and durable progress."""
        if next_acquisition_cursor <= cursor.acquisition_cursor:
            raise ValueError("next_acquisition_cursor must advance")
        entity_id = core.get("id")
        if not isinstance(entity_id, int) or entity_id != cursor.entity_id:
            return False
        try:
            with write_savepoint(self._conn):
                if not self._cursor_matches(cursor):
                    return False
                canonical = self._read_entity_stub(entity_id)
                identity = _apply_identity_patch(canonical, core, allow_deletions=False)
                upsert_entity_snapshots(
                    self._conn,
                    [
                        EntitySnapshot(
                            entity_id=entity_id,
                            entity_type=str(identity.get("type", "unknown")),
                            name=_optional_text(identity.get("name")),
                            username=_optional_text(identity.get("username")),
                            name_normalized=_identity_name_normalized(canonical, identity),
                            updated_at=now,
                        )
                    ],
                )
                detail_revision = cursor.profile_revision
                if self._detail_has("profile_revision"):
                    detail_exists = self._conn.execute(
                        "SELECT 1 FROM entity_details WHERE entity_id=?", (entity_id,)
                    ).fetchone()
                    if detail_exists is not None:
                        self._conn.execute(
                            "UPDATE entity_details SET profile_revision=profile_revision+1 WHERE entity_id=?",
                            (entity_id,),
                        )
                        detail_revision = self._profile_revision(entity_id)
                    else:
                        # The core RPC establishes canonical identity while the
                        # next section owns the first detail insert.  Advance the
                        # refresh fence through the cursor CAS below.
                        detail_revision = cursor.profile_revision + 1
                self._publish_dialog_identity(dialog_identity_observation, dialog_identity_baseline_revision)
                predicate, parameters = self._cursor_predicate(cursor)
                assignments = (
                    "status='pending', retry_at=NULL, reason='refresh_in_progress', updated_at=?, acquisition_cursor=?"
                )
                values: tuple[object, ...] = (now, next_acquisition_cursor)
                if self._refresh_has("profile_revision"):
                    assignments += ", profile_revision=?"
                    values += (detail_revision,)
                changed = self._conn.execute(
                    "UPDATE entity_profile_refresh_state SET " + assignments + " WHERE " + predicate,
                    (*values, *parameters),
                ).rowcount
                if changed != 1:
                    raise _CoreAcquisitionRejectedError
                return True
        except _CoreAcquisitionRejectedError:
            return False

    def ensure_unknown_refresh_parent(self, entity_id: int, *, now: int) -> bool:
        """Create an unknown entity parent before FK-protected refresh rows."""
        try:
            foreign_keys = self._conn.execute("PRAGMA foreign_keys").fetchone()
            if foreign_keys is None or int(foreign_keys[0]) != 1:
                return True
            with write_savepoint(self._conn):
                ensure_entity_stub(
                    self._conn,
                    EntitySnapshot(entity_id, "unknown", None, None, None, now),
                )
            return True
        except sqlite3.OperationalError, ValueError:
            return False

    def refresh_is_complete(self, cursor: EntityRefreshCursor) -> bool:
        """Read completion for the committed generation without exposing resolved state."""
        generation = self._refresh_has("generation")
        row = self._conn.execute(
            "SELECT status"
            + (", generation" if generation else "")
            + " FROM entity_profile_refresh_state WHERE entity_id=?",
            (cursor.entity_id,),
        ).fetchone()
        if row is None:
            return not generation and self._next_section(cursor) is None
        return row[0] == "complete" and (not generation or row[1] == cursor.generation)

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
                    "page_cursor_json",
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
            with write_savepoint(self._conn):
                new_generation = self._upsert_pending_refresh(
                    entity_id,
                    now=now,
                    reason=reason,
                    pair_eligible_override=pair_eligible_override,
                    pair_mode_override=pair_mode_override,
                )
                if new_generation:
                    self._conn.executemany(
                        "INSERT INTO entity_detail_sections(entity_id, section, status, observed_at, reason, payload_json, retry_at) "
                        "VALUES (?, ?, 'pending', NULL, ?, NULL, NULL) "
                        "ON CONFLICT(entity_id, section) DO UPDATE SET "
                        "status=CASE WHEN entity_detail_sections.status='not_applicable' "
                        "THEN entity_detail_sections.status ELSE 'pending' END, "
                        "reason=CASE WHEN entity_detail_sections.status='not_applicable' "
                        "THEN entity_detail_sections.reason ELSE excluded.reason END, retry_at=NULL",
                        ((entity_id, section, reason) for section in self._profile_sections(entity_id)),
                    )
                    if pair_eligible_override is None and self._refresh_has("pair_eligible"):
                        self._conn.execute(
                            "UPDATE entity_profile_refresh_state SET pair_eligible=? WHERE entity_id=?",
                            (int(self._pair_is_eligible(entity_id, now=now)), entity_id),
                        )
                else:
                    # Active-generation re-admission must not rewrite evidence,
                    # timestamps, retry boundaries, or captured pair mode.
                    self._conn.executemany(
                        "INSERT OR IGNORE INTO entity_detail_sections(entity_id, section, status, observed_at, reason, payload_json, retry_at) "
                        "VALUES (?, ?, 'pending', NULL, ?, NULL, NULL)",
                        ((entity_id, section, reason) for section in self._profile_sections(entity_id)),
                    )
        except sqlite3.OperationalError, sqlite3.IntegrityError:
            return

    def _upsert_pending_refresh(
        self,
        entity_id: int,
        *,
        now: int,
        reason: str,
        pair_eligible_override: bool | None = None,
        pair_mode_override: str | None = None,
    ) -> bool:
        require_write_transaction(self._conn)
        if not self._refresh_has("generation"):
            self._upsert_legacy_pending_refresh(entity_id, now=now, reason=reason)
            return True

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
        values: list[object] = [
            entity_id,
            "pending",
            optional_values.pop("retry_at", None),
            reason,
            now,
            PROFILE_SECTIONS[0],
            0,
        ]
        for column, value in optional_values.items():
            if self._refresh_has(column):
                columns.append(column)
                values.append(value)
        query = _pending_refresh_upsert_query(columns)
        self._conn.execute(query, values)
        if new_generation:
            self._reset_pair_measurement(entity_id)
        return new_generation

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
                "retry_at",
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
            "page_cursor_json": None,
        }

    def _upsert_legacy_pending_refresh(self, entity_id: int, *, now: int, reason: str) -> None:
        require_write_transaction(self._conn)
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
        if self._detail_has("profile_revision"):
            row = self._conn.execute(
                "SELECT profile_revision FROM entity_details WHERE entity_id=?", (entity_id,)
            ).fetchone()
            if row is not None and row[0] is not None:
                return int(row[0])
        if self._refresh_has("profile_revision"):
            row = self._conn.execute(
                "SELECT profile_revision FROM entity_profile_refresh_state WHERE entity_id=?", (entity_id,)
            ).fetchone()
            if row is not None and row[0] is not None:
                return int(row[0])
        return 0

    def _bump_identity_fence(self, entity_id: int) -> int | None:
        require_write_transaction(self._conn)
        if self._detail_has("profile_revision"):
            changed = self._conn.execute(
                "UPDATE entity_details SET profile_revision=profile_revision+1 WHERE entity_id=?",
                (entity_id,),
            ).rowcount
            if changed == 1:
                revision = self._profile_revision(entity_id)
                self._carry_active_refresh_revision(entity_id, revision)
                return revision
        if self._refresh_has("profile_revision"):
            changed = self._conn.execute(
                "UPDATE entity_profile_refresh_state SET profile_revision=profile_revision+1 "
                "WHERE entity_id=? AND status IN ('pending', 'failed')",
                (entity_id,),
            ).rowcount
            if changed == 1:
                return self._profile_revision(entity_id)
        return None

    def _carry_active_refresh_revision(self, entity_id: int, revision: int) -> None:
        require_write_transaction(self._conn)
        if self._refresh_has("profile_revision"):
            self._conn.execute(
                "UPDATE entity_profile_refresh_state SET profile_revision=? "
                "WHERE entity_id=? AND status IN ('pending', 'failed')",
                (revision, entity_id),
            )

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
            return self._legacy_section_status(detail, section, payload, observed_at, now=now) == "pending"
        return _profile_section_missing(row)

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
                    "page_cursor_json",
                )
                if column in columns
            )
            row = self._conn.execute(
                f"SELECT {', '.join(selected)} FROM entity_profile_refresh_state "
                "WHERE status IN ('pending', 'failed') AND (retry_at IS NULL OR retry_at <= ?) "
                "ORDER BY updated_at, entity_id LIMIT 1",
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
            page_cursor_json=cast(str | None, values.get("page_cursor_json")),
        )

    def recover_pair_measurements(self, *, now: int) -> None:
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
        with write_savepoint(self._conn):
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
        with write_savepoint(self._conn):
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
        with write_savepoint(self._conn):
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
        require_write_transaction(self._conn)
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
        with write_savepoint(self._conn):
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
        require_write_transaction(self._conn)
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
        require_write_transaction(self._conn)
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
        with write_savepoint(self._conn):
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
        with write_savepoint(self._conn):
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
        if commit.collection_page is not None:
            return self._commit_collection(cursor, commit, now=now)
        history_base, history_evidence = self._owned_history_base(cursor.entity_id, commit.field_observation)
        detail, accepted, reset_owner = self._prepare_observed_commit(
            cursor.entity_id, commit.detail_patch, commit.field_observation
        )
        if not self._write_detail(
            cursor.entity_id,
            detail,
            now=now,
            expected_revision=cursor.profile_revision,
            owner_account_id=commit.observation_owner_account_id,
            observation_scope=commit.observation_auth_scope,
            ownership_observed=commit.ownership_observed,
            identity_patch=_accepted_identity_patch(commit, accepted),
            dialog_identity_observation=commit.dialog_identity_observation,
            dialog_identity_baseline_revision=commit.dialog_identity_baseline_revision,
        ):
            return False
        self._finish_observed_commit(cursor.entity_id, accepted, reset_owner, commit.field_observation)
        self._store_semantic_commit(commit, history_base, history_evidence)
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
        self.persist_group_created(cursor.entity_id, commit.dialog_created)
        return True

    def _owned_history_base(
        self, entity_id: int, observation: ProfileFieldObservation | None
    ) -> tuple[dict[str, object], dict[str, object]]:
        detail, _at, owner, _scope = self._read_profile_blob(entity_id)
        if observation is None or owner != observation.owner_account_id:
            return {}, {}
        rows = self._conn.execute(
            "SELECT field,observation_order,observed_at,source FROM entity_profile_field_observations "
            "WHERE entity_id=? AND owner_account_id=? AND field IN ('name','username','current_photo')",
            (entity_id, owner),
        ).fetchall()
        partial = {str(row[0]) for row in rows if row[3] == "min"}
        values = {key: value for key, value in detail.items() if key not in partial}
        evidence: dict[str, object] = {
            "legacy." + str(row[0]): {"order": row[1], "observed_at": row[2], "source": row[3]}
            for row in rows
            if row[0] in values
        }
        return values, evidence

    def _store_semantic_commit(
        self, commit: EntitySectionCommit, legacy_before: Mapping[str, object], legacy_evidence: Mapping[str, object]
    ) -> EffectiveProfileMerge | None:
        snapshot = commit.semantic_snapshot
        observation = commit.field_observation
        if snapshot is not None:
            if snapshot.owner_account_id is None:
                return None
            if observation is None:
                raise ValueError("semantic commit requires account observation")
            snapshot.require_account(observation.owner_account_id)
            merged = store_semantic_snapshot(self._conn, snapshot, observation.order)
            if merged is not None:
                self._dirty_summary_sections(merged)
                if commit.evidence is not None:
                    merged = self._store_supplied_stories(merged, commit.evidence.generation, observation)
                append_effective_history(self._conn, merged, observation, legacy_before, legacy_evidence)
            return merged
        return None

    def _commit_collection(self, cursor: EntityRefreshCursor, commit: EntitySectionCommit, *, now: int) -> bool:
        page = commit.collection_page
        observation = commit.field_observation
        if page is None or observation is None:
            raise ValueError("collection commit requires captured account observation")
        if page.entity_id != cursor.entity_id or page.section != cursor.next_section:
            raise ValueError("collection page identity mismatch")
        owner = self._conn.execute(
            "SELECT profile_owner_account_id FROM entity_details WHERE entity_id=?", (cursor.entity_id,)
        ).fetchone()
        if owner is not None and owner[0] is not None and owner[0] != observation.owner_account_id:
            return False
        self._apply_collection_sidecars(page, observation)
        # Accepted sidecars can advance this page's own identity revision.
        cursor = replace(cursor, profile_revision=self._profile_revision(cursor.entity_id))
        state = json.loads(cursor.page_cursor_json) if cursor.page_cursor_json else {}
        summary = store_collection_page(
            self._conn,
            page,
            owner=observation.owner_account_id,
            generation=cursor.generation,
            cursor=state,
            order=observation.order,
        )
        reconcile_embedded_stories(self._conn, page, observation)
        summary["history_order"] = observation.order
        if not self._advance_collection(cursor, page, state, summary, now=now):
            raise sqlite3.OperationalError("entity profile collection cursor advance was rejected")
        return True

    def _apply_collection_sidecars(self, page: ProfileCollectionPage, observation: ProfileFieldObservation) -> None:
        if page.supplied_user is not None:
            if page.supplied_user.entity_id != page.entity_id:
                raise ValueError("supplied user target mismatch")
            self.apply_supplied_user(page.supplied_user, observation=observation, boundary=page.observation)
        for identity in page.sidecar_identities:
            if identity.dialog_id != page.entity_id:
                self._publish_supplied_identity(identity, observation)

    def _advance_collection(
        self,
        cursor: EntityRefreshCursor,
        page: ProfileCollectionPage,
        state: dict[str, object],
        summary: dict[str, object],
        *,
        now: int,
    ) -> bool:
        self._capture_legacy_section_evidence(cursor.entity_id, (cursor.next_section,))
        previous = self.read_section_payload(cursor.entity_id, cursor.next_section)
        if isinstance(previous, dict) and isinstance(previous.get("legacy_evidence"), dict):
            state = {**state, "legacy_evidence": previous["legacy_evidence"]}
        progress = collection_progress(page, state, summary, previous)
        self._journal_collection(cursor, page, progress.payload)
        self._write_section(
            cursor.entity_id,
            cursor.next_section,
            progress.status,
            progress.reason,
            progress.payload,
            now=now,
            evidence=ProfileAcquisitionEvidence(
                generation=cursor.generation,
                outcome="usable" if progress.status == "fresh" else "partial",
                observation_started_at=int(cast(float, page.observation.started_at)),
                observation_completed_at=int(cast(float, page.observation.completed_at)),
            ),
        )
        if progress.cursor is None:
            return self._advance_after_section_write(cursor, now=now, detail_revision=None) == 1
        predicate, parameters = self._cursor_predicate(cursor)
        return (
            self._conn.execute(
                "UPDATE entity_profile_refresh_state SET page_cursor_json=?,status=?,retry_at=?,reason=?,updated_at=? WHERE "
                + predicate,
                (
                    encode(progress.cursor),
                    "failed" if progress.retry else "pending",
                    now + 60 if progress.retry else None,
                    progress.reason or "collection_page",
                    now,
                    *parameters,
                ),
            ).rowcount
            == 1
        )

    def _store_supplied_stories(
        self, merge: EffectiveProfileMerge, generation: int, observation: ProfileFieldObservation
    ) -> EffectiveProfileMerge:
        page = supplied_current_stories(merge)
        if page is None:
            return merge
        previous = self.read_section_payload(page.entity_id, page.section)
        # Use a distinct scan when supplied contents supersede a publication in this generation.
        scan = (
            1 + int(previous.get("scan", -1))
            if isinstance(previous, dict) and previous.get("generation") == generation
            else 0
        )
        state: dict[str, object] = {"scan": scan}
        summary = store_collection_page(
            self._conn,
            page,
            owner=observation.owner_account_id,
            generation=generation,
            cursor=state,
            order=observation.order,
        )
        reconciled = reconcile_embedded_stories(self._conn, page, observation, merge)
        assert reconciled is not None
        summary["history_order"] = observation.order
        progress = collection_progress(page, state, summary, previous)
        payload = {**progress.payload, "supplied_cursor": progress.cursor, "supplied_generation": generation}
        self._journal_collection(
            EntityRefreshCursor(page.entity_id, page.section, 0, None, generation=generation), page, payload
        )
        self._write_section(
            page.entity_id,
            page.section,
            progress.status,
            progress.reason,
            payload,
            now=observation.observed_at,
            evidence=ProfileAcquisitionEvidence(
                generation,
                "usable" if progress.status == "fresh" else "partial",
                observation_started_at=int(cast(float, page.observation.started_at)),
                observation_completed_at=int(cast(float, page.observation.completed_at)),
            ),
        )
        return reconciled

    def _publish_supplied_identity(
        self, identity: DialogIdentityObservation, observation: ProfileFieldObservation
    ) -> None:
        if not identity.complete or not self._read_entity_stub(identity.dialog_id):
            return
        name = identity.name
        username = identity.username
        if name is not IDENTITY_OMITTED and not claim_dialog_fact(
            self._conn, identity.dialog_id, "name", observation.order
        ):
            name = IDENTITY_OMITTED
        if username is not IDENTITY_OMITTED and not claim_dialog_fact(
            self._conn, identity.dialog_id, "username", observation.order
        ):
            username = IDENTITY_OMITTED
        identity = replace(
            identity,
            name=name,
            username=username,
            complete=name is not IDENTITY_OMITTED and username is not IDENTITY_OMITTED,
        )
        baseline = capture_identity_baseline(self._conn, identity.dialog_id)
        patch = {
            name: value
            for name, value in (("name", identity.name), ("username", identity.username))
            if value is not IDENTITY_OMITTED
        }
        self._update_canonical_identity(identity.dialog_id, patch, now=observation.observed_at)
        self._publish_dialog_identity(identity, baseline)

    def _dirty_summary_sections(self, merge: EffectiveProfileMerge) -> None:
        dependencies = {
            "full_user.saved_music": ("saved_music",),
            "full_user.stargifts_count": ("saved_gifts",),
            "full_user.common_chats_count": ("common_chats",),
            "full_user.stories": ("current_stories", "pinned_stories"),
            "full_user.stories_pinned_available": ("pinned_stories",),
            "full_user.personal_channel_id": ("personal_channel",),
            "user.photo": ("avatar_history",),
            "full_user.profile_photo": ("avatar_history",),
            "full_user.personal_photo": ("avatar_history",),
            "full_user.fallback_photo": ("avatar_history",),
        }
        sections = {
            section
            for root, targets in dependencies.items()
            if root in merge.after
            and (
                root not in merge.before
                or summary_fingerprint(root, merge.before[root]) != summary_fingerprint(root, merge.after[root])
            )
            and any(path == root or path.startswith(root + ".") for path in merge.accepted_paths)
            for section in targets
        }
        self._conn.executemany(
            "INSERT INTO entity_detail_sections(entity_id,section,status,reason) VALUES (?,?,'pending','profile_summary_changed') "
            "ON CONFLICT(entity_id,section) DO UPDATE SET status='pending',reason='profile_summary_changed'",
            ((merge.snapshot.target_id, section) for section in sections),
        )

    def _journal_collection(
        self, cursor: EntityRefreshCursor, page: ProfileCollectionPage, payload: Mapping[str, object]
    ) -> None:
        published = payload.get("published")
        if payload.get("complete") is not True or not isinstance(published, dict):
            return
        observation = ProfileFieldObservation(
            int(cast(int, published["owner_account_id"])),
            int(cast(int, payload["history_order"])),
            int(cast(float, page.observation.started_at)),
            "rpc",
        )
        context = HistoryObservation(cursor.entity_id, observation, page.observation)
        old = payload.get("previous_published")
        if payload.get("comparable_membership") is True and isinstance(old, dict):
            before = effective_collection_membership(
                self._conn, context, page.section, old, self._published_membership(cursor.entity_id, page.section, old)
            )
            if before is not None:
                append_collection_history(
                    self._conn,
                    CollectionHistoryMerge(
                        context,
                        page.section,
                        before,
                        self._published_membership(cursor.entity_id, page.section, published),
                        True,
                        old,
                    ),
                )
        published["history_event_id"] = collection_history_watermark(self._conn, context)

    def _prepare_observed_commit(
        self, entity_id: int, fields: Mapping[str, object], observation: ProfileFieldObservation | None
    ) -> tuple[dict[str, object], frozenset[str], bool]:
        if observation is None:
            detail = self._read_detail_blob(entity_id) or self._read_entity_stub(entity_id)
            return {**_strip_schema(detail), **fields}, frozenset(fields), False
        base, reset_owner = self._observation_profile_base(entity_id, observation)
        detail, accepted = self._merge_observed_fields(entity_id, base, fields, observation)
        return detail, accepted, reset_owner

    def _finish_observed_commit(
        self,
        entity_id: int,
        accepted: frozenset[str],
        reset_owner: bool,
        observation: ProfileFieldObservation | None,
    ) -> None:
        if observation is None:
            return
        if reset_owner:
            self._reset_unowned_sections(entity_id)
        self._store_field_observations(entity_id, accepted, observation)

    def _advance_after_section_write(
        self,
        cursor: EntityRefreshCursor,
        *,
        now: int,
        detail_revision: int | None,
    ) -> int:
        next_section = self._next_section(cursor)
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
        require_write_transaction(self._conn)
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
        require_write_transaction(self._conn)
        predicate, parameters = self._cursor_predicate(cursor)
        revision_assignment = ", profile_revision=?" if detail_revision is not None else ""
        revision_value = (detail_revision,) if detail_revision is not None else ()
        payload = self.read_section_payload(cursor.entity_id, next_section)
        supplied = (
            payload.get("supplied_cursor")
            if isinstance(payload, dict) and payload.get("supplied_generation") == cursor.generation
            else None
        )
        page_assignment = ", page_cursor_json=?" if self._refresh_has("page_cursor_json") else ""
        page_value = (encode(supplied) if supplied is not None else None,) if page_assignment else ()
        return self._conn.execute(
            "UPDATE entity_profile_refresh_state SET status='pending', retry_at=NULL, reason='refresh_queued', "
            "updated_at=?, next_section=?, acquisition_cursor=0"
            + page_assignment
            + revision_assignment
            + " WHERE "
            + predicate,
            (now, next_section, *page_value, *revision_value, *parameters),
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
        identity_patch: Mapping[str, object] | None = None,
        dialog_identity_observation: DialogIdentityObservation | None = None,
        dialog_identity_baseline_revision: int | None = None,
    ) -> bool:
        canonical = self._read_entity_stub(entity_id)
        encoded_detail = json.dumps({"schema": _DETAIL_SCHEMA, **detail}, separators=(",", ":"))
        metadata_columns, metadata_values = self._detail_metadata(
            owner_account_id=owner_account_id,
            observation_scope=observation_scope,
            ownership_observed=ownership_observed,
        )
        encoded = _EncodedDetail(entity_id, encoded_detail, metadata_columns, metadata_values)
        if self._detail_has("profile_revision"):
            changed = self._write_fenced_detail(
                encoded,
                now=now,
                expected_revision=expected_revision,
            )
        else:
            changed = self._write_legacy_detail(entity_id, encoded_detail, metadata_columns, metadata_values, now=now)
        if changed and identity_patch:
            self._update_canonical_identity(entity_id, identity_patch, now=now, canonical=canonical)
        if changed:
            self._publish_dialog_identity(dialog_identity_observation, dialog_identity_baseline_revision)
        return changed

    def _publish_dialog_identity(
        self,
        observation: DialogIdentityObservation | None,
        baseline_revision: int | None,
    ) -> None:
        if observation is not None:
            publish_dialog_identity(self._conn, observation.dialog_id, observation, baseline_revision)

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
        require_write_transaction(self._conn)
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
        if not self._refresh_has("profile_revision"):
            return False
        refresh_columns = self._columns("entity_profile_refresh_state")
        if "status" not in refresh_columns:
            return False
        select_values = (entity_id, encoded_detail, now, *metadata_values)
        metadata_select = ", " + ", ".join("?" for _ in metadata_columns) if metadata_columns else ""
        changed = self._conn.execute(
            f"INSERT INTO entity_details({', '.join(columns)}) "
            f"SELECT ?, ?, ?, profile_revision + 1{metadata_select} "
            "FROM entity_profile_refresh_state "
            "WHERE entity_id=? AND status IN ('pending', 'failed') AND profile_revision=? "
            "AND NOT EXISTS (SELECT 1 FROM entity_details WHERE entity_id=?)",
            (*select_values, entity_id, expected_revision, entity_id),
        ).rowcount
        return changed == 1

    def _write_legacy_detail(
        self,
        entity_id: int,
        encoded_detail: str,
        metadata_columns: tuple[str, ...],
        metadata_values: tuple[object, ...],
        *,
        now: int,
    ) -> bool:
        require_write_transaction(self._conn)
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
        require_write_transaction(self._conn)
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
        with write_savepoint(self._conn):
            if not self._cursor_matches(cursor):
                return False
            detail = self._pair_detail(cursor, full_profile, personal_channel, now=now)
            if detail is None:
                return False
            self._record_full_pair_measurement(cursor, full_profile, personal_channel, now=now)
            if not self._advance_full_pair_cursor(cursor, now=now):
                raise sqlite3.OperationalError("full user pair cursor advance was rejected")
            return True

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
        with write_savepoint(self._conn):
            if not self._cursor_matches(cursor):
                return False
            detail = self._group_pair_detail(cursor, full_profile, contact_overlap, now=now)
            if detail is None:
                return False
            self._write_group_pair_sections(cursor, detail, full_profile, contact_overlap, now=now)
            self.persist_group_created(cursor.entity_id, full_profile.dialog_created)
            advanced = self._advance_group_full_chat_cursor(
                cursor, now=now, contact_overlap_pending=contact_overlap.status == "pending"
            )
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
        if not self._write_pair_detail(cursor, detail, full_profile, contact_overlap, now=now):
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
        if full_profile.status not in {"fresh", "unavailable"} or contact_overlap.status not in {
            "fresh",
            "unavailable",
            "pending",
        }:
            raise ValueError("invalid legacy group section status")
        for commit in (full_profile, contact_overlap):
            if commit.evidence is not None and commit.evidence.generation != cursor.generation:
                raise ValueError("legacy group evidence generation does not match refresh cursor")

    def _advance_group_full_chat_cursor(
        self, cursor: EntityRefreshCursor, *, now: int, contact_overlap_pending: bool
    ) -> bool:
        require_write_transaction(self._conn)
        predicate, parameters = self._cursor_predicate(cursor)
        assignments = (
            "status='pending', retry_at=NULL, reason='refresh_queued', updated_at=?, "
            "next_section=?, acquisition_cursor=0"
        )
        values: tuple[object, ...] = (now, PROFILE_SECTIONS[2 if contact_overlap_pending else 3])
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
        observation = full_profile.field_observation or personal_channel.field_observation
        if (
            full_profile.field_observation is not None
            and personal_channel.field_observation is not None
            and full_profile.field_observation != personal_channel.field_observation
        ):
            raise ValueError("paired profile field observations must match")
        history_base, history_evidence = self._owned_history_base(cursor.entity_id, observation)
        detail, accepted, reset_owner = self._prepare_observed_commit(
            cursor.entity_id, {**full_profile.detail_patch, **personal_channel.detail_patch}, observation
        )
        if not self._write_pair_detail(cursor, detail, full_profile, personal_channel, now=now, accepted=accepted):
            return None
        self._finish_observed_commit(cursor.entity_id, accepted, reset_owner, observation)
        self._store_semantic_commit(full_profile, history_base, history_evidence)
        self._write_pair_section(cursor.entity_id, "full_profile", full_profile, detail, now=now)
        self._write_pair_section(cursor.entity_id, "personal_channel", personal_channel, detail, now=now)
        return detail

    def _write_pair_detail(  # noqa: PLR0913
        self,
        cursor: EntityRefreshCursor,
        detail: Mapping[str, object],
        full_profile: EntitySectionCommit,
        companion: EntitySectionCommit,
        *,
        now: int,
        accepted: frozenset[str] | None = None,
    ) -> bool:
        owner_account_id, observation_scope, ownership_observed = _commit_metadata(full_profile, companion)
        return self._write_detail(
            cursor.entity_id,
            detail,
            now=now,
            expected_revision=cursor.profile_revision,
            owner_account_id=owner_account_id,
            observation_scope=observation_scope,
            ownership_observed=ownership_observed,
            identity_patch=_combine_identity_patches(
                _accepted_identity_patch(full_profile, accepted), _accepted_identity_patch(companion, accepted)
            ),
            dialog_identity_observation=full_profile.dialog_identity_observation,
            dialog_identity_baseline_revision=full_profile.dialog_identity_baseline_revision,
        )

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
        require_write_transaction(self._conn)
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
                pair_readiness_latency_ms(full_profile, personal_channel),
                now,
                cursor.entity_id,
                cursor.generation,
            ),
        )

    def _advance_full_pair_cursor(self, cursor: EntityRefreshCursor, *, now: int) -> bool:
        require_write_transaction(self._conn)
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
        with write_savepoint(self._conn):
            changed = self._conn.execute(
                "UPDATE entity_profile_refresh_state SET follow_up_required=1, updated_at=? "
                "WHERE entity_id=? AND status IN ('pending', 'failed')",
                (now, entity_id),
            ).rowcount
            if changed == 1:
                return True
            # A completed state is retained by v61 so the next demand can
            # start a fresh, never-reused generation immediately.
            revision_assignment = ""
            revision_value: tuple[object, ...] = ()
            if self._refresh_has("profile_revision") and self._detail_has("profile_revision"):
                revision_assignment = ", profile_revision=?"
                revision_value = (self._profile_revision(entity_id),)
            changed = self._conn.execute(
                "UPDATE entity_profile_refresh_state SET status='pending', retry_at=NULL, "
                "reason='refresh_follow_up', updated_at=?, follow_up_required=0, "
                "generation=generation+1, started_at=?, pair_eligible=?, next_section=?, acquisition_cursor=0"
                + revision_assignment
                + " WHERE entity_id=? AND status='complete'",
                (
                    now,
                    now,
                    int(self._pair_is_eligible(entity_id, now=now)),
                    PROFILE_SECTIONS[0],
                    *revision_value,
                    entity_id,
                ),
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
        require_write_transaction(self._conn)
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
            "pair_eligible=?, follow_up_required=0, next_section=?, acquisition_cursor=0"
            + (",page_cursor_json=NULL" if self._refresh_has("page_cursor_json") else "")
            + revision_assignment
            + " "
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
        require_write_transaction(self._conn)
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
        require_write_transaction(self._conn)
        next_section = self._next_section(cursor)
        if next_section is None and self._refresh_has("generation") and self._refresh_follow_up_required(cursor):
            return self._start_follow_up_generation(cursor, now=now) == 1
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
        if self._refresh_has("page_cursor_json"):
            clauses.append("page_cursor_json IS ?")
            parameters.append(cursor.page_cursor_json)
        return " AND ".join(clauses), tuple(parameters)

    def mark_refresh_rejected(self, entity_id: int, *, now: int, reason: str = "refresh_rejected") -> None:
        """Persist queue rejection without claiming that work was queued."""
        try:
            with write_savepoint(self._conn):
                active = self._conn.execute(
                    "SELECT 1 FROM entity_profile_refresh_state WHERE entity_id=? AND status IN ('pending','failed')",
                    (entity_id,),
                ).fetchone()
                if reason == "refresh_rejected" and active is not None:
                    return
                self._conn.execute(
                    "INSERT INTO entity_profile_refresh_state(entity_id, status, retry_at, reason, updated_at) "
                    "VALUES (?, 'rejected', NULL, ?, ?) ON CONFLICT(entity_id) DO UPDATE SET status=excluded.status, "
                    "retry_at=NULL, reason=excluded.reason, updated_at=excluded.updated_at",
                    (entity_id, reason, now),
                )
                self._conn.executemany(
                    "INSERT OR IGNORE INTO entity_detail_sections(entity_id, section, status, observed_at, reason, payload_json, retry_at) "
                    "VALUES (?, ?, 'unavailable', NULL, ?, NULL, NULL)",
                    ((entity_id, section, reason) for section in self._profile_sections(entity_id)),
                )
                self._conn.execute(
                    "UPDATE entity_detail_sections SET status='unavailable', reason=?, retry_at=NULL "
                    "WHERE entity_id=? AND status <> 'not_applicable'",
                    (reason, entity_id),
                )
        except sqlite3.OperationalError, sqlite3.IntegrityError:
            return

    def mark_refresh_not_found(self, entity_id: int, *, now: int) -> None:
        """Persist terminal resolution failure without leaving pending work."""
        self.mark_refresh_rejected(entity_id, now=now, reason="entity_not_found")

    def mark_section_failure(
        self,
        cursor: EntityRefreshCursor,
        *,
        now: int,
        reason: str,
        retry_at: int,
    ) -> bool:
        """Atomically defer only the section owned by the current durable cursor."""
        with write_savepoint(self._conn):
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
            with write_savepoint(self._conn):
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
        for section in self._profile_sections(entity_id):
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
        del now
        if not _section_applicable(detail, section):
            return "not_applicable"
        if section == "full_profile" or payload is None or observed_at is None:
            return "pending"
        return "fresh"

    def _stored_section_status(self, raw_status: str, observed_at: int | None, *, now: int) -> str:
        del now
        status = raw_status if raw_status in _SECTION_STATUSES else "pending"
        if status == "fresh" and observed_at is None:
            return "pending"
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
        return self._merge_canonical_identity(entity_id, value) if isinstance(value, dict) else {}


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


def _accepted_identity_patch(
    commit: EntitySectionCommit, accepted: frozenset[str] | None
) -> Mapping[str, object] | None:
    if commit.identity_patch is None or accepted is None or commit.field_observation is None:
        return commit.identity_patch
    return {field: value for field, value in commit.identity_patch.items() if field in accepted}


def _profile_section_missing(row: tuple[object, ...]) -> bool:
    return (
        row[1] in {"pending", "stale"}
        or (row[1] == "fresh" and row[2] is None)
        or (row[1] == "unavailable" and row[3] == "refresh_rejected")
    )


def _merge_current_photo(previous: object, incoming: object) -> object:
    if not isinstance(previous, Mapping) or not isinstance(incoming, Mapping):
        return incoming
    if previous.get("photo_id") == incoming.get("photo_id") and incoming.get("date") is None:
        return {**incoming, "date": previous.get("date")}
    return incoming


def _accept_field_observation(
    field: str,
    current: Mapping[str, object],
    previous: tuple[object, object, object] | None,
    observation: ProfileFieldObservation,
) -> bool:
    if previous is not None:
        owner, order, source = previous
        if owner != observation.owner_account_id or not isinstance(order, int) or order >= observation.order:
            return False
        return observation.source != "min" or source == "min"
    return observation.source != "min" or field not in current


def _recompose_profile_membership(detail: dict[str, object]) -> None:
    relationship = {
        field: detail[field] for field in ("contact", "mutual_contact", "close_friend", "blocked") if field in detail
    }
    if relationship:
        detail["my_membership"] = {
            "is_member": bool(relationship.get("contact") or relationship.get("mutual_contact")),
            "is_admin": False,
            "admin_rights": None,
            "relationship": relationship,
        }


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
    return (
        existing is not None
        and str(existing.get("status")) in {"pending", "failed"}
        and _as_int(existing.get("generation")) > 0
    )


def _active_pending_values(
    existing: Mapping[str, object],
    *,
    pair_mode_override: str | None,
) -> dict[str, object]:
    return {
        "generation": _as_int(existing["generation"]),
        "retry_at": existing.get("retry_at"),
        "started_at": existing.get("started_at"),
        "pair_eligible": _as_int(existing.get("pair_eligible")),
        "follow_up_required": 1,
        "profile_revision": _as_int(existing.get("profile_revision")),
        "pair_mode": existing.get("pair_mode") or pair_mode_override,
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
        (
            "status=CASE WHEN excluded.generation=entity_profile_refresh_state.generation "
            "AND entity_profile_refresh_state.status IN ('pending', 'failed') THEN entity_profile_refresh_state.status ELSE 'pending' END"
        ),
        (
            "retry_at=CASE WHEN excluded.generation=entity_profile_refresh_state.generation "
            "AND entity_profile_refresh_state.status IN ('pending', 'failed') THEN entity_profile_refresh_state.retry_at ELSE excluded.retry_at END"
        ),
        (
            "reason=CASE WHEN excluded.generation=entity_profile_refresh_state.generation "
            "AND entity_profile_refresh_state.status IN ('pending', 'failed') THEN entity_profile_refresh_state.reason ELSE excluded.reason END"
        ),
        (
            "updated_at=CASE WHEN excluded.generation=entity_profile_refresh_state.generation "
            "AND entity_profile_refresh_state.status IN ('pending', 'failed') THEN entity_profile_refresh_state.updated_at ELSE excluded.updated_at END"
        ),
        "next_section=CASE WHEN entity_profile_refresh_state.status IN ('pending', 'failed') AND entity_profile_refresh_state.next_section IS NOT NULL THEN entity_profile_refresh_state.next_section ELSE excluded.next_section END",
        "acquisition_cursor=CASE WHEN entity_profile_refresh_state.status IN ('pending', 'failed') AND entity_profile_refresh_state.next_section IS NOT NULL THEN entity_profile_refresh_state.acquisition_cursor ELSE 0 END",
    ]
    updates.extend(
        [
            (
                "pair_mode=CASE WHEN entity_profile_refresh_state.status IN ('pending', 'failed') "
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


def _apply_identity_patch(
    canonical: Mapping[str, object],
    patch: Mapping[str, object],
    *,
    allow_deletions: bool = True,
) -> dict[str, object]:
    """Apply validated identity observations while preserving canonical ids."""
    updated = dict(canonical)
    for field in ("name", "username"):
        if field not in patch:
            continue
        value = patch[field]
        if value is None:
            if allow_deletions:
                updated[field] = None
        elif isinstance(value, str) and value.strip():
            updated[field] = value.strip()
    if "type" in patch:
        parsed = DialogType.parse(patch["type"] if isinstance(patch["type"], (str, DialogType)) else None)
        if parsed is not DialogType.UNKNOWN:
            updated["type"] = parsed.value
    return updated


def _identity_name_normalized(
    canonical: Mapping[str, object],
    identity: Mapping[str, object],
) -> str | None:
    name = identity.get("name")
    if name == canonical.get("name") and isinstance(canonical.get("name_normalized"), str):
        return cast(str, canonical["name_normalized"])
    return latinize(name) if isinstance(name, str) else None


def _combine_identity_patches(
    first: Mapping[str, object] | None,
    second: Mapping[str, object] | None,
) -> Mapping[str, object] | None:
    if not first and not second:
        return None
    combined: dict[str, object] = {}
    if first:
        combined.update(first)
    if second:
        combined.update(second)
    return combined


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


def pair_readiness_latency_ms(
    full_profile: EntitySectionCommit,
    personal_channel: EntitySectionCommit,
) -> float | None:
    """Measure the evidence span in milliseconds, subtracting before float conversion."""
    evidences = [commit.evidence for commit in (full_profile, personal_channel) if commit.evidence is not None]
    starts = [evidence.observation_started_at for evidence in evidences]
    completes = [evidence.observation_completed_at for evidence in evidences]
    if not starts or any(value is None for value in starts + completes):
        return None
    return float((max(cast(list[int], completes)) - min(cast(list[int], starts))) * 1000)


def _first_dirty_section(sections: tuple[str, ...], previous: tuple[object, ...] | None) -> str:
    first = min(sections, key=PROFILE_SECTIONS.index)
    if previous is not None and previous[0] in PROFILE_SECTIONS:
        first = min((first, str(previous[0])), key=PROFILE_SECTIONS.index)
    return first


_PROFILE_ADMISSION_STATE = "profile_admission_v2"
_PROFILE_ADMISSION_TYPES = ("user", "bot", "User", "Bot")
_PROFILE_EXCLUDED_PEERS = frozenset({777000, 1271266957})


def _read_profile_admission_state(conn: sqlite3.Connection, owner: int) -> dict[str, object]:
    row = cast(
        tuple[object, ...] | None,
        conn.execute("SELECT value FROM daemon_state WHERE key=?", (_PROFILE_ADMISSION_STATE,)).fetchone(),
    )
    state = cast(dict[str, object], json.loads(cast(str, row[0]))) if row else {}
    if state.get("account") != owner or state.get("version") != NORMALIZATION_VERSION:
        return {
            "account": owner,
            "version": NORMALIZATION_VERSION,
            "phase": 0,
            "last_peer": 0,
            "completed": False,
            "directory_generation": 0,
            "pending_directory_generation": 0,
        }
    return state


def _save_profile_admission_state(conn: sqlite3.Connection, state: dict[str, object]) -> None:
    conn.execute(
        "INSERT INTO daemon_state(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_PROFILE_ADMISSION_STATE, json.dumps(state, separators=(",", ":"))),
    )


def _profile_peer_key(owner: int, peer: int) -> str:
    return f"profile_admission:{owner}:{NORMALIZATION_VERSION}:{peer}"


def profile_peer_managed(conn: sqlite3.Connection, owner: int, peer: int) -> bool:
    return (
        conn.execute("SELECT 1 FROM daemon_state WHERE key=?", (_profile_peer_key(owner, peer),)).fetchone() is not None
    )


def personal_profile_peer(conn: sqlite3.Connection, owner: int, peer: int) -> bool:
    if peer <= 0 or peer == owner or peer in _PROFILE_EXCLUDED_PEERS:
        return False
    dialog = cast(tuple[str] | None, conn.execute("SELECT type FROM dialogs WHERE dialog_id=?", (peer,)).fetchone())
    entity = cast(tuple[str] | None, conn.execute("SELECT type FROM entities WHERE id=?", (peer,)).fetchone())
    kinds = {str(value[0]).lower() for value in (dialog, entity) if value is not None}
    if kinds & {"service", "replies"}:
        return False
    return (
        (dialog is not None and dialog[0].lower() in {"user", "bot"})
        or profile_peer_managed(conn, owner, peer)
        or conn.execute("SELECT 1 FROM messages WHERE dialog_id=? LIMIT 1", (peer,)).fetchone() is not None
    )


def admit_profile_peer(
    conn: sqlite3.Connection, owner: int, peer: int, *, now: int, pair_mode: str = "enabled"
) -> bool:
    """Admit a locally evidenced private peer once per normalization version."""
    require_write_transaction(conn)
    if peer <= 0 or peer == owner or peer in _PROFILE_EXCLUDED_PEERS or profile_peer_managed(conn, owner, peer):
        return False
    if not _ensure_profile_private_identity(conn, peer, now):
        return False
    # Queue-only adapter: this path never evaluates receipt reuse or TTL.
    repo = EntityProfileRepository(conn, section_ttl_seconds=300)
    sections = repo._missing_profile_sections(peer)
    semantic = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT source,normalization_version FROM entity_profile_semantic_snapshots "
            "WHERE owner_account_id=? AND entity_id=?",
            (owner, peer),
        ).fetchone(),
    )
    if semantic is None or semantic[0] == "legacy" or semantic[1] != NORMALIZATION_VERSION:
        sections = repo._profile_sections(peer)
    queued = (
        False
        if _profile_retains_acquisition(conn, owner, peer)
        else repo.request_section_refresh(peer, sections, now=now, pair_mode=pair_mode)
    )
    conn.execute("INSERT INTO daemon_state(key,value) VALUES (?,?)", (_profile_peer_key(owner, peer), str(now)))
    return queued


def _profile_retains_acquisition(conn: sqlite3.Connection, owner: int, peer: int) -> bool:
    row = cast(
        tuple[str, str | None, int | None] | None,
        conn.execute(
            "SELECT r.status,r.reason,d.profile_owner_account_id FROM entity_profile_refresh_state r "
            "LEFT JOIN entity_details d ON d.entity_id=r.entity_id WHERE r.entity_id=?",
            (peer,),
        ).fetchone(),
    )
    if row is None or (row[2] if row[2] is not None else bound_profile_account(conn)) != owner:
        return False
    return row[0] in {"pending", "failed"} or (row[0] == "rejected" and row[1] != "refresh_rejected")


def _ensure_profile_private_identity(conn: sqlite3.Connection, peer: int, now: int) -> bool:
    row = cast(
        tuple[object, ...] | None, conn.execute("SELECT type,name FROM dialogs WHERE dialog_id=?", (peer,)).fetchone()
    )
    kind, name = (str(row[0]), cast(str | None, row[1])) if row else ("unknown", None)
    existing = cast(tuple[object, ...] | None, conn.execute("SELECT type FROM entities WHERE id=?", (peer,)).fetchone())
    if (existing and str(existing[0]).lower() in {"service", "replies"}) or kind.lower() in {"service", "replies"}:
        return False
    if existing is None:
        ensure_entity_stub(conn, EntitySnapshot(peer, kind, name, None, None, now))
    return True


def request_profile_directory_pass(conn: sqlite3.Connection, owner: int, generation: int) -> None:
    require_write_transaction(conn)
    state = _read_profile_admission_state(conn, owner)
    state["pending_directory_generation"] = max(int(cast(int, state["pending_directory_generation"])), generation)
    _save_profile_admission_state(conn, state)


def _reconcile_profile_admission(conn: sqlite3.Connection, owner: int, *, now: int, limit: int, pair_mode: str) -> int:
    """Checkpoint at most 32 examined peers, including already admitted peers."""
    with write_savepoint(conn):
        state = _read_profile_admission_state(conn, owner)
        if state["completed"]:
            if state["pending_directory_generation"] == state["directory_generation"]:
                return 0
            state.update(phase=0, last_peer=0, completed=False)
        if state["phase"] == 0 and state["last_peer"] == 0:
            state["directory_generation"] = state["pending_directory_generation"]
        examined = 0
        while examined < min(limit, 32) and int(cast(int, state["phase"])) < len(_PROFILE_ADMISSION_TYPES) + 1:
            phase, last = int(cast(int, state["phase"])), int(cast(int, state["last_peer"]))
            peers = _profile_admission_candidates(conn, phase, last, min(limit, 32) - examined)
            if not peers:
                state.update(phase=phase + 1, last_peer=0)
                continue
            for peer in peers:
                admit_profile_peer(conn, owner, peer, now=now, pair_mode=pair_mode)
                state["last_peer"] = peer
                examined += 1
        state["completed"] = state["phase"] == len(_PROFILE_ADMISSION_TYPES) + 1
        _save_profile_admission_state(conn, state)
        return examined


def _profile_admission_candidates(conn: sqlite3.Connection, phase: int, last: int, limit: int) -> list[int]:
    if phase < len(_PROFILE_ADMISSION_TYPES):
        return [
            int(cast(int, row[0]))
            for row in cast(
                list[tuple[int]],
                conn.execute(
                    "SELECT dialog_id FROM dialogs WHERE type=? AND dialog_id>? ORDER BY dialog_id LIMIT ?",
                    (_PROFILE_ADMISSION_TYPES[phase], last, limit),
                ).fetchall(),
            )
        ]
    row = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT dialog_id FROM messages WHERE dialog_id>? AND dialog_id>0 ORDER BY dialog_id LIMIT 1", (last,)
        ).fetchone(),
    )
    return [int(cast(int, row[0]))] if row else []


def bound_profile_account(conn: sqlite3.Connection) -> int | None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='dialog_directory_state'").fetchone() is None:
        return None
    row = cast(
        tuple[object, ...] | None,
        conn.execute("SELECT account_id FROM dialog_directory_state WHERE singleton=1").fetchone(),
    )
    return int(cast(int, row[0])) if row and row[0] is not None else None
