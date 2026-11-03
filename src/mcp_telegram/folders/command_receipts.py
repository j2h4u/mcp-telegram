"""Durable ownership of native folder command attempts."""

from typing import cast

from .command_schema import FOLDER_COMMAND_SCHEMA as FOLDER_COMMAND_SCHEMA


def _binding_values(binding: object) -> tuple[object, ...]:
    from ..native_write_fence import NativeWriteReceiptBinding

    if not isinstance(binding, NativeWriteReceiptBinding) or binding.domain != "folder":
        raise ValueError("invalid folder receipt binding")
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


_BINDING_WHERE = """account_id=? AND operation_key=? AND dc_id=? AND key_id=?
AND action=? AND target=? AND semantic_fingerprint=? AND binding_revision=?"""


def bind_attempt(conn: object, binding: object, attempt_id: str) -> None:
    """Bind exactly one physical attempt to an already durable operation."""
    import sqlite3

    from ..sync_transactions import write_transaction

    if not isinstance(conn, sqlite3.Connection) or not attempt_id:
        raise ValueError("invalid folder attempt")
    with write_transaction(conn):
        result = conn.execute(
            f"UPDATE folder_command_receipts SET attempt_id=? WHERE {_BINDING_WHERE} "
            "AND outcome='pending' AND (attempt_id IS NULL OR attempt_id=?)",
            (attempt_id, *_binding_values(binding), attempt_id),
        )
        if result.rowcount != 1:
            raise ValueError("folder receipt binding mismatch")


def record_wire_outcome(conn: object, binding: object, attempt_id: str, outcome: str) -> None:
    """Record wire evidence without converting a historical unknown into ACK."""
    import sqlite3

    from ..observation_order import allocate_observation_order
    from ..sync_transactions import write_transaction

    if not isinstance(conn, sqlite3.Connection) or outcome not in {"acknowledged", "rejected", "unknown"}:
        raise ValueError("invalid folder outcome")
    with write_transaction(conn):
        row = cast(
            tuple[object, ...] | None,
            conn.execute(
                f"SELECT outcome FROM folder_command_receipts WHERE {_BINDING_WHERE} AND attempt_id=?",
                (*_binding_values(binding), attempt_id),
            ).fetchone(),
        )
        if row is None:
            raise ValueError("folder receipt outcome mismatch")
        order = allocate_observation_order(conn) if row[0] == "pending" else None
        result = conn.execute(
            f"UPDATE folder_command_receipts SET outcome=CASE WHEN outcome='pending' THEN ? ELSE outcome END, "
            f"terminal_revision=terminal_revision + CASE WHEN outcome='pending' THEN 1 ELSE 0 END, "
            f"dispatch_order=COALESCE(dispatch_order, ?), "
            f"verification_deadline=COALESCE(verification_deadline,unixepoch()+900), "
            f"next_verification_at=CASE WHEN outcome='pending' THEN unixepoch()+5 ELSE next_verification_at END "
            f"WHERE {_BINDING_WHERE} AND attempt_id=? AND outcome IN ('pending', 'acknowledged', 'rejected', 'unknown')",
            (outcome, order, *_binding_values(binding), attempt_id),
        )
        if result.rowcount != 1:
            raise ValueError("folder receipt outcome mismatch")


def validate_binding(conn: object, binding: object, attempt_id: str) -> bool:
    """Validate terminal evidence for the exact immutable gateway binding."""
    import sqlite3

    if not isinstance(conn, sqlite3.Connection):
        raise ValueError("invalid receipt connection")
    row = cast(
        tuple[object, ...] | None,
        conn.execute(
            f"SELECT 1 FROM folder_command_receipts WHERE {_BINDING_WHERE} "
            "AND attempt_id=? AND outcome IN ('acknowledged', 'rejected', 'unknown')",
            (*_binding_values(binding), attempt_id),
        ).fetchone(),
    )
    return row is not None


def read_intent(conn: object, binding: object) -> tuple[str | None, str | None] | None:
    """Read only the exact immutable receipt used to reconstruct wire intent."""
    import sqlite3

    if not isinstance(conn, sqlite3.Connection):
        raise ValueError("invalid receipt connection")
    row = cast(
        tuple[object, ...] | None,
        conn.execute(
            f"SELECT before_image,intended_definition FROM folder_command_receipts WHERE {_BINDING_WHERE}",
            _binding_values(binding),
        ).fetchone(),
    )
    if row is None:
        return None
    return cast(str | None, row[0]), cast(str | None, row[1])


def find_operation(conn: object, account_id: int, operation_key: str) -> tuple[object, ...] | None:
    """Look up existing ownership before evaluating current catalog freshness."""
    import sqlite3

    if not isinstance(conn, sqlite3.Connection):
        raise ValueError("invalid receipt connection")
    return cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT operation_fingerprint,target,outcome,verification,attempt_id,preclaim_rejected,before_image FROM folder_command_receipts "
            "WHERE account_id=? AND operation_key=?",
            (account_id, operation_key),
        ).fetchone(),
    )


def insert_operation(  # noqa: PLR0913 - durable receipt fields are explicit
    conn: object,
    binding: object,
    *,
    operation_fingerprint: str,
    expected_version: str,
    before: str | None,
    intended: str | None,
    now: int,
) -> None:
    """Reserve one target and its fixed create ID before any physical attempt."""
    import sqlite3

    from ..sync_transactions import write_transaction

    if not isinstance(conn, sqlite3.Connection):
        raise ValueError("invalid receipt connection")
    values = _binding_values(binding)
    with write_transaction(conn):
        blocked = cast(
            tuple[object, ...] | None,
            conn.execute(
                "SELECT 1 FROM folder_command_receipts WHERE account_id=? AND "
                "(target=? OR (?='create' AND action='create')) AND outcome IN ('pending','unknown','acknowledged','rejected') "
                "AND COALESCE(verification,'pending') IN ('pending','unavailable') LIMIT 1",
                (values[0], values[5], values[4]),
            ).fetchone(),
        )
        if blocked is not None:
            raise ValueError("folder target has an unresolved operation")
        conn.execute(
            "INSERT INTO folder_command_receipts(account_id,operation_key,dc_id,key_id,action,target,semantic_fingerprint,binding_revision,operation_fingerprint,expected_version,outcome,verification,before_image,intended_definition,created_at,updated_at,verification_deadline) VALUES (?,?,?,?,?,?,?,?,?,?,'pending','pending',?,?,?,?,?)",
            (
                *values,
                operation_fingerprint,
                expected_version,
                before,
                intended,
                now,
                now,
                now + 900,
            ),
        )


def recover_interrupted_operations(conn: object, scope: object, now: int) -> int:
    """Classify only this verified auth scope's interrupted commands as unknown."""
    import sqlite3

    from ..auth_scope import TelegramAuthScope
    from ..observation_order import allocate_observation_order
    from ..sync_transactions import write_transaction

    if not isinstance(conn, sqlite3.Connection) or not isinstance(scope, TelegramAuthScope):
        raise ValueError("invalid recovery scope")
    with write_transaction(conn):
        pending = cast(
            tuple[object, ...] | None,
            conn.execute(
                "SELECT 1 FROM folder_command_receipts WHERE account_id=? AND dc_id=? AND key_id=? AND outcome='pending' LIMIT 1",
                (scope.account_id, scope.dc_id, str(scope.auth_key_id)),
            ).fetchone(),
        )
        if pending is None:
            return 0
        order = allocate_observation_order(conn)
        result = conn.execute(
            "UPDATE folder_command_receipts SET outcome='unknown',terminal_revision=terminal_revision+1,verification='pending',dispatch_order=?,verification_deadline=COALESCE(verification_deadline,?),next_verification_at=? WHERE account_id=? AND dc_id=? AND key_id=? AND outcome='pending'",
            (order, now + 900, now, scope.account_id, scope.dc_id, str(scope.auth_key_id)),
        )
        return result.rowcount


def recover_binding(conn: object, binding: object, attempt_id: str) -> bool:
    """Resolve one linked interrupted receipt without granting retry or ACK."""
    import sqlite3

    from ..observation_order import allocate_observation_order
    from ..sync_transactions import write_transaction

    if not isinstance(conn, sqlite3.Connection) or not attempt_id:
        raise ValueError("invalid folder receipt recovery")
    with write_transaction(conn):
        row = cast(
            tuple[object, ...] | None,
            conn.execute(
                f"SELECT outcome FROM folder_command_receipts WHERE {_BINDING_WHERE} AND attempt_id=?",
                (*_binding_values(binding), attempt_id),
            ).fetchone(),
        )
        if row is None:
            return False
        order = allocate_observation_order(conn) if row[0] == "pending" else None
        result = conn.execute(
            f"UPDATE folder_command_receipts SET outcome=CASE WHEN outcome='pending' THEN 'unknown' ELSE outcome END, terminal_revision=terminal_revision+CASE WHEN outcome='pending' THEN 1 ELSE 0 END, dispatch_order=COALESCE(dispatch_order,?), verification_deadline=COALESCE(verification_deadline,unixepoch()+900), next_verification_at=CASE WHEN outcome='pending' THEN unixepoch() ELSE next_verification_at END WHERE {_BINDING_WHERE} AND attempt_id=? AND outcome IN ('pending','unknown','acknowledged','rejected')",
            (order, *_binding_values(binding), attempt_id),
        )
        return result.rowcount == 1


def refine_from_observation(  # noqa: PLR0913 - accepted observation evidence is explicit
    conn: object,
    account_id: int,
    observation_order: int,
    definitions: object,
    *,
    complete: bool,
    affected_folder_id: int | None = None,
) -> None:
    """Refine retained outcomes inside the accepted observation transaction."""
    import sqlite3
    from collections.abc import Mapping

    if not isinstance(conn, sqlite3.Connection) or not isinstance(definitions, Mapping):
        raise ValueError("invalid folder observation")
    rows = cast(
        list[tuple[str, int, str, str | None]],
        conn.execute(
            "SELECT operation_key,target,action,intended_definition FROM folder_command_receipts "
            "WHERE account_id=? AND dispatch_order<? AND outcome IN ('unknown','acknowledged','rejected') "
            "AND verification IN ('pending','unavailable','exhausted')",
            (account_id, observation_order),
        ).fetchall(),
    )
    for key, target, action, intended in rows:
        if not complete and target != affected_folder_id:
            continue
        actual = definitions.get(target)
        matched = actual is None if action == "delete" else actual == intended and actual is not None
        conn.execute(
            "UPDATE folder_command_receipts SET verification=? WHERE account_id=? AND operation_key=?",
            ("matched" if matched else "mismatched", account_id, key),
        )
