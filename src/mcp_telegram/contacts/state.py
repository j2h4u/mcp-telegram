"""Read account-owned contact command facts from canonical profile evidence."""

import hashlib
import json
import sqlite3
from typing import cast

from .contracts import ContactCommandState

_CONTACT_PATHS = ("user.contact", "owner.saved_contact_name", "owner.saved_contact_phone_access")


def read_contact_command_state(conn: sqlite3.Connection, account_id: int, entity_id: int) -> ContactCommandState:
    row = cast(
        tuple[str, str, str] | None,
        conn.execute(
            "SELECT fields_json,coverage_json,provenance_json FROM entity_profile_semantic_snapshots "
            "WHERE owner_account_id=? AND entity_id=?",
            (account_id, entity_id),
        ).fetchone(),
    )
    return project_contact_state(account_id, entity_id, row)


def project_contact_state(account_id: int, entity_id: int, row: tuple[str, str, str] | None) -> ContactCommandState:
    result: ContactCommandState = {
        "membership": None,
        "first_name": None,
        "last_name": None,
        "phone_state": "unknown",
        "phone": None,
        "expected_version": None,
        "observation_order": None,
    }
    if row is None:
        return result
    fields = cast(dict[str, object], json.loads(row[0]))
    coverage = cast(dict[str, object], json.loads(row[1]))
    provenance = cast(dict[str, object], json.loads(row[2])).get("fields", {})
    _project_values(result, fields, coverage)
    if result["membership"] is False:
        result["first_name"] = result["last_name"] = None
    result["observation_order"] = _coherent_order(fields, coverage, provenance)
    if result["membership"] is not None:
        semantic = [
            account_id,
            entity_id,
            result["membership"],
            result["first_name"],
            result["last_name"],
            result["phone_state"],
            result["phone"],
        ]
        encoded = json.dumps(semantic, ensure_ascii=True, separators=(",", ":"))
        result["expected_version"] = hashlib.sha256(encoded.encode()).hexdigest()
    return result


def _observed(fields: dict[str, object], coverage: dict[str, object], path: str) -> bool:
    evidence = coverage.get(path)
    return isinstance(evidence, dict) and evidence.get("authoritative") is True and path in fields


def _project_values(result: ContactCommandState, fields: dict[str, object], coverage: dict[str, object]) -> None:
    member = fields.get("user.contact")
    if _observed(fields, coverage, "user.contact") and isinstance(member, bool):
        result["membership"] = member
    name = fields.get("owner.saved_contact_name")
    if _observed(fields, coverage, "owner.saved_contact_name") and isinstance(name, dict):
        first, last = name.get("first_name"), name.get("last_name")
        result["first_name"] = first if isinstance(first, str) else None
        result["last_name"] = last if isinstance(last, str) else None
    _project_phone(result, fields, coverage)


def _project_phone(result: ContactCommandState, fields: dict[str, object], coverage: dict[str, object]) -> None:
    if _observed(fields, coverage, "owner.saved_contact_phone_access"):
        access = fields.get("owner.saved_contact_phone_access")
        phone = fields.get("user.phone")
        if access is True and _observed(fields, coverage, "user.phone") and isinstance(phone, str) and phone:
            result["phone_state"], result["phone"] = "available", phone
        elif access is False:
            result["phone_state"] = "unavailable"


def _coherent_order(fields: dict[str, object], coverage: dict[str, object], provenance: object) -> int | None:
    if not isinstance(provenance, dict):
        return None
    paths = _CONTACT_PATHS + (("user.phone",) if fields.get("owner.saved_contact_phone_access") is True else ())
    orders = [provenance.get(path, {}).get("order") for path in paths if _observed(fields, coverage, path)]
    if len(orders) == len(paths) and type(orders[0]) is int and len(set(orders)) == 1:
        return orders[0]
    return None
