"""SQLite repository for progressive entity profile data.

``entity_details`` remains the compatibility blob. Section rows are additive
metadata and payload storage; old databases and test fixtures without the
table continue to work through the blob fallback.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from ..entity_store import EntitySnapshot, upsert_entity_snapshots
from ..models import DialogType
from .contracts import PROFILE_SECTIONS

_DETAIL_SCHEMA = 1
_SECTION_STATUSES = {"fresh", "stale", "pending", "unavailable", "not_applicable"}


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


@dataclass(frozen=True, slots=True)
class EntitySectionCommit:
    detail_patch: Mapping[str, object]
    status: str = "fresh"
    reason: str | None = None
    payload: object | None = None


class EntityProfileRepository:
    """Read and write entity profiles without making Telegram calls."""

    def __init__(self, conn: sqlite3.Connection, *, section_ttl_seconds: int) -> None:
        self._conn = conn
        self._section_ttl_seconds = max(1, int(section_ttl_seconds))

    def read(self, entity_id: int, *, now: int) -> StoredProfile | None:
        detail, observed_at = self._read_primary_detail(entity_id)
        if not detail:
            return None
        sections = self._read_sections(entity_id, detail, now=now, observed_at=observed_at)
        return StoredProfile(detail=detail, observed_at=observed_at, sections=sections)

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
            self._conn.commit()
        except sqlite3.OperationalError:
            # Compatibility fixtures can expose only entity_details.
            return

    def refresh_state(self, entity_id: int, *, now: int) -> dict[str, object] | None:
        """Return a durable unresolved-refresh state while it is relevant."""
        try:
            row = cast(
                tuple[object, object, object] | None,
                self._conn.execute(
                    "SELECT status, retry_at, reason FROM entity_profile_refresh_state WHERE entity_id = ?",
                    (entity_id,),
                ).fetchone(),
            )
        except sqlite3.OperationalError:
            return None
        if row is None:
            return None
        status, retry_at, reason = row
        if str(status) == "rejected" or (isinstance(retry_at, int) and retry_at > now):
            return {"status": str(status), "retry_at": retry_at, "reason": str(reason)}
        return None

    def mark_pending(self, entity_id: int, *, now: int, reason: str = "refresh_queued") -> None:
        """Make pending explicit where the additive section table is present."""
        try:
            with self._conn:
                self._upsert_pending_refresh(entity_id, now=now, reason=reason)
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

    def _upsert_pending_refresh(self, entity_id: int, *, now: int, reason: str) -> None:
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
            row = cast(
                tuple[int, str, int, int | None] | None,
                self._conn.execute(
                    """
                    SELECT entity_id, next_section, acquisition_cursor, retry_at
                    FROM entity_profile_refresh_state
                    WHERE status IN ('pending', 'failed')
                      AND (retry_at IS NULL OR retry_at <= ?)
                    ORDER BY COALESCE(retry_at, 0), updated_at, entity_id
                    LIMIT 1
                    """,
                    (now,),
                ).fetchone(),
            )
        except sqlite3.OperationalError:
            return None
        return EntityRefreshCursor(*row) if row is not None else None

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
            changed = self._conn.execute(
                "UPDATE entity_profile_refresh_state SET status='pending', retry_at=NULL, "
                "reason='refresh_in_progress', updated_at=?, acquisition_cursor=? "
                "WHERE entity_id=? AND status IN ('pending', 'failed') "
                "AND next_section=? AND acquisition_cursor=?",
                (
                    now,
                    next_acquisition_cursor,
                    cursor.entity_id,
                    cursor.next_section,
                    cursor.acquisition_cursor,
                ),
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
            encoded_detail = json.dumps({"schema": _DETAIL_SCHEMA, **detail}, separators=(",", ":"))
            self._conn.execute(
                "INSERT INTO entity_details(entity_id, detail_json, fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT(entity_id) DO UPDATE SET detail_json=excluded.detail_json, fetched_at=excluded.fetched_at",
                (cursor.entity_id, encoded_detail, now),
            )
            section_payload = (
                _section_payload(detail, cursor.next_section) if commit.payload is None else commit.payload
            )
            observed_at = now if commit.status in {"fresh", "not_applicable"} else None
            self._conn.execute(
                """
                INSERT INTO entity_detail_sections(
                    entity_id, section, status, observed_at, reason, payload_json, retry_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(entity_id, section) DO UPDATE SET
                    status=excluded.status, observed_at=excluded.observed_at, reason=excluded.reason,
                    payload_json=excluded.payload_json, retry_at=NULL
                """,
                (
                    cursor.entity_id,
                    cursor.next_section,
                    commit.status,
                    observed_at,
                    commit.reason,
                    _encode_payload(section_payload),
                ),
            )
            next_section = _next_profile_section(cursor.next_section)
            if next_section is None:
                changed = self._conn.execute(
                    "DELETE FROM entity_profile_refresh_state "
                    "WHERE entity_id=? AND next_section=? AND acquisition_cursor=?",
                    (cursor.entity_id, cursor.next_section, cursor.acquisition_cursor),
                ).rowcount
            else:
                changed = self._conn.execute(
                    "UPDATE entity_profile_refresh_state SET status='pending', retry_at=NULL, reason='refresh_queued', "
                    "updated_at=?, next_section=?, acquisition_cursor=0 "
                    "WHERE entity_id=? AND next_section=? AND acquisition_cursor=?",
                    (
                        now,
                        next_section,
                        cursor.entity_id,
                        cursor.next_section,
                        cursor.acquisition_cursor,
                    ),
                ).rowcount
        return changed == 1

    def _cursor_matches(self, cursor: EntityRefreshCursor) -> bool:
        row = cast(
            tuple[int] | None,
            self._conn.execute(
                "SELECT 1 FROM entity_profile_refresh_state "
                "WHERE entity_id=? AND status IN ('pending', 'failed') "
                "AND next_section=? AND acquisition_cursor=?",
                (cursor.entity_id, cursor.next_section, cursor.acquisition_cursor),
            ).fetchone(),
        )
        return row is not None

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
            changed = self._conn.execute(
                "UPDATE entity_profile_refresh_state SET status='failed', retry_at=?, reason=?, updated_at=? "
                "WHERE entity_id=? AND status IN ('pending', 'failed') "
                "AND next_section=? AND acquisition_cursor=?",
                (
                    retry_at,
                    reason,
                    now,
                    cursor.entity_id,
                    cursor.next_section,
                    cursor.acquisition_cursor,
                ),
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
            section_name, value = self._read_section(section, by_name, detail, now=now, observed_at=observed_at)
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

    def _read_section(
        self,
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
        return section_name, {
            "status": status,
            "observed_at": section_observed_at,
            "reason": reason,
            "data": payload,
        }

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
