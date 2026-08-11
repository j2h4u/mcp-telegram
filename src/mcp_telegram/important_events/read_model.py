"""Read model for compact agent-facing important events."""

from __future__ import annotations

import time
from typing import Protocol, cast, overload

from ..temporal import format_timestamp

_ACCESS_EVENT_SUMMARIES = {
    "access_lost": "Access lost",
    "access_restored": "Access restored",
}


class ImportantEventsCursor(Protocol):
    def fetchall(self) -> object: ...


class ImportantEventsConnection(Protocol):
    @overload
    def execute(self, sql: str, /) -> ImportantEventsCursor: ...

    @overload
    def execute(self, sql: str, _parameters: tuple[object, ...], /) -> ImportantEventsCursor: ...


def list_important_events(
    conn: ImportantEventsConnection,
    *,
    last_hours: int,
    timezone: str,
    now: int | None = None,
) -> list[dict[str, object]]:
    """Return important events observed during the requested recent window."""
    cutoff = (int(time.time()) if now is None else now) - last_hours * 3600
    rows = cast(
        list[tuple[int, str, int | None, str | None]],
        conn.execute(
            """
            SELECT de.occurred_at, de.kind, de.dialog_id, e.name
            FROM daemon_events AS de
            LEFT JOIN entities AS e ON e.id = de.dialog_id
            WHERE de.kind IN ('access_lost', 'access_restored')
              AND de.occurred_at >= ?
            ORDER BY de.occurred_at DESC, de.id DESC
            """,
            (cutoff,),
        ).fetchall(),  # type: ignore[union-attr]
    )

    events: list[dict[str, object]] = []
    for row in rows:
        occurred_at, kind, dialog_id, dialog_title = row
        event_type = str(kind)
        events.append(
            {
                "time": format_timestamp(int(occurred_at), timezone),
                "time_basis": "observed",
                "type": event_type,
                "summary": _ACCESS_EVENT_SUMMARIES[event_type],
                "dialog_id": int(dialog_id) if dialog_id is not None else None,
                "dialog_title": str(dialog_title) if dialog_title is not None else None,
                "message_id": None,
            }
        )
    return events


__all__ = ["ImportantEventsConnection", "list_important_events"]
