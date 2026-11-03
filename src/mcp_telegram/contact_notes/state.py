"""Account-scoped versions of canonical private note facts."""

# pyright: reportAny=false
import hashlib
import json
import sqlite3
from typing import cast


def read_note(conn: sqlite3.Connection, account_id: int, entity_id: int) -> dict[str, object]:
    """Read only authoritative complete note evidence; unknown is never absence."""
    row = conn.execute(
        "SELECT fields_json, coverage_json FROM entity_profile_semantic_snapshots "
        "WHERE owner_account_id=? AND entity_id=?",
        (account_id, entity_id),
    ).fetchone()
    if row is None:
        return {"state": "unknown", "expected_version": None}
    return _project_note(account_id, entity_id, json.loads(row[0]), json.loads(row[1]))


def _project_note(account_id: int, entity_id: int, fields: dict, coverage: dict) -> dict[str, object]:
    evidence = coverage.get("full_user.note", {})
    if not isinstance(evidence, dict) or evidence.get("authoritative") is not True:
        return {"state": "unknown", "expected_version": None}
    if "full_user.note" not in fields:
        return {"state": "unknown", "expected_version": None}
    value = fields["full_user.note"]
    state = _known_state(value, evidence.get("state"))
    if state is None:
        return {"state": "unknown", "expected_version": None}
    encoded = json.dumps([account_id, entity_id, value], sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return {"state": state, "expected_version": hashlib.sha256(encoded.encode()).hexdigest()}


def _known_state(value: object, state: object) -> str | None:
    if value is None and state == "cleared":
        return "absent"
    if not isinstance(value, dict) or state != "present":
        return None
    note = cast(dict[str, object], value)
    if note.get("constructor") != "TextWithEntities" or not isinstance(note.get("text"), str):
        return None
    entities = note.get("entities")
    return "retained" if isinstance(entities, list) and all(isinstance(item, dict) for item in entities) else None


def read_note_command_state(conn: sqlite3.Connection, account_id: int, entity_id: int) -> dict[str, object]:
    """Read exact note and authoritative contact eligibility for command validation."""
    row = conn.execute(
        "SELECT fields_json,coverage_json,provenance_json FROM entity_profile_semantic_snapshots "
        "WHERE owner_account_id=? AND entity_id=?",
        (account_id, entity_id),
    ).fetchone()
    if row is None:
        return {
            "state": "unknown",
            "expected_version": None,
            "contact": None,
            "value": None,
            "observation_order": None,
            "contact_observation_order": None,
        }
    fields = json.loads(row[0])
    coverage = json.loads(row[1])
    state = _project_note(account_id, entity_id, fields, coverage)
    contact_evidence = coverage.get("user.contact", {})
    contact = (
        fields.get("user.contact")
        if isinstance(contact_evidence, dict) and contact_evidence.get("authoritative") is True
        else None
    )
    provenance = json.loads(row[2]).get("fields", {})
    note_provenance = provenance.get("full_user.note", {})
    contact_provenance = provenance.get("user.contact", {})
    order = note_provenance.get("order") if isinstance(note_provenance, dict) else None
    contact_order = contact_provenance.get("order") if isinstance(contact_provenance, dict) else None
    return {
        **state,
        "contact": contact if isinstance(contact, bool) else None,
        "value": fields.get("full_user.note"),
        "observation_order": order if type(order) is int else None,
        "contact_observation_order": contact_order if type(contact_order) is int else None,
    }


def note_observation_order(conn: sqlite3.Connection, account_id: int, entity_id: int) -> int | None:
    """Return the canonical note root's accepted source order, if observed."""
    row = conn.execute(
        """SELECT json_extract(provenance_json, '$.fields."full_user.note".order') """
        "FROM entity_profile_semantic_snapshots WHERE owner_account_id=? AND entity_id=?",
        (account_id, entity_id),
    ).fetchone()
    return int(row[0]) if row is not None and type(row[0]) is int else None
