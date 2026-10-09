"""SQLite adapter for the canonical ``topic_metadata`` snapshot."""

from __future__ import annotations

import sqlite3
import time
from typing import cast

from ..observation_order import allocate_observation_order
from ..sync_transactions import require_write_transaction, write_savepoint
from .contracts import TopicFact
from .ports import TopicMetadataRepository, TopicSnapshotRepository

_UPSERT_TOPIC_SQL = """
INSERT INTO topic_metadata
    (dialog_id, topic_id, title, top_message_id,
     is_general, is_deleted, updated_at,
     icon_emoji_id, icon_emoji, icon_color,
     pinned, hidden, snapshot_at, date, observation_order)
VALUES
    (:dialog_id, :topic_id, :title, NULL,
     :is_general, 0, :updated_at,
     :icon_emoji_id, :icon_emoji, :icon_color,
     0, 0, :snapshot_at, :date, :observation_order)
ON CONFLICT(dialog_id, topic_id) DO UPDATE SET
    title          = COALESCE(excluded.title, topic_metadata.title),
    icon_emoji_id  = COALESCE(excluded.icon_emoji_id, topic_metadata.icon_emoji_id),
    icon_emoji     = COALESCE(excluded.icon_emoji, topic_metadata.icon_emoji),
    icon_color     = COALESCE(excluded.icon_color, topic_metadata.icon_color),
    is_general     = excluded.is_general,
    updated_at     = excluded.updated_at,
    snapshot_at    = excluded.snapshot_at,
    date           = COALESCE(excluded.date, topic_metadata.date),
    observation_order = excluded.observation_order
WHERE topic_metadata.observation_order < excluded.observation_order
"""


class SQLiteTopicMetadataRepository(TopicSnapshotRepository, TopicMetadataRepository):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def begin_snapshot(self) -> int:
        with write_savepoint(self._conn):
            return allocate_observation_order(self._conn)

    def upsert_topics(
        self, dialog_id: int, topics: tuple[TopicFact, ...], *, observation_order: int | None = None
    ) -> None:
        if observation_order is None:
            observation_order = self.begin_snapshot()
        now = int(time.time())
        rows = (
            {
                "dialog_id": dialog_id,
                "topic_id": topic.topic_id,
                "title": topic.title,
                "is_general": int(topic.is_general),
                "icon_emoji_id": topic.icon_emoji_id,
                "icon_emoji": topic.icon_emoji,
                "icon_color": topic.icon_color,
                "updated_at": now,
                "snapshot_at": now,
                "date": topic.date,
                "observation_order": observation_order,
            }
            for topic in topics
        )
        with write_savepoint(self._conn):
            self._conn.executemany(_UPSERT_TOPIC_SQL, rows)

    def apply_topic_create(  # noqa: PLR0913
        self,
        dialog_id: int,
        topic_id: int,
        *,
        title: str,
        icon_emoji_id: int | None,
        date: int | None,
        observed_at: int,
        observation_order: int | None = None,
    ) -> None:
        require_write_transaction(self._conn)
        if observation_order is None:
            observation_order = allocate_observation_order(self._conn)
        self._conn.execute(
            """
            INSERT INTO topic_metadata
                (dialog_id, topic_id, title, top_message_id,
                 is_general, is_deleted, updated_at,
                 icon_emoji_id, pinned, hidden, snapshot_at, date, observation_order)
            VALUES (?, ?, ?, NULL, 0, 0, ?, ?, 0, 0, ?, ?, ?)
            ON CONFLICT(dialog_id, topic_id) DO UPDATE SET
                title = COALESCE(excluded.title, topic_metadata.title),
                icon_emoji_id = COALESCE(excluded.icon_emoji_id, topic_metadata.icon_emoji_id),
                updated_at = excluded.updated_at,
                snapshot_at = excluded.snapshot_at,
                date = COALESCE(excluded.date, topic_metadata.date),
                observation_order = excluded.observation_order
            WHERE topic_metadata.observation_order < excluded.observation_order
            """,
            (dialog_id, topic_id, title, observed_at, icon_emoji_id, observed_at, date, observation_order),
        )

    def apply_topic_edit(  # noqa: PLR0913
        self,
        dialog_id: int,
        topic_id: int,
        *,
        title: str | None,
        icon_emoji_id: int | None,
        hidden: bool | None,
        observed_at: int,
        observation_order: int | None = None,
    ) -> None:
        require_write_transaction(self._conn)
        if observation_order is None:
            observation_order = allocate_observation_order(self._conn)
        self._conn.execute(
            "UPDATE topic_metadata SET title=COALESCE(?, title), "
            "icon_emoji_id=COALESCE(?, icon_emoji_id), hidden=COALESCE(?, hidden), "
            "updated_at=?, snapshot_at=?, observation_order=? "
            "WHERE dialog_id=? AND topic_id=? AND observation_order < ?",
            (
                title,
                icon_emoji_id,
                hidden,
                observed_at,
                observed_at,
                observation_order,
                dialog_id,
                topic_id,
                observation_order,
            ),
        )

    def apply_topic_pin(
        self,
        dialog_id: int,
        topic_id: int,
        *,
        pinned: bool,
        observed_at: int,
        observation_order: int | None = None,
    ) -> None:
        require_write_transaction(self._conn)
        if observation_order is None:
            observation_order = allocate_observation_order(self._conn)
        self._conn.execute(
            "UPDATE topic_metadata SET pinned=?, snapshot_at=?, updated_at=?, observation_order=? "
            "WHERE dialog_id=? AND topic_id=? AND observation_order < ?",
            (int(pinned), observed_at, observed_at, observation_order, dialog_id, topic_id, observation_order),
        )

    def apply_topic_pins(
        self,
        dialog_id: int,
        order: tuple[int, ...],
        *,
        observed_at: int,
        observation_order: int | None = None,
    ) -> None:
        """Apply a complete pin membership set without creating unknown topics."""
        require_write_transaction(self._conn)
        if observation_order is None:
            observation_order = allocate_observation_order(self._conn)
        rows = cast(
            list[tuple[int]],
            self._conn.execute("SELECT topic_id FROM topic_metadata WHERE dialog_id=?", (dialog_id,)).fetchall(),
        )
        known = {int(topic_id) for (topic_id,) in rows}
        if not known:
            return
        pinned_ids = known.intersection(order)
        self._conn.execute(
            "UPDATE topic_metadata SET pinned=CASE WHEN topic_id IN ({}) THEN 1 ELSE 0 END, "
            "snapshot_at=?, updated_at=?, observation_order=? WHERE dialog_id=? AND observation_order < ?".format(
                ",".join("?" for _ in pinned_ids) or "NULL"
            ),
            (*sorted(pinned_ids), observed_at, observed_at, observation_order, dialog_id, observation_order),
        )


class SQLiteTopicSnapshotRepository(SQLiteTopicMetadataRepository):
    """Backward-compatible name for the canonical topic repository."""
