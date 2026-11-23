"""Test-only setup for the canonical dialog-directory coverage boundary."""

from __future__ import annotations

import sqlite3

from mcp_telegram.sync_db import (
    _DIALOG_DIRECTORY_PUBLICATION_DDL,
    _DIALOG_DIRECTORY_STATE_DDL,
    _FOLDER_PEER_REFERENCES_DDL,
)


def install_dialog_directory_coverage_schema(conn: sqlite3.Connection) -> None:
    """Install the current coverage tables and their migration defaults."""
    conn.execute(_DIALOG_DIRECTORY_STATE_DDL)
    conn.execute(_DIALOG_DIRECTORY_PUBLICATION_DDL)
    conn.execute(_FOLDER_PEER_REFERENCES_DDL)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dialog_listing_observations (account_id INTEGER NOT NULL, dialog_id INTEGER NOT NULL, state TEXT NOT NULL, source TEXT NOT NULL, observed_at INTEGER NOT NULL, generation INTEGER NOT NULL, PRIMARY KEY(account_id,dialog_id)) WITHOUT ROWID"
    )
    conn.execute(
        "INSERT OR IGNORE INTO dialog_directory_publication("
        "singleton,account_id,generation,observation_started_at,observation_completed_at) "
        "VALUES (1,NULL,NULL,NULL,NULL)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO dialog_directory_state("
        "singleton,account_id,generation,status,ordinary_status,pinned_main_status,"
        "pinned_archive_status,offset_date,offset_id,offset_peer,observation_started_at,"
        "observation_completed_at,observed_count,retry_at,reason) "
        "VALUES (1,NULL,1,'pending','pending','pending','pending',NULL,0,NULL,NULL,NULL,0,NULL,NULL)"
    )
    conn.commit()
