"""SQLite schema for native contact-note command receipts."""

CONTACT_NOTE_RECEIPT_DDL = """
CREATE TABLE IF NOT EXISTS contact_note_operation_receipts (
    account_id INTEGER NOT NULL CHECK(account_id > 0),
    dc_id INTEGER NOT NULL,
    key_id TEXT NOT NULL,
    operation_key TEXT NOT NULL CHECK(length(operation_key) BETWEEN 1 AND 128),
    request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint) = 64),
    semantic_fingerprint TEXT NOT NULL CHECK(length(semantic_fingerprint) = 64),
    expected_version TEXT NOT NULL CHECK(length(expected_version) = 64),
    action TEXT NOT NULL CHECK(action IN ('create','update','clear')),
    entity_id INTEGER NOT NULL CHECK(entity_id > 0),
    binding_revision INTEGER NOT NULL DEFAULT 1,
    terminal_revision INTEGER NOT NULL DEFAULT 0,
    terminal_observation_order INTEGER,
    attempt_id TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN ('pending','acknowledged','unchanged','unknown','rejected')),
    verification TEXT NOT NULL DEFAULT 'pending',
    verification_order INTEGER,
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(account_id, operation_key)
) WITHOUT ROWID
"""
