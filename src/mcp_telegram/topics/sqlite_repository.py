"""SQLite adapter for the canonical ``topic_metadata`` snapshot."""

from __future__ import annotations

import sqlite3
import time

from .contracts import TopicFact
from .ports import TopicSnapshotRepository

_UPSERT_TOPIC_SQL = """
INSERT INTO topic_metadata
    (dialog_id, topic_id, title, top_message_id,
     is_general, is_deleted, updated_at,
     icon_emoji_id, icon_emoji, icon_color,
     pinned, hidden, snapshot_at, date)
VALUES
    (:dialog_id, :topic_id, :title, NULL,
     :is_general, 0, :updated_at,
     :icon_emoji_id, :icon_emoji, :icon_color,
     0, 0, :snapshot_at, :date)
ON CONFLICT(dialog_id, topic_id) DO UPDATE SET
    title          = COALESCE(excluded.title, topic_metadata.title),
    icon_emoji_id  = COALESCE(excluded.icon_emoji_id, topic_metadata.icon_emoji_id),
    icon_emoji     = COALESCE(excluded.icon_emoji, topic_metadata.icon_emoji),
    icon_color     = COALESCE(excluded.icon_color, topic_metadata.icon_color),
    is_general     = excluded.is_general,
    updated_at     = excluded.updated_at,
    snapshot_at    = excluded.snapshot_at,
    date           = COALESCE(excluded.date, topic_metadata.date)
WHERE topic_metadata.snapshot_at IS NULL
   OR topic_metadata.snapshot_at < excluded.snapshot_at
"""


class SQLiteTopicSnapshotRepository(TopicSnapshotRepository):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert_topics(self, dialog_id: int, topics: tuple[TopicFact, ...]) -> None:
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
            }
            for topic in topics
        )
        with self._conn:
            self._conn.executemany(_UPSERT_TOPIC_SQL, rows)
