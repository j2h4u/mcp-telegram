"""Bounded structured runtime observations stored in sync.db."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Mapping
from typing import cast

RUNTIME_INSTANCE_ID = uuid.uuid4().hex
MAX_PAYLOAD_BYTES = 1024
DEFAULT_ROW_CAP = 100_000
ALLOWED_KINDS = frozenset(
    {
        "mcp.call",
        "telegram.inbox_read_received",
        "sync.inbox_read_finished",
        "sync.read_reconciliation",
        "runtime.started",
        "runtime.stopped",
        "runtime.connection_observed",
        "runtime.catch_up_requested",
        "runtime.catch_up_request_failed",
        "runtime.task_failed",
    }
)


def tool_telemetry_identity(tool_name: str) -> tuple[str, int]:
    """Return the stable product capability and wire-contract generation."""
    if tool_name in {"get_sync_alerts", "list_important_events"}:
        return "conversation_changes", 0
    if tool_name == "list_conversation_changes":
        return "conversation_changes", 1
    return tool_name, 1


def encode_payload(payload: Mapping[str, object] | None) -> str:
    encoded = json.dumps(dict(payload or {}), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("runtime event payload exceeds 1024 bytes")
    return encoded


def record_runtime_observation(  # noqa: PLR0913
    conn: sqlite3.Connection,
    *,
    kind: str,
    dialog_id: int | None = None,
    operation_id: str | None = None,
    outcome: str | None = None,
    reason_code: str | None = None,
    duration_ms: float | None = None,
    tool_name: str | None = None,
    tool_capability: str | None = None,
    contract_version: int | None = None,
    result_count: int | None = None,
    has_cursor: bool | None = None,
    page_depth: int | None = None,
    has_filter: bool | None = None,
    error_type: str | None = None,
    payload: Mapping[str, object] | None = None,
    observed_at_ms: int | None = None,
) -> int:
    """Append one allowlisted observation without committing the caller's transaction."""
    if kind not in ALLOWED_KINDS:
        raise ValueError(f"unsupported runtime event kind: {kind}")
    if kind == "mcp.call" and tool_name is not None:
        default_capability, default_contract = tool_telemetry_identity(tool_name)
        tool_capability = default_capability if tool_capability is None else tool_capability
        contract_version = default_contract if contract_version is None else contract_version
    cursor = conn.execute(
        """INSERT INTO runtime_observations(
               observed_at_ms, kind, runtime_instance_id, operation_id, outcome,
               reason_code, dialog_id, duration_ms, tool_name, tool_capability, contract_version, result_count,
               has_cursor, page_depth, has_filter, error_type, payload_json
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            int(time.time() * 1000) if observed_at_ms is None else observed_at_ms,
            kind,
            RUNTIME_INSTANCE_ID,
            operation_id,
            outcome,
            reason_code,
            dialog_id,
            duration_ms,
            tool_name,
            tool_capability,
            contract_version,
            result_count,
            None if has_cursor is None else int(has_cursor),
            page_depth,
            None if has_filter is None else int(has_filter),
            error_type,
            encode_payload(payload),
        ),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("runtime event insert did not return an id")
    return cursor.lastrowid


def prune_runtime_observations(
    conn: sqlite3.Connection, *, ttl_seconds: int, row_cap: int = DEFAULT_ROW_CAP, now_ms: int | None = None
) -> int:
    """Prune by age and emergency cap; record the resulting coverage boundary."""
    if ttl_seconds < 1 or row_cap < 1:
        raise ValueError("runtime event retention limits must be positive")
    effective_now = int(time.time() * 1000) if now_ms is None else now_ms
    deleted = conn.execute(
        "DELETE FROM runtime_observations WHERE observed_at_ms < ?", (effective_now - ttl_seconds * 1000,)
    ).rowcount
    count_row = cast(tuple[int] | None, conn.execute("SELECT COUNT(*) FROM runtime_observations").fetchone())
    event_count = int(count_row[0] or 0) if count_row is not None else 0
    excess = max(event_count - row_cap, 0)
    if excess:
        conn.execute(
            "DELETE FROM runtime_observations WHERE id IN (SELECT id FROM runtime_observations ORDER BY id LIMIT ?)",
            (excess,),
        )
        deleted += excess
        conn.execute(
            "INSERT OR REPLACE INTO daemon_state(key, value) VALUES ('runtime_observations_last_cap_truncation_ms', ?)",
            (str(effective_now),),
        )
    return deleted


__all__ = [
    "ALLOWED_KINDS",
    "DEFAULT_ROW_CAP",
    "MAX_PAYLOAD_BYTES",
    "RUNTIME_INSTANCE_ID",
    "encode_payload",
    "prune_runtime_observations",
    "record_runtime_observation",
    "tool_telemetry_identity",
]
