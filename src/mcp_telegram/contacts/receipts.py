"""Durable ownership of exact native contact attempts."""

# pyright: reportAny=false
import json
import sqlite3
from collections.abc import Callable
from typing import cast

from ..auth_scope import TelegramAuthScope
from ..native_write_fence import NativeWriteReceiptBinding
from ..observation_order import allocate_observation_order
from ..sync_transactions import write_transaction
from .contracts import ContactCommandState

_WHERE = """account_id=? AND operation_key=? AND dc_id=? AND key_id=?
AND action=? AND entity_id=? AND semantic_fingerprint=? AND binding_revision=?"""
_FINGERPRINT_LENGTH = 64


def _values(binding: NativeWriteReceiptBinding) -> tuple[object, ...]:
    if binding.domain != "contact":
        raise ValueError("invalid contact binding")
    return (
        binding.account_id,
        binding.operation_key,
        binding.dc_id,
        str(binding.key_id),
        binding.action,
        binding.target,
        binding.semantic_fingerprint,
        binding.binding_revision,
    )


def bind_attempt(conn: sqlite3.Connection, binding: NativeWriteReceiptBinding, attempt_id: str) -> None:
    with write_transaction(conn):
        updated = conn.execute(
            f"UPDATE contact_operation_receipts SET attempt_id=? WHERE {_WHERE} "
            "AND outcome='pending' AND (attempt_id IS NULL OR attempt_id=?)",
            (attempt_id, *_values(binding), attempt_id),
        )
        if not attempt_id or updated.rowcount != 1:
            raise ValueError("contact receipt binding mismatch")


def _terminal(conn: sqlite3.Connection, binding: NativeWriteReceiptBinding, attempt_id: str, outcome: str) -> bool:
    row = conn.execute(
        f"SELECT outcome FROM contact_operation_receipts WHERE {_WHERE} AND attempt_id=?",
        (*_values(binding), attempt_id),
    ).fetchone()
    if row is None or row[0] not in {"pending", "acknowledged", "unknown", "rejected"}:
        return False
    if row[0] != "pending":
        return True
    return (
        conn.execute(
            f"UPDATE contact_operation_receipts SET outcome=?,terminal_revision=terminal_revision+1, "
            f"terminal_observation_order=? WHERE {_WHERE} AND attempt_id=? AND outcome='pending'",
            (outcome, allocate_observation_order(conn), *_values(binding), attempt_id),
        ).rowcount
        == 1
    )


def record_wire_outcome(
    conn: sqlite3.Connection, binding: NativeWriteReceiptBinding, attempt_id: str, outcome: str
) -> None:
    if outcome not in {"acknowledged", "unknown", "rejected"}:
        raise ValueError("invalid contact outcome")
    with write_transaction(conn):
        if not _terminal(conn, binding, attempt_id, outcome):
            raise ValueError("contact receipt outcome mismatch")


def recover_binding(conn: sqlite3.Connection, binding: NativeWriteReceiptBinding, attempt_id: str) -> bool:
    with write_transaction(conn):
        return _terminal(conn, binding, attempt_id, "unknown")


def validate_binding(conn: sqlite3.Connection, binding: NativeWriteReceiptBinding, attempt_id: str) -> bool:
    return (
        conn.execute(
            f"SELECT 1 FROM contact_operation_receipts WHERE {_WHERE} "
            "AND attempt_id=? AND outcome IN ('acknowledged','unknown','rejected')",
            (*_values(binding), attempt_id),
        ).fetchone()
        is not None
    )


def pending_expected_version(conn: sqlite3.Connection, binding: NativeWriteReceiptBinding) -> str | None:
    row = conn.execute(
        f"SELECT expected_version FROM contact_operation_receipts WHERE {_WHERE} AND outcome='pending'",
        _values(binding),
    ).fetchone()
    return None if row is None else str(row[0])


def peer_quarantined(conn: sqlite3.Connection, account_id: int, entity_id: int) -> bool:
    for table in ("contact_operation_receipts", "contact_note_operation_receipts"):
        if (
            conn.execute(
                f"SELECT 1 FROM {table} WHERE account_id=? AND entity_id=? "
                "AND (outcome='pending' OR (outcome='unknown' AND verification!='observed_after_unknown')) LIMIT 1",
                (account_id, entity_id),
            ).fetchone()
            is not None
        ):
            return True
    return False


def reserve_operation(  # noqa: PLR0913 - immutable receipt fields and no-op witness
    conn: sqlite3.Connection,
    binding: NativeWriteReceiptBinding,
    fingerprint: str,
    now: int,
    *,
    expected_version: str,
    verification_fingerprint: str,
    unchanged: bool = False,
    no_op_check: Callable[[sqlite3.Connection], bool] | None = None,
    no_op_order: int | None = None,
) -> tuple[bool, str]:
    if any(len(value) != _FINGERPRINT_LENGTH for value in (fingerprint, expected_version, verification_fingerprint)):
        raise ValueError("invalid contact operation")
    with write_transaction(conn):
        prior = _existing_operation(conn, binding, fingerprint)
        if prior is not None:
            return False, prior
        if peer_quarantined(conn, binding.account_id, binding.target):
            raise ValueError("contact_outcome_unresolved")
        _check_no_op(conn, unchanged, no_op_check, no_op_order)
        values = _values(binding)
        conn.execute(
            "INSERT INTO contact_operation_receipts(account_id,operation_key,dc_id,key_id,action,entity_id,"
            "semantic_fingerprint,binding_revision,request_fingerprint,expected_version,outcome,verification,"
            "verification_order,result_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                *values,
                fingerprint,
                expected_version,
                "unchanged" if unchanged else "pending",
                "verified" if unchanged else "pending",
                no_op_order if unchanged else None,
                json.dumps({"verification_fingerprint": verification_fingerprint}, separators=(",", ":")),
                now,
                now,
            ),
        )
        return True, "unchanged" if unchanged else "pending"


def _existing_operation(conn: sqlite3.Connection, binding: NativeWriteReceiptBinding, fingerprint: str) -> str | None:
    rows = conn.execute(
        "SELECT account_id,request_fingerprint,outcome FROM contact_operation_receipts WHERE operation_key=?",
        (binding.operation_key,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1 or rows[0][0] != binding.account_id or rows[0][1] != fingerprint:
        raise ValueError("operation_conflict")
    return str(rows[0][2])


def _check_no_op(
    conn: sqlite3.Connection, unchanged: bool, check: Callable[[sqlite3.Connection], bool] | None, order: int | None
) -> None:
    if unchanged and (check is None or not check(conn) or not order):
        raise ValueError("contact_state_changed")


def recover_interrupted_operations(conn: sqlite3.Connection, scope: TelegramAuthScope) -> int:
    identity = (scope.account_id, scope.dc_id, str(scope.auth_key_id))
    with write_transaction(conn):
        if (
            conn.execute(
                "SELECT 1 FROM contact_operation_receipts WHERE outcome='pending' "
                "AND account_id=? AND dc_id=? AND key_id=? LIMIT 1",
                identity,
            ).fetchone()
            is None
        ):
            return 0
        return conn.execute(
            "UPDATE contact_operation_receipts SET outcome='unknown',terminal_revision=terminal_revision+1, "
            "terminal_observation_order=? WHERE outcome='pending' AND account_id=? AND dc_id=? AND key_id=?",
            (allocate_observation_order(conn), *identity),
        ).rowcount


def refine_unknown_for_observation(
    conn: sqlite3.Connection, account_id: int, entity_id: int, observation_order: int, guard: Callable[[], bool]
) -> int:
    """Release only object quarantine after a later authoritative contact observation."""
    from ..sync_transactions import require_write_transaction
    from .state import read_contact_command_state

    require_write_transaction(conn)
    state = read_contact_command_state(conn, account_id, entity_id)
    if state["observation_order"] != observation_order or not guard():
        return 0
    return conn.execute(
        "UPDATE contact_operation_receipts SET verification='observed_after_unknown',verification_order=? "
        "WHERE account_id=? AND entity_id=? AND outcome='unknown' AND verification!='observed_after_unknown' "
        "AND terminal_observation_order IS NOT NULL AND terminal_observation_order<?",
        (observation_order, account_id, entity_id, observation_order),
    ).rowcount


def record_verification(
    conn: sqlite3.Connection, binding: NativeWriteReceiptBinding, order: int, result: str, guard: Callable[[], bool]
) -> bool:
    """Publish historical readback only after this attempt's terminal boundary."""
    if result not in {"verified", "mismatch"} or not guard():
        return False
    with write_transaction(conn):
        if not guard():
            return False
        return (
            conn.execute(
                f"UPDATE contact_operation_receipts SET verification=?,verification_order=? WHERE {_WHERE} "
                "AND outcome='acknowledged' AND terminal_observation_order IS NOT NULL "
                "AND terminal_observation_order<? AND verification IN ('pending','unavailable')",
                (result, order, *_values(binding), order),
            ).rowcount
            == 1
        )


def refine_acknowledged_for_observation(
    conn: sqlite3.Connection, account_id: int, entity_id: int, observation_order: int, guard: Callable[[], bool]
) -> int:
    """Record accepted post-terminal contact evidence without changing the wire outcome."""
    from ..sync_transactions import require_write_transaction
    from .state import read_contact_command_state

    require_write_transaction(conn)
    state = read_contact_command_state(conn, account_id, entity_id)
    if state["observation_order"] != observation_order or not guard():
        return 0
    rows = conn.execute(
        "SELECT operation_key,action,result_json FROM contact_operation_receipts "
        "WHERE account_id=? AND entity_id=? AND outcome='acknowledged' "
        "AND verification IN ('pending','unavailable') AND terminal_observation_order<?",
        (account_id, entity_id, observation_order),
    ).fetchall()
    count = 0
    for key, action, result_json in rows:
        matches = _observation_matches(state, action, entity_id, result_json)
        if matches is None:
            continue
        count += conn.execute(
            "UPDATE contact_operation_receipts SET verification=?,verification_order=? "
            "WHERE account_id=? AND operation_key=? AND outcome='acknowledged' "
            "AND verification IN ('pending','unavailable')",
            ("verified" if matches else "mismatch", observation_order, account_id, key),
        ).rowcount
    return count


def _observation_matches(state: object, action: str, entity_id: int, result_json: str) -> bool | None:
    from .composition import contact_fingerprint

    state = cast(ContactCommandState, state)
    if action == "delete":
        return state["membership"] is False
    first, last = state["first_name"], state["last_name"]
    if state["membership"] is not True or first is None or last is None:
        return None
    expected = json.loads(result_json).get("verification_fingerprint")
    if not isinstance(expected, str) or len(expected) != _FINGERPRINT_LENGTH:
        return None
    return contact_fingerprint(action, entity_id, first, last, "") == expected
