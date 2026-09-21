"""Read-only adapter for the dedicated, account-fenced draft projection."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from ..drafts.contracts import DraftCoverageFreshness, DraftCoveragePresence
from ..models import DraftReadRecord

_DRAFT_TABLE = "draft_current"
_DRAFT_STATE_TABLE = "draft_sync_state"


@dataclass(frozen=True, slots=True)
class DraftCoverage:
    """Receipt accompanying a local draft read.

    Missing persistence, an unbound account, and an unfinished snapshot are
    intentionally distinct from a confirmed absent draft.
    """

    presence: DraftCoveragePresence
    freshness: DraftCoverageFreshness
    observed_at: int | None
    observation_source: str | None
    absence_basis: str | None
    continuity_reason: str | None
    composition_complete: bool | None
    projection_fingerprint: str | None

    def to_wire(self) -> dict[str, object]:
        return {
            "presence": self.presence.value,
            "freshness": self.freshness.value,
            "observed_at": self.observed_at,
            "observation_source": self.observation_source,
            "absence_basis": self.absence_basis,
            "continuity_reason": self.continuity_reason,
            "composition_complete": self.composition_complete,
        }


@dataclass(frozen=True, slots=True)
class _DraftReadScope:
    dialog_id: int
    topic_id: int | None
    since_utc: int | None
    until_utc: int | None


def draft_projection_available(conn: sqlite3.Connection) -> bool:
    """Return whether both required projection tables are installed."""
    raw_rows = cast(
        list[tuple[object]],
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN (?, ?)",
            (_DRAFT_TABLE, _DRAFT_STATE_TABLE),
        ).fetchall(),
    )
    names = {str(row[0]) for row in raw_rows}
    return names == {_DRAFT_TABLE, _DRAFT_STATE_TABLE}


def _optional_int(value: object | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    return int(cast(int | str, value))


def _optional_bool(value: object | None) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _normalized_json(
    value: object | None, *, array: bool = False
) -> dict[str, object] | tuple[dict[str, object], ...] | None:
    decoded = _decode_json(value)
    if array:
        return _json_objects(decoded)
    return decoded if isinstance(decoded, dict) else None


def _decode_json(value: object | None) -> object | None:
    if value is None or not isinstance(value, str):
        return None
    try:
        return cast(object, json.loads(value))
    except json.JSONDecodeError:
        return None


def _json_objects(value: object | None) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        return ()
    return tuple(cast(dict[str, object], item) for item in value)


def _record_from_row(row: Mapping[str, object]) -> DraftReadRecord:
    topic_value = _optional_int(row["top_message_id"])
    subdialog_value = _optional_int(row["subdialog_peer_id"])
    state = str(row["state"])
    if state not in {"present", "empty", "cleared"}:
        raise ValueError("draft_current.state is invalid")
    entities = _normalized_json(row["entities_json"], array=True)
    assert isinstance(entities, tuple)
    return DraftReadRecord(
        account_id=int(cast(int | str, row["account_id"])),
        dialog_id=int(cast(int | str, row["dialog_id"])),
        topic_id=None if topic_value == 0 else topic_value,
        subdialog_peer_id=None if subdialog_value == 0 else subdialog_value,
        state=cast(Literal["present", "empty", "cleared"], state),
        text=cast(str | None, row["text"]),
        entities=entities,
        reply_to=cast(dict[str, object] | None, _normalized_json(row["reply_to_json"])),
        media=cast(dict[str, object] | None, _normalized_json(row["media_json"])),
        suggested_post=cast(dict[str, object] | None, _normalized_json(row["suggested_post_json"])),
        rich_message=cast(dict[str, object] | None, _normalized_json(row["rich_message_json"])),
        effect_id=_optional_int(row["effect_id"]),
        no_webpage=_optional_bool(row["no_webpage"]),
        invert_media=_optional_bool(row["invert_media"]),
        composition_complete=bool(row["composition_complete"]),
        source_kind=str(row["source_kind"]),
        source_observed_at=_optional_int(row["source_observed_at"]),
        observation_started_at=int(cast(int | str, row["observation_started_at"])),
        observation_completed_at=int(cast(int | str, row["observation_completed_at"])),
        projection_revision=int(cast(int | str, row["projection_revision"])),
        normalization_version=str(row["normalization_version"]),
    )


def draft_message_key(record: DraftReadRecord) -> str:
    """Return an opaque, stable identifier derived from the immutable scope."""
    raw_scope = f"{record.account_id}:{record.dialog_id}:{record.topic_id or 0}:{record.subdialog_peer_id or 0}"
    return "draft_" + hashlib.sha256(raw_scope.encode()).hexdigest()[:24]


def draft_projection_fingerprint(records: Sequence[DraftReadRecord]) -> str:
    """Bind a cursor to every mutable projection row in its selected scope."""
    payload = "|".join(
        f"{draft_message_key(record)}:{record.projection_revision}" for record in sorted(records, key=draft_message_key)
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def read_drafts(  # noqa: PLR0913
    conn: sqlite3.Connection,
    *,
    account_id: int | None,
    dialog_id: int,
    topic_id: int | None,
    sender_id: int | None,
    sender_name: str | None,
    since_utc: int | None,
    until_utc: int | None,
) -> tuple[list[DraftReadRecord], DraftCoverage]:
    """Read the current draft scopes locally and return an honest coverage receipt."""
    early_result = _early_read_result(conn, account_id, sender_id, sender_name)
    if early_result is not None:
        return early_result
    assert account_id is not None
    scope = _DraftReadScope(dialog_id, topic_id, since_utc, until_utc)
    records = _draft_records(conn, account_id, scope)
    return records, _draft_coverage(conn, account_id, records)


def _early_read_result(
    conn: sqlite3.Connection,
    account_id: int | None,
    sender_id: int | None,
    sender_name: str | None,
) -> tuple[list[DraftReadRecord], DraftCoverage] | None:
    if account_id is None or not draft_projection_available(conn):
        return [], DraftCoverage(
            DraftCoveragePresence.UNKNOWN,
            DraftCoverageFreshness.UNKNOWN,
            None,
            None,
            None,
            "projection_unavailable",
            None,
            None,
        )
    if sender_id is not None and sender_id != account_id:
        return [], DraftCoverage(
            DraftCoveragePresence.UNKNOWN,
            DraftCoverageFreshness.UNKNOWN,
            None,
            None,
            None,
            "sender_filter_excludes_author_only_drafts",
            None,
            None,
        )
    if sender_name is not None:
        return [], DraftCoverage(
            DraftCoveragePresence.UNKNOWN,
            DraftCoverageFreshness.UNKNOWN,
            None,
            None,
            None,
            "sender_name_not_locally_resolvable",
            None,
            None,
        )
    return None


def _draft_records(
    conn: sqlite3.Connection,
    account_id: int,
    scope: _DraftReadScope,
) -> list[DraftReadRecord]:
    clauses = ["account_id = :account_id", "dialog_id = :dialog_id"]
    params: dict[str, object] = {"account_id": account_id, "dialog_id": scope.dialog_id}
    if scope.topic_id is not None:
        clauses.append("top_message_id = :topic_id")
        params["topic_id"] = scope.topic_id
    if scope.since_utc is not None:
        clauses.append("observation_completed_at >= :since_utc")
        params["since_utc"] = scope.since_utc
    if scope.until_utc is not None:
        clauses.append("observation_completed_at < :until_utc")
        params["until_utc"] = scope.until_utc
    rows = cast(
        list[Mapping[str, object]],
        conn.execute(
            "SELECT * FROM draft_current WHERE "
            + " AND ".join(clauses)
            + " ORDER BY observation_completed_at DESC, top_message_id ASC, subdialog_peer_id ASC",
            params,
        ).fetchall(),
    )
    return [_record_from_row(row) for row in rows]


def _draft_coverage(
    conn: sqlite3.Connection,
    account_id: int,
    records: list[DraftReadRecord],
) -> DraftCoverage:
    state_row = _draft_sync_state(conn, account_id)
    if state_row is None or _optional_int(state_row["account_id"]) != account_id:
        return _unfenced_draft_coverage(records)
    return _stateful_draft_coverage(state_row, records)


def _draft_sync_state(conn: sqlite3.Connection, account_id: int) -> Mapping[str, object] | None:
    return cast(
        Mapping[str, object] | None,
        conn.execute(
            "SELECT account_id, status, coverage_status, observation_completed_at, reason "
            "FROM draft_sync_state WHERE account_id = ?",
            (account_id,),
        ).fetchone(),
    )


def _unfenced_draft_coverage(records: list[DraftReadRecord]) -> DraftCoverage:
    latest = _latest_record(records)
    return DraftCoverage(
        DraftCoveragePresence.PRESENT
        if any(record.state == "present" for record in records)
        else DraftCoveragePresence.UNKNOWN,
        DraftCoverageFreshness.UNKNOWN,
        latest.observation_completed_at if latest is not None else None,
        latest.source_kind if latest is not None else None,
        None,
        "account_fence_unbound",
        _all_compositions_complete(records),
        draft_projection_fingerprint(records),
    )


def _stateful_draft_coverage(state_row: Mapping[str, object], records: list[DraftReadRecord]) -> DraftCoverage:
    fresh = _draft_is_fresh(state_row)
    present = [record for record in records if record.state == "present"]
    absence = [record for record in records if record.state in {"empty", "cleared"}]
    latest = _latest_record(records)
    return DraftCoverage(
        _draft_presence(present, absence, fresh),
        _draft_freshness(state_row, fresh),
        latest.observation_completed_at if latest is not None else _optional_int(state_row["observation_completed_at"]),
        latest.source_kind if latest is not None else None,
        _absence_basis(absence),
        cast(str | None, state_row["reason"]),
        _composition_completeness(latest),
        draft_projection_fingerprint(records),
    )


def _latest_record(records: list[DraftReadRecord]) -> DraftReadRecord | None:
    return max(records, key=lambda record: record.observation_completed_at, default=None)


def _absence_basis(records: list[DraftReadRecord]) -> str | None:
    if not records:
        return None
    return "explicit_empty" if records[0].state == "empty" else "complete_snapshot_absence"


def _composition_completeness(record: DraftReadRecord | None) -> bool | None:
    return record.composition_complete if record is not None else None


def _all_compositions_complete(records: list[DraftReadRecord]) -> bool | None:
    return all(record.composition_complete for record in records) if records else None


def _draft_is_fresh(state_row: Mapping[str, object]) -> bool:
    return str(state_row["status"]) == "ready" and str(state_row["coverage_status"]) == "complete"


def _draft_presence(
    present: list[DraftReadRecord], absence: list[DraftReadRecord], fresh: bool
) -> DraftCoveragePresence:
    if present:
        return DraftCoveragePresence.PRESENT
    if absence and fresh:
        return DraftCoveragePresence.ABSENT
    return DraftCoveragePresence.UNKNOWN


def _draft_freshness(state_row: Mapping[str, object], fresh: bool) -> DraftCoverageFreshness:
    if fresh:
        return DraftCoverageFreshness.CURRENT
    if str(state_row["status"]) in {"unknown", "recovery_needed", "recovering", "failed"}:
        return DraftCoverageFreshness.STALE
    return DraftCoverageFreshness.UNKNOWN
