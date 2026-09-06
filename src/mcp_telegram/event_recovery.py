"""One-shot, verified recovery of event data from the sealed schema-50 backup."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import cast

from .alert_policy import incoming_human_dm_sql
from .runtime_observations import prune_runtime_observations
from .sync_db import _CONVERSATION_HISTORY_TRIGGERS_V54

EXPECTED_TELEMETRY = 1_271
EXPECTED_EDITS = 102
EXPECTED_DELETES = 38
EXPECTED_LOSSES = 9
SOURCE_SCHEMA_VERSION = 50
TARGET_SCHEMA_VERSION = 54


def source_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    for suffix in ("", "-wal", "-shm"):
        part = Path(f"{path}{suffix}")
        digest.update(part.name.encode())
        digest.update(b"\0")
        with part.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def _rows(conn: sqlite3.Connection, sql: str) -> list[tuple[object, ...]]:
    return cast(list[tuple[object, ...]], conn.execute(sql).fetchall())


def _verify_history(source: sqlite3.Connection, target: sqlite3.Connection) -> None:
    policy = incoming_human_dm_sql("m")
    source_edits = _rows(
        source,
        f"""SELECT a.dialog_id,a.message_id,a.version,a.occurred_at,mv.old_text
      FROM sync_alert_events a JOIN messages m USING(dialog_id,message_id)
      JOIN message_versions mv USING(dialog_id,message_id,version)
     WHERE a.kind='edit' AND {policy}
       AND NOT EXISTS (SELECT 1 FROM message_transcriptions mt WHERE mt.dialog_id=a.dialog_id
       AND mt.message_id=a.message_id AND mt.received_at=a.occurred_at) ORDER BY 1,2,3""",
    )
    source_deletes = _rows(
        source,
        f"""SELECT a.dialog_id,a.message_id,a.occurred_at
      FROM sync_alert_events a JOIN messages m USING(dialog_id,message_id)
     WHERE a.kind='deleted_message' AND {policy} ORDER BY 1,2""",
    )
    source_losses = _rows(
        source, "SELECT dialog_id,occurred_at FROM daemon_events WHERE kind='access_lost' ORDER BY 1,2"
    )
    if (len(source_edits), len(source_deletes), len(source_losses)) != (
        EXPECTED_EDITS,
        EXPECTED_DELETES,
        EXPECTED_LOSSES,
    ):
        raise RuntimeError("sealed backup history inventory changed")
    target_edits = _rows(
        target,
        """SELECT h.dialog_id,h.message_id,h.version,h.occurred_at,mv.old_text
      FROM conversation_history_events h JOIN message_versions mv USING(dialog_id,message_id,version)
     WHERE h.kind='edit' ORDER BY 1,2,3""",
    )
    target_deletes = _rows(
        target,
        "SELECT dialog_id,message_id,occurred_at FROM conversation_history_events WHERE kind='deleted_message' ORDER BY 1,2",
    )
    target_losses = _rows(
        target, "SELECT dialog_id,occurred_at FROM conversation_history_events WHERE kind='access_lost' ORDER BY 1,2"
    )
    if not set(source_edits) <= set(target_edits) or not set(source_deletes) <= set(target_deletes):
        raise RuntimeError("production message history differs from sealed backup")
    if not set(source_losses) <= set(target_losses):
        raise RuntimeError("production lifecycle history differs from sealed backup")


def _inventory(source: sqlite3.Connection) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    telemetry = _rows(
        source,
        "SELECT id,tool_name,timestamp,duration_ms,result_count,has_cursor,page_depth,has_filter,error_type,outcome,error_code FROM telemetry_events ORDER BY id",
    )
    losses = _rows(source, "SELECT id,dialog_id,occurred_at,payload_json FROM daemon_events WHERE kind='access_lost'")
    if len(telemetry) != EXPECTED_TELEMETRY or len(losses) != EXPECTED_LOSSES:
        raise RuntimeError("sealed backup recovery inventory changed")
    return telemetry, losses


def _enrich_losses(target: sqlite3.Connection, losses: list[tuple[object, ...]]) -> None:
    for event_id, dialog_id, occurred_at, payload_json in losses:
        payload = cast(dict[str, object], json.loads(cast(str, payload_json)))
        row = cast(
            tuple[object, ...] | None,
            target.execute(
                "SELECT seq,reason_code,previous_status,source_namespace,source_event_id FROM conversation_history_events WHERE kind='access_lost' AND dialog_id=? AND occurred_at=?",
                (dialog_id, occurred_at),
            ).fetchone(),
        )
        if row is None:
            raise RuntimeError("verified lifecycle row disappeared during recovery")
        expected = (payload.get("reason"), payload.get("previous_status"), "backup-2026-09-06", event_id)
        current = tuple(row[1:])
        if any(value is not None for value in current) and current != expected:
            raise RuntimeError("lifecycle enrichment conflict")
        target.execute(
            "UPDATE conversation_history_events SET reason_code=?,previous_status=?,source_namespace=?,source_event_id=? WHERE seq=?",
            (*expected, row[0]),
        )


def _import_telemetry(target: sqlite3.Connection, telemetry: list[tuple[object, ...]]) -> None:
    for row in telemetry:
        (
            event_id,
            tool_name,
            timestamp,
            duration_ms,
            result_count,
            has_cursor,
            page_depth,
            has_filter,
            error_type,
            outcome,
            error_code,
        ) = row
        target.execute(
            """INSERT INTO runtime_observations(observed_at_ms,kind,runtime_instance_id,
          operation_id,outcome,reason_code,duration_ms,tool_name,result_count,has_cursor,page_depth,
          has_filter,error_type,source_namespace,source_event_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                int(cast(float, timestamp) * 1000),
                "mcp.call",
                "legacy-v50",
                f"legacy:{event_id}",
                outcome,
                error_code,
                duration_ms,
                tool_name,
                result_count,
                has_cursor,
                page_depth,
                has_filter,
                error_type,
                "backup-2026-09-06",
                event_id,
            ),
        )


def _write_coverage(target: sqlite3.Connection) -> None:
    bounds = cast(
        tuple[int | None, int | None],
        target.execute(
            "SELECT MIN(observed_at_ms),MAX(observed_at_ms) FROM runtime_observations "
            "WHERE source_namespace='backup-2026-09-06'"
        ).fetchone(),
    )
    if bounds[0] is None:
        target.execute(
            "DELETE FROM daemon_state WHERE key IN "
            "('runtime_observations_legacy_started_at_ms','runtime_observations_legacy_ended_at_ms')"
        )
        return
    target.execute(
        "INSERT OR REPLACE INTO daemon_state(key,value) VALUES ('runtime_observations_legacy_started_at_ms',?)",
        (str(bounds[0]),),
    )
    target.execute(
        "INSERT OR REPLACE INTO daemon_state(key,value) VALUES ('runtime_observations_legacy_ended_at_ms',?)",
        (str(bounds[1]),),
    )


def _verify_recovery_state(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    losses: list[tuple[object, ...]],
    *,
    cutoff_ms: int,
) -> None:
    for event_id, dialog_id, occurred_at, payload_json in losses:
        payload = cast(dict[str, object], json.loads(cast(str, payload_json)))
        row = cast(
            tuple[object, ...] | None,
            target.execute(
                "SELECT reason_code,previous_status,source_namespace,source_event_id "
                "FROM conversation_history_events WHERE kind='access_lost' AND dialog_id=? AND occurred_at=?",
                (dialog_id, occurred_at),
            ).fetchone(),
        )
        expected = (payload.get("reason"), payload.get("previous_status"), "backup-2026-09-06", event_id)
        if row is None or tuple(row) != expected:
            raise RuntimeError("recovered lifecycle metadata differs from sealed backup")

    expected_rows = cast(
        list[tuple[int]],
        source.execute(
            "SELECT id FROM telemetry_events WHERE CAST(timestamp * 1000 AS INTEGER) >= ?", (cutoff_ms,)
        ).fetchall(),
    )
    actual_rows = cast(
        list[tuple[int]],
        target.execute(
            "SELECT source_event_id FROM runtime_observations WHERE source_namespace='backup-2026-09-06'"
        ).fetchall(),
    )
    expected_ids = {row[0] for row in expected_rows}
    actual_ids = {row[0] for row in actual_rows}
    if actual_ids != expected_ids:
        raise RuntimeError("recovered telemetry differs from sealed backup retention window")


def _recover(
    source: sqlite3.Connection, target: sqlite3.Connection, fingerprint: str, retention_seconds: int
) -> dict[str, object]:
    if source.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] != SOURCE_SCHEMA_VERSION:
        raise RuntimeError("backup must be schema 50")
    if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeError("backup is corrupt")
    if target.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] != TARGET_SCHEMA_VERSION:
        raise RuntimeError("target must be schema 54")
    _verify_history(source, target)
    telemetry, losses = _inventory(source)
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - retention_seconds * 1000
    existing = cast(
        tuple[int, int] | None,
        target.execute(
            "SELECT legacy_observation_count,enriched_history_count FROM event_recovery_ledger WHERE source_fingerprint=?",
            (fingerprint,),
        ).fetchone(),
    )
    if existing is not None:
        target.execute("BEGIN IMMEDIATE")
        prune_runtime_observations(target, **{"ttl_seconds": retention_seconds}, now_ms=now_ms)  # noqa: PIE804
        _write_coverage(target)
        _verify_recovery_state(source, target, losses, cutoff_ms=cutoff_ms)
        target.commit()
        return {"status": "no_op", "telemetry": existing[0], "lifecycle": existing[1]}
    target.execute("BEGIN IMMEDIATE")
    target.execute("DROP TRIGGER conversation_history_no_update")
    target.execute("DROP TRIGGER conversation_history_no_delete")
    _enrich_losses(target, losses)
    _import_telemetry(target, telemetry)
    prune_runtime_observations(
        target,
        **{"ttl_seconds": retention_seconds},  # noqa: PIE804
        now_ms=now_ms,
    )
    _write_coverage(target)
    for statement in _CONVERSATION_HISTORY_TRIGGERS_V54[-2:]:
        target.execute(statement)
    target.execute(
        "INSERT INTO event_recovery_ledger VALUES (?,strftime('%s','now'),?,?)",
        (fingerprint, len(telemetry), len(losses)),
    )
    _verify_recovery_state(source, target, losses, cutoff_ms=cutoff_ms)
    target.commit()
    return {"status": "imported", "telemetry": len(telemetry), "lifecycle": len(losses)}


def recover_events(
    source_path: Path, target_path: Path, *, expected_fingerprint: str, retention_seconds: int
) -> dict[str, object]:
    fingerprint = source_fingerprint(source_path)
    if fingerprint != expected_fingerprint:
        raise RuntimeError("backup fingerprint mismatch")
    with tempfile.TemporaryDirectory(prefix="mcp-telegram-recovery-") as temp_dir:
        working_source = Path(temp_dir) / source_path.name
        for suffix in ("", "-wal", "-shm"):
            shutil.copy2(Path(f"{source_path}{suffix}"), Path(f"{working_source}{suffix}"))
        if source_fingerprint(working_source) != fingerprint:
            raise RuntimeError("backup changed while creating the recovery copy")
        source = sqlite3.connect(f"file:{working_source}?mode=ro", uri=True)
        target = sqlite3.connect(target_path)
        try:
            return _recover(source, target, fingerprint, retention_seconds)
        except BaseException:
            target.rollback()
            raise
        finally:
            source.close()
            target.close()


__all__ = ["recover_events", "source_fingerprint"]

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--expected-fingerprint", required=True)
    parser.add_argument("--retention-seconds", type=int, required=True)
    arguments = parser.parse_args()
    print(
        json.dumps(
            recover_events(
                cast(Path, arguments.source),
                cast(Path, arguments.target),
                expected_fingerprint=cast(str, arguments.expected_fingerprint),
                retention_seconds=cast(int, arguments.retention_seconds),
            )
        )
    )
