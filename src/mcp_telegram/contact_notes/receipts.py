"""Durable receipt schema for native private contact-note commands."""

# pyright: reportAny=false
import sqlite3
from collections.abc import Callable

from .schema import CONTACT_NOTE_RECEIPT_DDL as CONTACT_NOTE_RECEIPT_DDL

_FINGERPRINT_LENGTH = 64


_BINDING_WHERE = """account_id=? AND operation_key=? AND dc_id=? AND key_id=?
AND action=? AND entity_id=? AND semantic_fingerprint=? AND binding_revision=?"""


def _binding_values(binding: object) -> tuple[object, ...]:
    from ..native_write_fence import NativeWriteReceiptBinding

    if not isinstance(binding, NativeWriteReceiptBinding) or binding.domain != "contact_note":
        raise ValueError("invalid contact-note receipt binding")
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


def bind_attempt(conn: object, binding: object, attempt_id: str) -> None:
    """Attach one physical attempt to its already durable immutable receipt."""
    import sqlite3

    from ..sync_transactions import write_transaction

    if not isinstance(conn, sqlite3.Connection) or not attempt_id:
        raise ValueError("invalid contact-note attempt")
    with write_transaction(conn):
        result = conn.execute(
            f"UPDATE contact_note_operation_receipts SET attempt_id=? WHERE {_BINDING_WHERE} "
            "AND outcome='pending' AND (attempt_id IS NULL OR attempt_id=?)",
            (attempt_id, *_binding_values(binding), attempt_id),
        )
        if result.rowcount != 1:
            raise ValueError("contact-note receipt binding mismatch")


def record_wire_outcome(conn: object, binding: object, attempt_id: str, outcome: str) -> None:
    """Persist a terminal wire result while retaining any earlier unknown."""
    from ..sync_transactions import write_transaction

    if not isinstance(conn, sqlite3.Connection) or outcome not in {"acknowledged", "unknown", "rejected"}:
        raise ValueError("invalid contact-note wire outcome")
    with write_transaction(conn):
        if not _record_terminal(conn, binding, attempt_id, outcome):
            raise ValueError("contact-note receipt outcome mismatch")


def recover_binding(conn: object, binding: object, attempt_id: str) -> bool:
    """Recover the exact historical physical attempt without issuing network work."""
    from ..sync_transactions import write_transaction

    if not isinstance(conn, sqlite3.Connection):
        raise ValueError("invalid contact-note receipt connection")
    with write_transaction(conn):
        return _record_terminal(conn, binding, attempt_id, "unknown")


def _record_terminal(conn: sqlite3.Connection, binding: object, attempt_id: str, outcome: str) -> bool:
    from ..observation_order import allocate_observation_order

    row = conn.execute(
        f"SELECT outcome FROM contact_note_operation_receipts WHERE {_BINDING_WHERE} AND attempt_id=?",
        (*_binding_values(binding), attempt_id),
    ).fetchone()
    if row is None or row[0] not in {"pending", "acknowledged", "unknown", "rejected"}:
        return False
    if row[0] == "pending":
        order = allocate_observation_order(conn)
        return (
            conn.execute(
                f"UPDATE contact_note_operation_receipts SET outcome=?,terminal_revision=terminal_revision+1, "
                f"terminal_observation_order=? WHERE {_BINDING_WHERE} AND attempt_id=? AND outcome='pending'",
                (outcome, order, *_binding_values(binding), attempt_id),
            ).rowcount
            == 1
        )
    return True


def pending_expected_version(conn: object, binding: object) -> str | None:
    """Read the immutable expected note version for an active bound receipt."""
    import sqlite3

    if not isinstance(conn, sqlite3.Connection):
        raise ValueError("invalid contact-note receipt connection")
    row = conn.execute(
        f"SELECT expected_version FROM contact_note_operation_receipts WHERE {_BINDING_WHERE} AND outcome='pending'",
        _binding_values(binding),
    ).fetchone()
    return None if row is None else str(row[0])


def validate_binding(conn: object, binding: object, attempt_id: str) -> bool:
    """Prove terminal evidence belongs to exactly this physical attempt."""
    import sqlite3

    if not isinstance(conn, sqlite3.Connection):
        raise ValueError("invalid contact-note receipt connection")
    return (
        conn.execute(
            f"SELECT 1 FROM contact_note_operation_receipts WHERE {_BINDING_WHERE} "
            "AND attempt_id=? AND outcome IN ('acknowledged','unknown','rejected')",
            (*_binding_values(binding), attempt_id),
        ).fetchone()
        is not None
    )


def reserve_operation(  # noqa: PLR0913 - explicit immutable receipt fields
    conn: object,
    binding: object,
    request_fingerprint: str,
    now: int,
    *,
    expected_version: str,
    unchanged: bool = False,
    no_op_check: Callable[[sqlite3.Connection], bool] | None = None,
    no_op_order: int | None = None,
) -> tuple[bool, str]:
    """Bind an operation once and quarantine a peer with unresolved ownership."""
    import sqlite3

    from ..sync_transactions import write_transaction

    if not isinstance(conn, sqlite3.Connection):
        raise ValueError("invalid contact-note operation")
    _validate_versions(request_fingerprint, expected_version)
    values = _binding_values(binding)
    account_id, operation_key, dc_id, key_id, action, target, semantic, revision = values
    with write_transaction(conn):
        existing = _existing_operation(conn, operation_key, account_id, request_fingerprint)
        if existing is not None:
            return False, existing
        _ensure_available(conn, account_id, target, unchanged, no_op_check)
        if unchanged and (type(no_op_order) is not int or no_op_order <= 0):
            raise ValueError("note_observation_unavailable")
        conn.execute(
            "INSERT INTO contact_note_operation_receipts(account_id,operation_key,dc_id,key_id,action,entity_id,"
            "semantic_fingerprint,binding_revision,request_fingerprint,expected_version,outcome,verification,verification_order,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                account_id,
                operation_key,
                dc_id,
                key_id,
                action,
                target,
                semantic,
                revision,
                request_fingerprint,
                expected_version,
                "unchanged" if unchanged else "pending",
                "verified" if unchanged else "pending",
                no_op_order if unchanged else None,
                now,
                now,
            ),
        )
        return True, "unchanged" if unchanged else "pending"


def _validate_versions(request_fingerprint: str, expected_version: str) -> None:
    if len(request_fingerprint) != _FINGERPRINT_LENGTH or len(expected_version) != _FINGERPRINT_LENGTH:
        raise ValueError("invalid contact-note operation")


def _ensure_available(
    conn: sqlite3.Connection,
    account_id: object,
    target: object,
    unchanged: bool,
    no_op_check: Callable[[sqlite3.Connection], bool] | None,
) -> None:
    pending = conn.execute(
        "SELECT 1 FROM contact_note_operation_receipts WHERE account_id=? AND entity_id=? "
        "AND (outcome='pending' OR (outcome='unknown' AND verification!='observed_after_unknown')) LIMIT 1",
        (account_id, target),
    ).fetchone()
    if pending is not None:
        raise ValueError("note_outcome_unresolved")
    if unchanged and (no_op_check is None or not no_op_check(conn)):
        raise ValueError("note_state_changed")


def _existing_operation(conn: object, key: object, account_id: object, fingerprint: str) -> str | None:
    import sqlite3

    if not isinstance(conn, sqlite3.Connection):
        raise ValueError("invalid contact-note receipt connection")
    rows = conn.execute(
        "SELECT account_id,request_fingerprint,outcome FROM contact_note_operation_receipts WHERE operation_key=?",
        (key,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1 or rows[0][0] != account_id or rows[0][1] != fingerprint:
        raise ValueError("operation_conflict")
    return str(rows[0][2])


def recover_interrupted_operations(conn: object, scope: object) -> int:
    """Recover interrupted receipts only for the authenticated account and key."""
    import sqlite3

    from ..auth_scope import TelegramAuthScope
    from ..sync_transactions import write_transaction

    if not isinstance(conn, sqlite3.Connection) or not isinstance(scope, TelegramAuthScope):
        raise ValueError("invalid contact-note receipt recovery scope")
    from ..observation_order import allocate_observation_order

    with write_transaction(conn):
        identity = (scope.account_id, scope.dc_id, str(scope.auth_key_id))
        if (
            conn.execute(
                "SELECT 1 FROM contact_note_operation_receipts WHERE outcome='pending' "
                "AND account_id=? AND dc_id=? AND key_id=? LIMIT 1",
                identity,
            ).fetchone()
            is None
        ):
            return 0
        order = allocate_observation_order(conn)
        return conn.execute(
            "UPDATE contact_note_operation_receipts SET outcome='unknown',terminal_revision=terminal_revision+1, "
            "terminal_observation_order=? WHERE outcome='pending' AND account_id=? AND dc_id=? AND key_id=?",
            (order, *identity),
        ).rowcount


def refine_unknown_for_observation(
    conn: sqlite3.Connection, account_id: int, entity_id: int, observation_order: int, guard: Callable[[], bool]
) -> int:
    """Refine unknown receipts from one accepted, freshly ordered profile publication."""
    from ..sync_transactions import require_write_transaction

    require_write_transaction(conn)
    eligible = conn.execute(
        "SELECT 1 FROM contact_note_operation_receipts WHERE account_id=? AND entity_id=? "
        "AND outcome='unknown' AND terminal_observation_order<? LIMIT 1",
        (account_id, entity_id, observation_order),
    ).fetchone()
    if eligible is None or not guard():
        return 0
    return conn.execute(
        "UPDATE contact_note_operation_receipts SET verification='observed_after_unknown',verification_order=? "
        "WHERE account_id=? AND entity_id=? AND outcome='unknown' AND verification!='observed_after_unknown' "
        "AND terminal_observation_order IS NOT NULL AND terminal_observation_order<? AND EXISTS ("
        "SELECT 1 FROM entity_profile_semantic_snapshots AS profile "
        "WHERE profile.owner_account_id=contact_note_operation_receipts.account_id "
        "AND profile.entity_id=contact_note_operation_receipts.entity_id "
        """AND json_extract(profile.provenance_json, '$.fields."full_user.note".order') = ? """
        """AND json_extract(profile.provenance_json, '$.fields."user.contact".order') = ? """
        """AND json_extract(profile.coverage_json, '$."full_user.note".authoritative') = 1 """
        """AND json_extract(profile.coverage_json, '$."user.contact".authoritative') = 1)""",
        (observation_order, account_id, entity_id, observation_order, observation_order, observation_order),
    ).rowcount


def refine_acknowledged_for_observation(
    conn: sqlite3.Connection, account_id: int, entity_id: int, observation_order: int, guard: Callable[[], bool]
) -> int:
    """Record a later accepted profile as historical readback of acknowledged writes."""
    from ..sync_transactions import require_write_transaction
    from .intake import plain_note_fingerprint
    from .state import read_note_command_state

    require_write_transaction(conn)
    profile = read_note_command_state(conn, account_id, entity_id)
    if not _profile_is_fresh(profile, observation_order):
        return 0
    state = profile["state"]
    rows = conn.execute(
        "SELECT operation_key,action,semantic_fingerprint FROM contact_note_operation_receipts "
        "WHERE account_id=? AND entity_id=? AND outcome='acknowledged' "
        "AND verification IN ('pending','unavailable') AND terminal_observation_order<?",
        (account_id, entity_id, observation_order),
    ).fetchall()
    if not rows or not guard():
        return 0
    value = profile["value"]
    if state == "absent":
        fingerprint = plain_note_fingerprint("")
    elif isinstance(value, dict) and isinstance(value.get("text"), str) and value.get("entities") == []:
        fingerprint = plain_note_fingerprint(value["text"])
    else:
        fingerprint = None
    return _publish_acknowledged_refinements(conn, account_id, observation_order, state, fingerprint, rows=rows)


def _profile_is_fresh(profile: dict[str, object], order: int) -> bool:
    return (
        profile["observation_order"] == order
        and profile["contact_observation_order"] == order
        and isinstance(profile["contact"], bool)
        and profile["state"] in {"retained", "absent"}
    )


def _publish_acknowledged_refinements(  # noqa: PLR0913 - explicit recorded evidence
    conn: sqlite3.Connection, account_id: int, order: int, state: object, fingerprint: str | None, *, rows: list
) -> int:
    changed = 0
    for key, action, expected in rows:
        matches = fingerprint == expected and ((action == "clear") == (state == "absent"))
        changed += conn.execute(
            "UPDATE contact_note_operation_receipts SET verification=?,verification_order=? "
            "WHERE account_id=? AND operation_key=? AND outcome='acknowledged' "
            "AND verification IN ('pending','unavailable')",
            ("verified" if matches else "mismatch", order, account_id, key),
        ).rowcount
    return changed
