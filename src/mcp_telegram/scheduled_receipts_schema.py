"""SQLite schema for durable native scheduled-command receipts."""

from .scheduled_contracts import SCHEDULED_OPERATION_KEY_LIMIT

SCHEDULED_OPERATION_RECEIPT_DDL = f"""
CREATE TABLE IF NOT EXISTS scheduled_operation_receipts (
    account_id INTEGER NOT NULL CHECK(account_id > 0),
    operation_key TEXT NOT NULL CHECK(length(operation_key) BETWEEN 1 AND {SCHEDULED_OPERATION_KEY_LIMIT}),
    request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint) = 64),
    action TEXT NOT NULL CHECK(action IN ('create','update','cancel')),
    dialog_id INTEGER NOT NULL CHECK(dialog_id != 0),
    scheduled_message_id INTEGER CHECK(scheduled_message_id > 0),
    random_id INTEGER,
    expected_version TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN ('pending','acknowledged','unchanged','unknown','rejected')),
    mutation_dispatched INTEGER CHECK(mutation_dispatched IN (0,1) OR mutation_dispatched IS NULL),
    result_json TEXT NOT NULL DEFAULT '{{}}',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(account_id, operation_key),
    CHECK((action = 'create' AND random_id IS NOT NULL) OR (action != 'create' AND random_id IS NULL))
) WITHOUT ROWID
"""

__all__ = ["SCHEDULED_OPERATION_RECEIPT_DDL"]
