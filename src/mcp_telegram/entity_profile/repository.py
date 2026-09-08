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
class PriorBlob:
    detail: dict[str, object]
    observed_at: int | None


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
            try:
                self._conn.execute("DELETE FROM entity_profile_refresh_state WHERE entity_id = ?", (entity_id,))
            except sqlite3.OperationalError:
                pass
            self._conn.commit()
        except sqlite3.OperationalError:
            # Compatibility fixtures can expose only entity_details.
            return

    def save_detail(
        self,
        entity_id: int,
        detail: Mapping[str, object],
        *,
        now: int,
        section_outcomes: Mapping[str, str] | None = None,
    ) -> None:
        """Store last-good detail and section observations atomically."""
        outcomes = dict(section_outcomes or {})
        if detail.get("_full_fetch_ok") is False:
            outcomes.setdefault("full_profile", "refresh_failed")
        previous = PriorBlob(
            detail=self._read_detail_blob(entity_id),
            observed_at=self._read_detail_fetched_at(entity_id),
        )
        merged = _merge_failed_sections(dict(detail), previous.detail, outcomes)
        payload = {"schema": _DETAIL_SCHEMA, **merged}
        payload.pop("_full_fetch_ok", None)
        entity_type = str(payload.get("type", "unknown"))
        has_section_storage = self._has_section_storage()
        with self._conn:
            upsert_entity_snapshots(
                self._conn,
                [
                    EntitySnapshot(
                        entity_id=entity_id,
                        entity_type=entity_type,
                        name=_optional_text(payload.get("name")),
                        username=_optional_text(payload.get("username")),
                        name_normalized=None,
                        updated_at=now,
                    )
                ],
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO entity_details(entity_id, detail_json, fetched_at) VALUES (?, ?, ?)",
                (entity_id, json.dumps(payload, separators=(",", ":")), now),
            )
            if has_section_storage:
                self._write_sections(
                    entity_id,
                    payload,
                    now=now,
                    outcomes=outcomes,
                    previous=previous,
                )
                self._conn.execute("DELETE FROM entity_profile_refresh_state WHERE entity_id = ?", (entity_id,))

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
            self._conn.executemany(
                "INSERT INTO entity_detail_sections(entity_id, section, status, observed_at, reason, payload_json, retry_at) "
                "VALUES (?, ?, 'pending', NULL, ?, NULL, NULL) "
                "ON CONFLICT(entity_id, section) DO UPDATE SET status='pending', reason=excluded.reason",
                ((entity_id, section, reason) for section in PROFILE_SECTIONS),
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            return

    def mark_refresh_queued(self, entity_id: int, *, reason: str = "refresh_queued") -> None:
        """Clear rejection state and restore an honest queued reason."""
        try:
            self._conn.execute("DELETE FROM entity_profile_refresh_state WHERE entity_id = ?", (entity_id,))
            self._conn.execute(
                "UPDATE entity_detail_sections SET status='pending', reason=?, retry_at=NULL "
                "WHERE entity_id=? AND status IN ('pending', 'stale', 'unavailable')",
                (reason, entity_id),
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            return

    def mark_refresh_rejected(self, entity_id: int, *, now: int, reason: str = "refresh_rejected") -> None:
        """Persist queue rejection without claiming that work was queued."""
        try:
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
            self._conn.commit()
        except sqlite3.OperationalError:
            return

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

    def _write_sections(
        self,
        entity_id: int,
        detail: Mapping[str, object],
        *,
        now: int,
        outcomes: Mapping[str, str],
        previous: PriorBlob,
    ) -> None:
        existing = self._read_section_rows(entity_id)
        legacy_detail = _strip_schema(previous.detail)
        legacy_observed_at = previous.observed_at
        values: list[tuple[int, str, str, int | None, str | None, str | None, int | None]] = []
        for section in PROFILE_SECTIONS:
            failure = outcomes.get(section)
            old_row = existing.get(section)
            if failure:
                old_observed_at = cast(int | None, old_row[2]) if old_row is not None else legacy_observed_at
                old_payload = (
                    cast(str | None, old_row[4])
                    if old_row is not None
                    else _encode_payload(_section_payload(legacy_detail, section))
                )
                values.append(
                    (
                        entity_id,
                        section,
                        "stale" if old_observed_at is not None else "unavailable",
                        old_observed_at,
                        failure,
                        old_payload,
                        None,
                    )
                )
                continue
            payload = _section_payload(detail, section)
            if not _section_applicable(detail, section):
                status = "not_applicable"
                observed_at = now
                reason = None
            elif payload is None:
                status = "unavailable"
                observed_at = None
                reason = "section_unavailable"
            else:
                status = "fresh"
                observed_at = now
                reason = None
            values.append((entity_id, section, status, observed_at, reason, _encode_payload(payload), None))
        self._conn.executemany(
            "INSERT INTO entity_detail_sections(entity_id, section, status, observed_at, reason, payload_json, retry_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(entity_id, section) DO UPDATE SET status=excluded.status, observed_at=excluded.observed_at, "
            "reason=excluded.reason, payload_json=excluded.payload_json, retry_at=NULL",
            values,
        )

    def _has_section_storage(self) -> bool:
        row = cast(
            tuple[int] | None,
            self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entity_detail_sections'"
            ).fetchone(),
        )
        return row is not None

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

    def _read_detail_fetched_at(self, entity_id: int) -> int | None:
        try:
            row = cast(
                tuple[object, ...] | None,
                self._conn.execute(
                    "SELECT fetched_at FROM entity_details WHERE entity_id = ?",
                    (entity_id,),
                ).fetchone(),
            )
        except sqlite3.OperationalError:
            return None
        value = row[0] if row else None
        return int(value) if isinstance(value, int) else None

    def _read_section_rows(self, entity_id: int) -> dict[str, tuple[object, ...]]:
        try:
            rows = cast(
                list[tuple[object, ...]],
                self._conn.execute(
                    "SELECT section, status, observed_at, reason, payload_json, retry_at "
                    "FROM entity_detail_sections WHERE entity_id = ?",
                    (entity_id,),
                ).fetchall(),
            )
        except sqlite3.OperationalError:
            return {}
        return {str(row[0]): row for row in rows}


def _normalise_entity_type(value: str) -> str:
    return DialogType.parse(value).value


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


def _merge_failed_sections(
    detail: dict[str, object],
    previous: Mapping[str, object],
    outcomes: Mapping[str, str],
) -> dict[str, object]:
    """Merge last-good values for failed sections into the compatibility blob."""
    keys = {
        "common_chats": ("common_chats",),
        "contact_overlap": ("contacts_subscribed", "contacts_subscribed_partial", "contacts_reason"),
        "avatar_history": ("avatar_history", "avatar_count"),
        "personal_channel": ("personal_channel", "personal_channel_unavailable_reason"),
    }
    if outcomes.get("full_profile"):
        all_section_keys = {key for keys_for_section in keys.values() for key in keys_for_section}
        detail.update(
            {
                key: value
                for key, value in previous.items()
                if key not in {"schema", "id", "type", "name", "username"} | all_section_keys
            }
        )
    for section, section_keys in keys.items():
        if outcomes.get(section):
            for key in section_keys:
                if key in previous:
                    detail[key] = previous[key]
    return detail


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
