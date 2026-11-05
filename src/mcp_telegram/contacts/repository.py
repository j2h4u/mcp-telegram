"""Local account-owned contact catalogue projection."""

import sqlite3
from typing import cast

from ..sync_transactions import require_write_transaction
from .contracts import CONTACT_PAGE_SIZE_LIMIT
from .state import project_contact_state


def read_contacts(
    conn: sqlite3.Connection, account_id: int, *, limit: int, after_entity_id: int | None
) -> dict[str, object]:
    _validate_page(limit, after_entity_id)
    rows = cast(
        list[tuple[int, str, str, str, str | None, str | None]],
        conn.execute(
            "SELECT entity_id,fields_json,coverage_json,provenance_json, "
            "json_type(fields_json, '$.\"user.mutual_contact\"'), "
            "json_type(coverage_json, '$.\"user.mutual_contact\".authoritative') "
            "FROM entity_profile_semantic_snapshots WHERE owner_account_id=? AND entity_id>? "
            "AND json_type(fields_json, '$.\"user.contact\"')='true' "
            "AND json_extract(coverage_json, '$.\"user.contact\".authoritative')=1 "
            "ORDER BY entity_id LIMIT ?",
            (account_id, after_entity_id or 0, limit + 1),
        ).fetchall(),
    )
    items: list[dict[str, object]] = []
    for entity_id, fields, coverage, provenance, mutual_type, authority_type in rows[:limit]:
        state = project_contact_state(account_id, entity_id, (fields, coverage, provenance))
        item: dict[str, object] = {
            key: state[key] for key in ("membership", "first_name", "last_name", "expected_version")
        }
        item["entity_id"] = entity_id
        item["mutual_contact"] = _mutual_contact(mutual_type, authority_type)
        items.append(item)
    metadata = cast(
        tuple[int | None] | None,
        conn.execute("SELECT observed_at FROM contact_catalogue_state WHERE account_id=?", (account_id,)).fetchone(),
    )
    observed_at = None if metadata is None else metadata[0]
    return {
        "account_id": account_id,
        "items": items,
        "coverage": {"state": "complete" if observed_at is not None else "unknown", "observed_at": observed_at},
        "next_after_entity_id": rows[limit - 1][0] if len(rows) > limit else None,
    }


def _mutual_contact(value_type: str | None, authority_type: str | None) -> bool | None:
    if authority_type != "true":
        return None
    return True if value_type == "true" else False if value_type == "false" else None


def _validate_page(limit: int, after_entity_id: int | None) -> None:
    if type(limit) is not int or not 1 <= limit <= CONTACT_PAGE_SIZE_LIMIT:
        raise ValueError("contact limit must be between 1 and 100")
    if after_entity_id is not None and (type(after_entity_id) is not int or after_entity_id <= 0):
        raise ValueError("contact cursor must be a positive id")


def request_contact_catalogue(conn: sqlite3.Connection, account_id: int) -> int:
    """Durably request one finite catalogue refresh within the caller's transaction."""
    require_write_transaction(conn)
    conn.execute(
        "INSERT INTO contact_catalogue_state(account_id,requested_revision,accepted_revision) VALUES (?,1,0) "
        "ON CONFLICT(account_id) DO UPDATE SET requested_revision=requested_revision+1",
        (account_id,),
    )
    row = cast(
        tuple[int],
        conn.execute(
            "SELECT requested_revision FROM contact_catalogue_state WHERE account_id=?", (account_id,)
        ).fetchone(),
    )
    return row[0]
