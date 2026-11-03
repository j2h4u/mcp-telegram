"""Schema for durable native folder command receipts."""

FOLDER_COMMAND_SCHEMA = """
CREATE TABLE IF NOT EXISTS folder_command_receipts (
    account_id INTEGER NOT NULL,
    operation_key TEXT NOT NULL,
    dc_id INTEGER NOT NULL,
    key_id TEXT NOT NULL,
    action TEXT NOT NULL,
    operation_fingerprint TEXT NOT NULL DEFAULT '',
    expected_version TEXT NOT NULL DEFAULT '',
    permit_issued INTEGER NOT NULL DEFAULT 0,
    preclaim_rejected INTEGER NOT NULL DEFAULT 0,
    target INTEGER NOT NULL,
    semantic_fingerprint TEXT NOT NULL,
    binding_revision INTEGER NOT NULL,
    terminal_revision INTEGER NOT NULL DEFAULT 0,
    attempt_id TEXT,
    outcome TEXT NOT NULL,
    verification TEXT,
    verification_attempts INTEGER NOT NULL DEFAULT 0,
    verification_deadline INTEGER,
    next_verification_at INTEGER,
    dispatch_order INTEGER,
    before_image TEXT,
    intended_definition TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (account_id, operation_key)
)
"""
