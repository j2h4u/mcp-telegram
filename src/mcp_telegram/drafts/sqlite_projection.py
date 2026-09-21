"""SQLite implementation of the account-fenced current draft projection."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import cast

from mcp_telegram.drafts.contracts import (
    DraftApplyResult,
    DraftComposition,
    DraftDisposition,
    DraftObservation,
    DraftObservationSource,
    DraftReference,
    DraftScope,
    SnapshotCoverage,
)

_NORMALIZATION_VERSION = 1
_MAX_RECOVERY_REASON_LENGTH = 256
_CURRENT_VALUE_COLUMNS = (
    "state",
    "text",
    "entities_json",
    "reply_to_json",
    "media_json",
    "suggested_post_json",
    "rich_message_json",
    "effect_id",
    "no_webpage",
    "invert_media",
    "composition_complete",
    "source_kind",
    "source_observed_at",
    "observation_started_at",
    "observation_completed_at",
    "normalization_version",
)


class DraftAccountFenceError(RuntimeError):
    """A draft write belongs to an account other than the active runtime."""


class SQLiteDraftProjection:
    """Persist current drafts with account fencing and snapshot revision fences."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def bind_account(self, account_id: int) -> None:
        """Bind this writer runtime to one authenticated Telegram account."""
        _validate_account_id(account_id)
        with self._write_transaction():
            self._conn.execute("INSERT OR IGNORE INTO draft_projection_runtime(singleton) VALUES (1)")
            self._conn.execute(
                "UPDATE draft_projection_runtime SET account_id=?,recovery_due_at=NULL,recovery_claimed_at=NULL WHERE singleton=1",
                (account_id,),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO draft_sync_state(account_id,status,coverage_status) VALUES (?,'unknown','unknown')",
                (account_id,),
            )

    def apply_realtime(self, observation: DraftObservation) -> DraftApplyResult:
        """Atomically apply one delivered observation or request recovery on ambiguity."""
        if observation.source is not DraftObservationSource.REALTIME:
            raise ValueError("apply_realtime requires a realtime observation")
        with self._write_transaction():
            self._require_active_account(observation.scope.account_id)
            if observation.ambiguity:
                self._set_recovery_needed("ambiguous_realtime_observation", _as_unix(observation.observed_at))
                return DraftApplyResult(accepted=False, ambiguous=True)
            existing = self._row_for_scope(observation.scope)
            candidate = _row_values(observation, source_kind=_source_kind(observation))
            if existing is not None:
                source_observed_at = _database_int(existing["source_observed_at"])
                incoming_observed_at = _database_int(candidate["source_observed_at"])
                if incoming_observed_at < source_observed_at:
                    return DraftApplyResult(accepted=False, revision=_database_int(existing["projection_revision"]))
                if incoming_observed_at == source_observed_at:
                    if _row_matches(existing, candidate):
                        return DraftApplyResult(accepted=True, revision=_database_int(existing["projection_revision"]))
                    self._set_recovery_needed("equal_realtime_observation_conflict", incoming_observed_at)
                    return DraftApplyResult(accepted=False, ambiguous=True)
            revision = self._next_revision()
            self._upsert_current(observation.scope, candidate, revision)
            self._conn.execute(
                "UPDATE draft_sync_state SET status='ready' WHERE account_id=?",
                (observation.scope.account_id,),
            )
            return DraftApplyResult(accepted=True, revision=revision)

    def snapshot_baselines(self, account_id: int) -> Mapping[DraftScope, int]:
        """Capture durable per-scope revisions before one GetAllDrafts request."""
        _validate_account_id(account_id)
        captured_at = int(time.time())
        with self._write_transaction():
            self._require_active_account(account_id)
            self._conn.execute("DELETE FROM draft_snapshot_baseline WHERE account_id=?", (account_id,))
            rows = cast(
                list[tuple[int, int, int, int]],
                self._conn.execute(
                    "SELECT dialog_id,top_message_id,subdialog_peer_id,projection_revision "
                    "FROM draft_current WHERE account_id=?",
                    (account_id,),
                ).fetchall(),
            )
            baselines = {
                DraftScope(
                    account_id, dialog_id, _none_for_zero(top_message_id), _none_for_zero(subdialog_peer_id)
                ): revision
                for dialog_id, top_message_id, subdialog_peer_id, revision in rows
            }
            self._conn.executemany(
                "INSERT INTO draft_snapshot_baseline("
                "account_id,dialog_id,top_message_id,subdialog_peer_id,baseline_revision,captured_at) VALUES (?,?,?,?,?,?)",
                [
                    (
                        scope.account_id,
                        scope.dialog_id,
                        _zero_for_none(scope.top_message_id),
                        _zero_for_none(scope.subdialog_peer_id),
                        revision,
                        captured_at,
                    )
                    for scope, revision in baselines.items()
                ],
            )
            self._conn.execute(
                "UPDATE draft_sync_state SET status='recovering',coverage_status='incomplete',"
                "observation_started_at=?,observation_completed_at=NULL WHERE account_id=?",
                (captured_at, account_id),
            )
            return baselines

    def apply_snapshot(
        self,
        observations: Sequence[DraftObservation],
        coverage: SnapshotCoverage,
        baselines: Mapping[DraftScope, int],
    ) -> DraftApplyResult:
        """Publish one fenced snapshot and only then infer its durable absences."""
        _validate_account_id(coverage.account_id)
        _validate_snapshot_inputs(observations, coverage, baselines)
        completed_at = int(time.time())
        with self._write_transaction():
            self._require_active_account(coverage.account_id)
            deduplicated = _deduplicate_snapshot(observations)
            if deduplicated is None:
                self._set_recovery_needed("conflicting_snapshot_observations", completed_at)
                return DraftApplyResult(accepted=False, ambiguous=True)
            changed_revision = self._apply_snapshot_observations(deduplicated, baselines, completed_at)
            if coverage.authoritative:
                changed_revision = self._infer_snapshot_absences(
                    coverage.account_id, deduplicated, baselines, completed_at, changed_revision
                )
                self._mark_snapshot_complete(coverage.account_id, completed_at)
            else:
                self._set_recovery_needed("snapshot_not_authoritative", completed_at)
            self._conn.execute("DELETE FROM draft_snapshot_baseline WHERE account_id=?", (coverage.account_id,))
            return DraftApplyResult(accepted=coverage.authoritative, revision=changed_revision)

    def _apply_snapshot_observations(
        self,
        observations: Sequence[DraftObservation],
        baselines: Mapping[DraftScope, int],
        completed_at: int,
    ) -> int | None:
        changed_revision: int | None = None
        for observation in observations:
            current = self._row_for_scope(observation.scope)
            baseline = baselines.get(observation.scope)
            if current is not None and (baseline is None or _database_int(current["projection_revision"]) != baseline):
                continue
            candidate = _row_values(observation, source_kind=_source_kind(observation), completed_at=completed_at)
            if current is not None and _row_matches(current, candidate):
                continue
            changed_revision = self._next_revision()
            self._upsert_current(observation.scope, candidate, changed_revision)
        return changed_revision

    def _infer_snapshot_absences(
        self,
        account_id: int,
        observations: Sequence[DraftObservation],
        baselines: Mapping[DraftScope, int],
        completed_at: int,
        changed_revision: int | None,
    ) -> int | None:
        known = cast(
            list[tuple[int, int, int, int]],
            self._conn.execute(
                "SELECT dialog_id,top_message_id,subdialog_peer_id,projection_revision FROM draft_current WHERE account_id=?",
                (account_id,),
            ).fetchall(),
        )
        observed_scopes = {observation.scope for observation in observations}
        for dialog_id, top_message_id, subdialog_peer_id, revision in known:
            scope = DraftScope(account_id, dialog_id, _none_for_zero(top_message_id), _none_for_zero(subdialog_peer_id))
            if scope in observed_scopes or baselines.get(scope) != revision:
                continue
            changed_revision = self._next_revision()
            self._upsert_current(scope, _cleared_row(completed_at), changed_revision)
        return changed_revision

    def _mark_snapshot_complete(self, account_id: int, completed_at: int) -> None:
        self._conn.execute(
            "UPDATE draft_sync_state SET status='ready',coverage_status='complete',"
            "observation_completed_at=?,reason=NULL WHERE account_id=?",
            (completed_at, account_id),
        )
        self._conn.execute(
            "UPDATE draft_projection_runtime SET recovery_due_at=NULL,recovery_claimed_at=NULL WHERE singleton=1"
        )

    def mark_recovery_needed(self, *, reason: str, observed_at: datetime) -> None:
        """Persist a retryable recovery requirement without inventing draft absence."""
        with self._write_transaction():
            self._set_recovery_needed(reason, _as_unix(observed_at))

    def recovery_due_at(self) -> float | None:
        """Return the active account's recovery due time, if recovery is pending."""
        row = cast(
            tuple[object | None, object | None] | None,
            self._conn.execute(
                "SELECT account_id,recovery_due_at FROM draft_projection_runtime WHERE singleton=1"
            ).fetchone(),
        )
        if row is None or row[0] is None or row[1] is None:
            return None
        return float(cast(int, row[1]))

    def claim_recovery(self, *, now: float) -> bool:
        """Claim a due recovery run exactly once for the active account."""
        now_int = _finite_non_negative_unix(now)
        with self._write_transaction():
            row = cast(
                tuple[object | None, object | None] | None,
                self._conn.execute(
                    "SELECT account_id,recovery_due_at FROM draft_projection_runtime WHERE singleton=1"
                ).fetchone(),
            )
            if row is None or row[0] is None or row[1] is None or int(cast(int, row[1])) > now_int:
                return False
            account_id = int(cast(int, row[0]))
            self._conn.execute(
                "UPDATE draft_projection_runtime SET recovery_due_at=NULL,recovery_claimed_at=? WHERE singleton=1",
                (now_int,),
            )
            self._conn.execute(
                "UPDATE draft_sync_state SET status='recovering',coverage_status='incomplete' WHERE account_id=?",
                (account_id,),
            )
            return True

    @contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        """Yield a stable local read snapshot without opening a second database."""
        if self._conn.in_transaction:
            yield self._conn
            return
        self._conn.execute("BEGIN")
        try:
            yield self._conn
        finally:
            self._conn.rollback()

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        owns_transaction = not self._conn.in_transaction
        if owns_transaction:
            self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if owns_transaction:
                self._conn.rollback()
            raise
        else:
            if owns_transaction:
                self._conn.commit()

    def _require_active_account(self, account_id: int) -> None:
        row = cast(
            tuple[object | None] | None,
            self._conn.execute("SELECT account_id FROM draft_projection_runtime WHERE singleton=1").fetchone(),
        )
        if row is None or row[0] is None or int(cast(int, row[0])) != account_id:
            raise DraftAccountFenceError("draft projection account is not bound to this runtime")

    def _next_revision(self) -> int:
        self._conn.execute(
            "UPDATE draft_projection_runtime SET projection_revision=projection_revision+1 WHERE singleton=1"
        )
        row = cast(
            tuple[object] | None,
            self._conn.execute("SELECT projection_revision FROM draft_projection_runtime WHERE singleton=1").fetchone(),
        )
        if row is None:
            raise RuntimeError("draft projection runtime row is missing")
        return int(cast(int, row[0]))

    def _set_recovery_needed(self, reason: str, observed_at: int) -> None:
        if not reason or len(reason) > _MAX_RECOVERY_REASON_LENGTH:
            raise ValueError("draft recovery reason must be a non-empty string of at most 256 characters")
        row = cast(
            tuple[object | None] | None,
            self._conn.execute("SELECT account_id FROM draft_projection_runtime WHERE singleton=1").fetchone(),
        )
        if row is None or row[0] is None:
            raise DraftAccountFenceError("draft recovery requires a bound account")
        account_id = int(cast(int, row[0]))
        self._conn.execute(
            "UPDATE draft_projection_runtime SET recovery_due_at=?,recovery_claimed_at=NULL WHERE singleton=1",
            (observed_at,),
        )
        self._conn.execute(
            "UPDATE draft_sync_state SET status='recovery_needed',coverage_status='unknown',reason=?,"
            "observation_completed_at=? WHERE account_id=?",
            (reason, observed_at, account_id),
        )

    def _row_for_scope(self, scope: DraftScope) -> dict[str, object] | None:
        cursor = self._conn.execute(
            "SELECT * FROM draft_current WHERE account_id=? AND dialog_id=? AND top_message_id=? AND subdialog_peer_id=?",
            (
                scope.account_id,
                scope.dialog_id,
                _zero_for_none(scope.top_message_id),
                _zero_for_none(scope.subdialog_peer_id),
            ),
        )
        row = cast(tuple[object, ...] | None, cursor.fetchone())
        if row is None:
            return None
        if cursor.description is None:
            raise RuntimeError("draft projection query did not describe its row")
        return dict(zip((column[0] for column in cursor.description), row, strict=True))

    def _upsert_current(self, scope: DraftScope, values: dict[str, object], revision: int) -> None:
        assignments = ",".join(f"{column}=excluded.{column}" for column in _CURRENT_VALUE_COLUMNS)
        placeholders = ",".join("?" for _ in range(5 + len(_CURRENT_VALUE_COLUMNS)))
        self._conn.execute(
            "INSERT INTO draft_current(account_id,dialog_id,top_message_id,subdialog_peer_id,"
            f"{','.join(_CURRENT_VALUE_COLUMNS)},projection_revision) VALUES ({placeholders}) "
            "ON CONFLICT(account_id,dialog_id,top_message_id,subdialog_peer_id) DO UPDATE SET "
            f"{assignments},projection_revision=excluded.projection_revision",
            (
                scope.account_id,
                scope.dialog_id,
                _zero_for_none(scope.top_message_id),
                _zero_for_none(scope.subdialog_peer_id),
                *(values[column] for column in _CURRENT_VALUE_COLUMNS),
                revision,
            ),
        )


def _row_values(
    observation: DraftObservation, *, source_kind: str, completed_at: int | None = None
) -> dict[str, object]:
    observed_at = _as_unix(observation.observed_at)
    completed = observed_at if completed_at is None else completed_at
    if observation.disposition is DraftDisposition.TOMBSTONE:
        return {
            "state": "empty",
            "text": None,
            "entities_json": None,
            "reply_to_json": None,
            "media_json": None,
            "suggested_post_json": None,
            "rich_message_json": None,
            "effect_id": None,
            "no_webpage": None,
            "invert_media": None,
            "composition_complete": 1,
            "source_kind": source_kind,
            "source_observed_at": observed_at,
            "observation_started_at": observed_at,
            "observation_completed_at": completed,
            "normalization_version": _NORMALIZATION_VERSION,
        }
    composition = observation.composition
    if composition is None:
        raise ValueError("present draft observation is missing its composition")
    return _present_row(composition, source_kind, observed_at, completed)


def _present_row(
    composition: DraftComposition, source_kind: str, observed_at: int, completed_at: int
) -> dict[str, object]:
    return {
        "state": "present",
        "text": composition.text,
        "entities_json": _json_value(
            [
                {
                    "kind": entity.kind,
                    "language": entity.language,
                    "length_utf16": entity.length_utf16,
                    "offset_utf16": entity.offset_utf16,
                    "reference_id": entity.reference_id,
                }
                for entity in composition.entities
            ]
        ),
        "reply_to_json": _json_value(
            {
                "monoforum": _reference_value(composition.monoforum),
                "quote": _reference_value(composition.quote),
                "reply": _reference_value(composition.reply),
                "story": _reference_value(composition.story),
            }
        ),
        "media_json": _json_value(_reference_value(composition.media)),
        "suggested_post_json": _json_value(_reference_value(composition.suggested_post)),
        "rich_message_json": _json_value(_reference_value(composition.rich)),
        "effect_id": composition.effect_id,
        "no_webpage": _nullable_bool(composition.no_webpage),
        "invert_media": _nullable_bool(composition.invert_media),
        "composition_complete": int(composition.completeness.value == "complete"),
        "source_kind": source_kind,
        "source_observed_at": observed_at,
        "observation_started_at": observed_at,
        "observation_completed_at": completed_at,
        "normalization_version": _NORMALIZATION_VERSION,
    }


def _cleared_row(observed_at: int) -> dict[str, object]:
    return {
        "state": "cleared",
        "text": None,
        "entities_json": None,
        "reply_to_json": None,
        "media_json": None,
        "suggested_post_json": None,
        "rich_message_json": None,
        "effect_id": None,
        "no_webpage": None,
        "invert_media": None,
        "composition_complete": 1,
        "source_kind": "snapshot_absence",
        "source_observed_at": observed_at,
        "observation_started_at": observed_at,
        "observation_completed_at": observed_at,
        "normalization_version": _NORMALIZATION_VERSION,
    }


def _source_kind(observation: DraftObservation) -> str:
    if observation.source is DraftObservationSource.REALTIME:
        return "realtime_present" if observation.disposition is DraftDisposition.PRESENT else "realtime_empty"
    return "snapshot_present" if observation.disposition is DraftDisposition.PRESENT else "snapshot_empty"


def _reference_value(reference: DraftReference | None) -> dict[str, object] | None:
    if reference is None:
        return None
    return {
        "identifier": reference.identifier,
        "kind": reference.kind,
        "message_id": reference.message_id,
        "peer_id": reference.peer_id,
    }


def _json_value(value: object) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _row_matches(existing: Mapping[str, object], candidate: Mapping[str, object]) -> bool:
    return all(
        existing[column] == value
        for column, value in candidate.items()
        if column not in {"observation_started_at", "observation_completed_at"}
    )


def _deduplicate_snapshot(observations: Sequence[DraftObservation]) -> tuple[DraftObservation, ...] | None:
    result: dict[DraftScope, DraftObservation] = {}
    for observation in observations:
        existing = result.get(observation.scope)
        if existing is None:
            result[observation.scope] = observation
        elif _row_values(existing, source_kind=_source_kind(existing)) != _row_values(
            observation, source_kind=_source_kind(observation)
        ):
            return None
    return tuple(result.values())


def _validate_snapshot_inputs(
    observations: Sequence[DraftObservation], coverage: SnapshotCoverage, baselines: Mapping[DraftScope, int]
) -> None:
    for scope, revision in baselines.items():
        if (
            scope.account_id != coverage.account_id
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
        ):
            raise ValueError("snapshot baselines must contain non-negative revisions for the covered account")
    for observation in observations:
        if (
            observation.scope.account_id != coverage.account_id
            or observation.source is not DraftObservationSource.SNAPSHOT
        ):
            raise ValueError("snapshot observations must belong to the covered account and snapshot source")


def _validate_account_id(account_id: int) -> None:
    if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id == 0:
        raise ValueError("account_id must be a non-zero integer")


def _as_unix(value: datetime) -> int:
    return _finite_non_negative_unix(value.timestamp())


def _finite_non_negative_unix(value: float) -> int:
    if value != value or value == float("inf") or value == float("-inf") or value < 0:
        raise ValueError("timestamp must be finite and non-negative")
    return int(value)


def _zero_for_none(value: int | None) -> int:
    return 0 if value is None else value


def _none_for_zero(value: int) -> int | None:
    return None if value == 0 else value


def _nullable_bool(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _database_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError("draft projection database value is not an integer")
    return value


__all__ = ["DraftAccountFenceError", "SQLiteDraftProjection"]
