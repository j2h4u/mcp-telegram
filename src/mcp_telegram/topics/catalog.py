"""Durable progress and completeness receipts for topic enumeration."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import cast

from ..observation_order import allocate_observation_order
from ..sync_transactions import write_savepoint
from .contracts import TopicCursor, TopicPage, TopicSourceUnavailableError
from .sqlite_repository import SQLiteTopicSnapshotRepository

type _PageState = tuple[int, int, int | None, int | None, str]
type _ScanRow = tuple[int | None, int | None, int, int | None, int, int, int]
type _CoverageRow = tuple[int | None, int, int | None, str | None, int | None]
_RESTART_SCAN_REASONS = frozenset(
    {
        "topic_count_changed",
        "topic_order_changed",
        "topic_page_repeated",
        "topic_count_inconsistent",
        "topic_cursor_repeated",
        "topic_cursor_missing",
    }
)


@dataclass(frozen=True, slots=True)
class TopicScan:
    dialog_id: int
    generation: int
    revision: int
    observation_order: int
    cursor: TopicCursor


class TopicCatalogRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _enabled(self, dialog_id: int) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM full_history_enrollment WHERE dialog_id=? AND enabled=1", (dialog_id,)
            ).fetchone()
            is not None
        )

    def request(self, dialog_id: int, reason: str) -> None:
        if not self._enabled(dialog_id):
            return
        with write_savepoint(self._conn):
            self._conn.execute(
                """INSERT INTO topic_catalog_state(dialog_id,reason) VALUES (?,?)
                ON CONFLICT(dialog_id) DO UPDATE SET revision=revision+1,
                pending=1, reason=excluded.reason, retry_at=NULL""",
                (dialog_id, reason),
            )

    def bootstrap(self) -> None:
        with write_savepoint(self._conn):
            self._conn.execute(
                """INSERT OR IGNORE INTO topic_catalog_state(dialog_id,reason)
                SELECT dialog_id,'bootstrap' FROM full_history_enrollment WHERE enabled=1"""
            )

    def observe_capability(self, dialog_id: int, capable: bool, observation_order: int) -> bool:
        """Apply a supplied complete entity's support flag without acquiring a catalog."""
        with write_savepoint(self._conn):
            if not self._enabled(dialog_id):
                return False
            self._conn.execute("INSERT OR IGNORE INTO topic_catalog_state(dialog_id) VALUES (?)", (dialog_id,))
            previous = cast(
                tuple[int | None, int, str | None],
                self._conn.execute(
                    "SELECT applicable,capability_observation_order,reason FROM topic_catalog_state WHERE dialog_id=?",
                    (dialog_id,),
                ).fetchone(),
            )
            if previous[1] >= observation_order:
                return False
            changed = previous[0] != int(capable) or previous[2] == "source_access_unavailable"
            self._conn.execute(
                "UPDATE topic_catalog_state SET applicable=?,capability_observation_order=? WHERE dialog_id=?",
                (int(capable), observation_order, dialog_id),
            )
            if changed:
                self.request(dialog_id, "topic_capability_changed")
                self._reset_scan(dialog_id)
                if not capable:
                    self._conn.execute(
                        "UPDATE topic_catalog_state SET pending=0,reason='not_topic_capable' WHERE dialog_id=?",
                        (dialog_id,),
                    )
            return changed

    def request_recovery(self) -> None:
        with write_savepoint(self._conn):
            self.bootstrap()
            self._conn.execute(
                "UPDATE topic_catalog_state SET revision=revision+1,pending=1,reason='recovery_gap',retry_at=NULL "
                "WHERE (applicable IS NOT 0 OR pending=1) AND dialog_id IN "
                "(SELECT dialog_id FROM full_history_enrollment WHERE enabled=1)"
            )

    def next_pending(self) -> tuple[int, int] | None:
        return cast(
            tuple[int, int] | None,
            self._conn.execute(
                """SELECT e.dialog_id,COALESCE(s.retry_at,0) FROM full_history_enrollment e
            LEFT JOIN topic_catalog_state s ON s.dialog_id=e.dialog_id
            WHERE e.enabled=1 AND (s.dialog_id IS NULL OR s.pending=1)
            AND COALESCE(s.reason,'')<>'source_access_unavailable'
            ORDER BY COALESCE(s.retry_at,0),e.dialog_id LIMIT 1"""
            ).fetchone(),
        )

    def pending_rows(self) -> list[tuple[int, int]]:
        return cast(
            list[tuple[int, int]],
            self._conn.execute(
                """SELECT e.dialog_id,COALESCE(s.retry_at,0) FROM full_history_enrollment e
                LEFT JOIN topic_catalog_state s ON s.dialog_id=e.dialog_id
                WHERE e.enabled=1 AND (s.dialog_id IS NULL OR s.pending=1)
                AND COALESCE(s.reason,'')<>'source_access_unavailable'
                ORDER BY COALESCE(s.retry_at,0),e.dialog_id""",
            ).fetchall(),
        )

    def begin_page(self, dialog_id: int) -> TopicScan | None:
        with write_savepoint(self._conn):
            if not self._enabled(dialog_id):
                return None
            self.bootstrap()
            row = cast(
                _ScanRow | None,
                self._conn.execute(
                    "SELECT generation,scan_revision,revision,offset_date,offset_id,offset_topic,pending "
                    "FROM topic_catalog_state WHERE dialog_id=?",
                    (dialog_id,),
                ).fetchone(),
            )
            if row is None or not row[6]:
                return None
            generation, revision = row[0], row[1]
            if generation is None:
                generation = allocate_observation_order(self._conn)
                revision = row[2]
                self._conn.execute(
                    "UPDATE topic_catalog_state SET generation=?,scan_revision=? WHERE dialog_id=?",
                    (generation, revision, dialog_id),
                )
            assert revision is not None
            return TopicScan(
                dialog_id,
                generation,
                revision,
                allocate_observation_order(self._conn),
                TopicCursor(row[3], row[4], row[5]),
            )

    def apply_page(self, scan: TopicScan, page: TopicPage) -> bool:
        """Apply a page and report whether this scan revision still owns it."""
        return self.apply_page_outcome(scan, page)[1]

    def apply_page_outcome(self, scan: TopicScan, page: TopicPage) -> tuple[bool, bool, str | None]:
        """Return acceptance, terminal state and its revision from the write transaction."""
        with write_savepoint(self._conn):
            if not self._enabled(scan.dialog_id):
                return False, False, None
            state = cast(
                _PageState | None,
                self._conn.execute(
                    "SELECT generation,revision,expected_count,order_by_create_date,cursors "
                    "FROM topic_catalog_state WHERE dialog_id=?",
                    (scan.dialog_id,),
                ).fetchone(),
            )
            if state is None or state[0] != scan.generation:
                return False, False, None
            ids, seen = self._apply_page_topics(scan, page, state)
            complete = len(seen.union(ids)) == page.count
            if complete:
                accepted = self._complete(scan, state[1])
                return accepted, accepted, self._revision(scan.dialog_id) if accepted else None
            cursor = page.next_cursor
            if cursor is None:
                raise TopicSourceUnavailableError("topic_cursor_missing")
            cursors = cast(list[list[int | None]], json.loads(state[4]))
            cursors.append([cursor.offset_date, cursor.offset_id, cursor.offset_topic])
            self._conn.execute(
                """UPDATE topic_catalog_state SET expected_count=?,order_by_create_date=?,
                offset_date=?,offset_id=?,offset_topic=?,cursors=?,
                applicable=CASE WHEN capability_observation_order<? THEN 1 ELSE applicable END,
                capability_observation_order=MAX(capability_observation_order,?),retry_at=NULL
                WHERE dialog_id=?""",
                (
                    page.count,
                    int(page.order_by_create_date),
                    cursor.offset_date,
                    cursor.offset_id,
                    cursor.offset_topic,
                    json.dumps(cursors),
                    scan.observation_order,
                    scan.observation_order,
                    scan.dialog_id,
                ),
            )
            return True, False, self._revision(scan.dialog_id)

    def _apply_page_topics(self, scan: TopicScan, page: TopicPage, state: _PageState) -> tuple[list[int], set[int]]:
        ids = [topic.topic_id for topic in page.topics] + list(page.deleted_ids)
        seen = {
            row[0]
            for row in cast(
                list[tuple[int]],
                self._conn.execute(
                    "SELECT topic_id FROM topic_metadata WHERE dialog_id=? AND seen_generation=?",
                    (scan.dialog_id, scan.generation),
                ).fetchall(),
            )
        }
        self._validate_page(state, page, ids, seen)
        repository = SQLiteTopicSnapshotRepository(self._conn)
        repository.upsert_topics(scan.dialog_id, page.topics, observation_order=scan.observation_order)
        self._conn.executemany(
            "UPDATE topic_metadata SET seen_generation=?,catalog_present=1 WHERE dialog_id=? AND topic_id=?",
            ((scan.generation, scan.dialog_id, topic_id) for topic_id in ids),
        )
        # Deleted constructors carry no title; preserve any retained descriptive facts.
        self._conn.executemany(
            """INSERT INTO topic_metadata(dialog_id,topic_id,title,is_general,is_deleted,updated_at,observation_order,seen_generation)
            VALUES (?,?,'',0,1,?,?,?) ON CONFLICT(dialog_id,topic_id) DO UPDATE SET
            is_deleted=1,observation_order=excluded.observation_order
            WHERE topic_metadata.observation_order < excluded.observation_order""",
            (
                (scan.dialog_id, topic_id, int(time.time()), scan.observation_order, scan.generation)
                for topic_id in page.deleted_ids
            ),
        )
        return ids, seen

    def _revision(self, dialog_id: int) -> str | None:
        row = cast(
            tuple[object, ...] | None,
            self._conn.execute(
                "SELECT generation,revision,offset_date,offset_id,offset_topic,pending,applicable "
                "FROM topic_catalog_state WHERE dialog_id=?",
                (dialog_id,),
            ).fetchone(),
        )
        return None if row is None else repr(tuple(row))

    @staticmethod
    def _validate_page(state: _PageState, page: TopicPage, ids: list[int], seen: set[int]) -> None:
        TopicCatalogRepository._validate_traversal(state, page)
        if len(set(ids)) != len(ids) or (ids and not set(ids).difference(seen)):
            raise TopicSourceUnavailableError("topic_page_repeated")
        if len(seen.union(ids)) > page.count or (not ids and len(seen) != page.count):
            raise TopicSourceUnavailableError("topic_count_inconsistent")

    @staticmethod
    def _validate_traversal(state: _PageState, page: TopicPage) -> None:
        if page.count < 0 or (state[2] is not None and state[2] != page.count):
            raise TopicSourceUnavailableError("topic_count_changed")
        if state[3] is not None and bool(state[3]) != page.order_by_create_date:
            raise TopicSourceUnavailableError("topic_order_changed")
        cursor = page.next_cursor
        if cursor is not None and [cursor.offset_date, cursor.offset_id, cursor.offset_topic] in json.loads(state[4]):
            raise TopicSourceUnavailableError("topic_cursor_repeated")

    def _complete(self, scan: TopicScan, revision: int) -> bool:
        if revision == scan.revision:
            self._conn.execute(
                "UPDATE topic_metadata SET catalog_present=0 WHERE dialog_id=? "
                "AND seen_generation<>? AND observation_order<?",
                (scan.dialog_id, scan.generation, scan.generation),
            )
            self._conn.execute(
                "UPDATE topic_catalog_state SET completed_at=?,pending=0,reason=NULL,applicable=1, "
                "capability_observation_order=MAX(capability_observation_order,?) "
                "WHERE dialog_id=? AND revision=?",
                (int(time.time()), scan.observation_order, scan.dialog_id, scan.revision),
            )
        self._reset_scan(scan.dialog_id)
        return revision == scan.revision

    def _reset_scan(self, dialog_id: int) -> None:
        self._conn.execute(
            "UPDATE topic_catalog_state SET generation=NULL,scan_revision=NULL,offset_date=NULL,"
            "offset_id=0,offset_topic=0,expected_count=NULL,order_by_create_date=NULL,cursors='[]' "
            "WHERE dialog_id=?",
            (dialog_id,),
        )

    def fail(self, scan: TopicScan, reason: str, retry_at: int | None) -> None:
        with write_savepoint(self._conn):
            if reason == "source_access_unavailable":
                self._conn.execute(
                    "UPDATE topic_catalog_state SET reason=?,retry_at=NULL,capability_observation_order=? "
                    "WHERE dialog_id=? AND generation=? "
                    "AND revision=? AND capability_observation_order<=?",
                    (
                        reason,
                        scan.observation_order,
                        scan.dialog_id,
                        scan.generation,
                        scan.revision,
                        scan.observation_order,
                    ),
                )
                return
            changed = self._conn.execute(
                "UPDATE topic_catalog_state SET reason=?,retry_at=? WHERE dialog_id=? AND generation=?",
                (reason, retry_at, scan.dialog_id, scan.generation),
            ).rowcount
            if changed and reason in _RESTART_SCAN_REASONS:
                self._reset_scan(scan.dialog_id)

    def mark_not_applicable(self, scan: TopicScan) -> bool:
        return self.mark_not_applicable_outcome(scan)[0]

    def mark_not_applicable_outcome(self, scan: TopicScan) -> tuple[bool, str | None]:
        with write_savepoint(self._conn):
            if not self._enabled(scan.dialog_id):
                return False, None
            changed = self._conn.execute(
                "UPDATE topic_catalog_state SET applicable=0,pending=0,reason='not_topic_capable',retry_at=NULL "
                ", capability_observation_order=? WHERE dialog_id=? AND revision=? AND generation=? "
                "AND capability_observation_order<=?",
                (scan.observation_order, scan.dialog_id, scan.revision, scan.generation, scan.observation_order),
            ).rowcount
            active = (
                self._conn.execute(
                    "SELECT 1 FROM topic_catalog_state WHERE dialog_id=? AND generation=?",
                    (scan.dialog_id, scan.generation),
                ).fetchone()
                is not None
            )
            if changed or active:
                self._reset_scan(scan.dialog_id)
            accepted = bool(changed)
            return accepted, self._revision(scan.dialog_id) if accepted else None

    def coverage(self, dialog_id: int) -> dict[str, object]:
        row = cast(
            _CoverageRow | None,
            self._conn.execute(
                "SELECT completed_at,pending,applicable,reason,retry_at FROM topic_catalog_state WHERE dialog_id=?",
                (dialog_id,),
            ).fetchone(),
        )
        observed = (
            self._conn.execute("SELECT 1 FROM topic_metadata WHERE dialog_id=? LIMIT 1", (dialog_id,)).fetchone()
            is not None
        )
        row = row or (None, 0, None, None, None)
        return {
            "coverage": self._coverage_state(row, observed),
            "completed_at": row[0],
            "pending": bool(row[1]) and row[3] != "source_access_unavailable" and self._enabled(dialog_id),
            "reason": row[3],
            "retry_at": row[4],
        }

    @staticmethod
    def _coverage_state(row: _CoverageRow, observed: bool) -> str:
        if row[2] == 0 and not row[1]:
            return "not_applicable"
        if row[0] is not None:
            return "partial" if row[1] else "complete"
        return "partial" if observed else "not_observed"
