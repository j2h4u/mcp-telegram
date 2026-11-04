"""SQLite adapter for canonical topic facts."""

from __future__ import annotations

import sqlite3
import time
from typing import cast

from ..observation_order import allocate_observation_order
from ..sync_transactions import require_write_transaction, write_savepoint
from .contracts import TopicFact
from .ports import TopicMetadataRepository, TopicSnapshotRepository


class SQLiteTopicMetadataRepository(TopicSnapshotRepository, TopicMetadataRepository):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def for_connection(self, conn: sqlite3.Connection) -> SQLiteTopicSnapshotRepository:
        return SQLiteTopicSnapshotRepository(conn)

    def begin_snapshot(self) -> int:
        with write_savepoint(self._conn):
            return allocate_observation_order(self._conn)

    def _apply(self, dialog_id: int, topic_id: int, fields: dict[str, object], now: int, order: int) -> None:
        require_write_transaction(self._conn)
        self._conn.execute(
            "INSERT OR IGNORE INTO topic_metadata(dialog_id,topic_id,title,is_general,is_deleted,updated_at,"
            "pinned,hidden,snapshot_at,observation_order,catalog_present,membership_order) "
            "VALUES (?,?,NULL,0,0,?,NULL,NULL,?,?,1,?)",
            (dialog_id, topic_id, now, now, order, order),
        )
        row = cast(
            tuple[int, int] | None,
            self._conn.execute(
                "SELECT is_deleted,membership_order FROM topic_metadata WHERE dialog_id=? AND topic_id=?",
                (dialog_id, topic_id),
            ).fetchone(),
        )
        if row is None or (row[0] and row[1] >= order):
            return
        for name, value in fields.items():
            order_column = {
                "title": "title_order",
                "icon_emoji_id": "icon_order",
                "icon_color": "color_order",
                "pinned": "pinned_order",
                "hidden": "hidden_order",
                "date": "date_order",
            }[name]
            bit = {"title": 1, "icon_emoji_id": 2, "icon_color": 4, "pinned": 8, "hidden": 16, "date": 32}[name]
            extra = (
                ",icon_emoji=CASE WHEN icon_emoji_id IS ? THEN icon_emoji ELSE NULL END"
                if name == "icon_emoji_id"
                else ""
            )
            args = (
                (value, value, order, dialog_id, topic_id, order)
                if extra
                else (value, order, dialog_id, topic_id, order)
            )
            self._conn.execute(
                f"UPDATE topic_metadata SET {name}=?{extra},{order_column}=?,known_fields=known_fields | {bit} "
                f"WHERE dialog_id=? AND topic_id=? AND {order_column}<?",
                args,
            )
        self._conn.execute(
            "UPDATE topic_metadata SET is_deleted=0,catalog_present=1,membership_order=?,updated_at=?,snapshot_at=?,"
            "observation_order=MAX(observation_order,?) WHERE dialog_id=? AND topic_id=? AND membership_order<?",
            (order, now, now, order, dialog_id, topic_id, order),
        )

    def upsert_topics(
        self, dialog_id: int, topics: tuple[TopicFact, ...], *, observation_order: int | None = None
    ) -> None:
        order = self.begin_snapshot() if observation_order is None else observation_order
        now = int(time.time())
        with write_savepoint(self._conn):
            for topic in topics:
                fields: dict[str, object] = {
                    "title": topic.title,
                    "icon_emoji_id": topic.icon_emoji_id,
                }
                if topic.hidden is not None:
                    fields["hidden"] = int(topic.hidden)
                if topic.icon_color is not None:
                    fields["icon_color"] = topic.icon_color
                if topic.date is not None:
                    fields["date"] = topic.date
                if topic.pinned is not None:
                    fields["pinned"] = int(topic.pinned)
                self._apply(dialog_id, topic.topic_id, fields, now, order)
                self._conn.execute(
                    "UPDATE topic_metadata SET icon_emoji=COALESCE(?,icon_emoji),is_general=? "
                    "WHERE dialog_id=? AND topic_id=? AND icon_order=? AND is_deleted=0",
                    (topic.icon_emoji, int(topic.is_general), dialog_id, topic.topic_id, order),
                )

    def apply_topic_create(  # noqa: PLR0913
        self,
        dialog_id: int,
        topic_id: int,
        *,
        title: str,
        icon_emoji_id: int | None,
        icon_color: int | None = None,
        date: int | None,
        observed_at: int,
        observation_order: int | None = None,
    ) -> None:
        order = allocate_observation_order(self._conn) if observation_order is None else observation_order
        fields: dict[str, object] = {"title": title, "icon_color": icon_color}
        if icon_emoji_id is not None:
            fields["icon_emoji_id"] = icon_emoji_id or None
        if date is not None:
            fields["date"] = date
        self._apply(dialog_id, topic_id, fields, observed_at, order)

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
        order = allocate_observation_order(self._conn) if observation_order is None else observation_order
        fields: dict[str, object] = {}
        if title is not None:
            fields["title"] = title
        if icon_emoji_id is not None:
            fields["icon_emoji_id"] = icon_emoji_id or None
        if hidden is not None:
            fields["hidden"] = int(hidden)
        if fields:
            self._apply(dialog_id, topic_id, fields, observed_at, order)

    def apply_topic_pin(
        self, dialog_id: int, topic_id: int, *, pinned: bool, observed_at: int, observation_order: int | None = None
    ) -> None:
        order = allocate_observation_order(self._conn) if observation_order is None else observation_order
        self._apply(dialog_id, topic_id, {"pinned": int(pinned)}, observed_at, order)

    def apply_topic_pins(
        self, dialog_id: int, order: tuple[int, ...], *, observed_at: int, observation_order: int | None = None
    ) -> None:
        sequence = allocate_observation_order(self._conn) if observation_order is None else observation_order
        exclude = f" AND topic_id NOT IN ({','.join('?' for _ in order)})" if order else ""
        self._conn.execute(
            "UPDATE topic_metadata SET pinned=0,pinned_order=?,known_fields=known_fields | 8,"
            "updated_at=?,snapshot_at=?,observation_order=MAX(observation_order,?) "
            "WHERE dialog_id=? AND is_deleted=0 AND pinned_order<?" + exclude,
            (sequence, observed_at, observed_at, sequence, dialog_id, sequence, *order),
        )
        for topic_id in order:
            self._apply(dialog_id, topic_id, {"pinned": 1}, observed_at, sequence)


class SQLiteTopicSnapshotRepository(SQLiteTopicMetadataRepository):
    """Canonical topic snapshot repository."""
