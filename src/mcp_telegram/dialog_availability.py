"""Project independent listing and current access evidence for one account."""

from __future__ import annotations

import json
import sqlite3
from typing import TypedDict, cast

from .entity_profile.repository import bound_profile_account


class ListingEvidence(TypedDict):
    state: str
    source: str | None
    observed_at: int | None
    generation: int | None


class AcquisitionEvidence(TypedDict):
    state: str
    source: str | None
    observed_at: int | None
    error_code: str | None


class AccessEvidence(TypedDict):
    state: str
    scope: str
    reason_code: str | None
    cause: str | None
    observed_at: int | None


DialogAvailability = TypedDict(  # noqa: UP013 - public string keys are consumed dynamically
    "DialogAvailability",
    {"listing": ListingEvidence, "acquisition": AcquisitionEvidence, "access": AccessEvidence},
)


_AvailabilityRow = tuple[
    int,
    str | None,
    str | None,
    int | None,
    int | None,
    str | None,
    int | None,
    str | None,
    str | None,
    str | None,
    int | None,
    str | None,
    str | None,
    int | None,
    str | None,
]


def read_dialog_availabilities(
    conn: sqlite3.Connection, account_id: int, dialog_ids: list[int]
) -> dict[int, DialogAvailability]:
    """Read persisted evidence in one query without acquiring Telegram data."""
    account_matches = bound_profile_account(conn) == account_id
    rows = cast(
        list[_AvailabilityRow],
        conn.execute(
            "SELECT ids.value,l.state,l.source,l.observed_at,l.generation,sd.status,sd.access_lost_at,"
            "event.reason_code,event.access_change_cause,sd.access_loss_scope,sd.access_scope_observed_at,ref.metadata_state,ref.metadata_source,ref.metadata_observed_at,ref.metadata_error_code FROM json_each(?) ids "
            "LEFT JOIN dialog_listing_observations l ON l.account_id=? AND l.dialog_id=ids.value "
            "LEFT JOIN synced_dialogs sd ON ? AND sd.dialog_id=ids.value "
            "LEFT JOIN folder_peer_references ref ON ref.account_id=? AND ref.dialog_id=ids.value "
            "LEFT JOIN conversation_history_events event ON event.seq=sd.access_loss_event_seq AND sd.status='access_lost'",
            (json.dumps(dialog_ids), account_id, account_matches, account_id),
        ).fetchall(),
    )
    return {
        int(row[0]): {
            "listing": {
                "state": row[1] or "not_yet_observed",
                "source": row[2],
                "observed_at": row[3],
                "generation": row[4],
            },
            "acquisition": {
                "state": row[11] or "not_requested",
                "source": row[12],
                "observed_at": row[13],
                "error_code": row[14],
            },
            "access": {
                "state": "access_lost" if row[5] == "access_lost" else "unknown",
                "scope": (row[9] or "unknown") if row[5] == "access_lost" else "unknown",
                "reason_code": row[7],
                "cause": row[8],
                "observed_at": (row[10] if row[9] else row[6]) if row[5] == "access_lost" else None,
            },
        }
        for row in rows
    }


def read_dialog_availability(conn: sqlite3.Connection, account_id: int, dialog_id: int) -> DialogAvailability:
    return read_dialog_availabilities(conn, account_id, [dialog_id])[dialog_id]
