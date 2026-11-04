"""Construct current account-scoped folder facts for local read fixtures."""

import sqlite3

from mcp_telegram.folders.contracts import FolderRule, FolderRuleObservation
from mcp_telegram.folders.sqlite_repository import SQLiteFolderSnapshotRepository
from mcp_telegram.observation_order import allocate_observation_order
from mcp_telegram.sync_transactions import write_savepoint


def seed_folder_snapshot(
    conn: sqlite3.Connection, folders: list[tuple[int, str]], memberships: list[tuple[int, int]]
) -> None:
    with write_savepoint(conn):
        conn.execute(
            "UPDATE dialog_directory_publication SET account_id=42,generation=1,observation_started_at=1 "
            "WHERE singleton=1 AND account_id IS NULL"
        )
        rules = tuple(
            FolderRule(
                folder_id,
                title,
                source_position=index,
                included_ids=tuple(dialog_id for member_folder, dialog_id in memberships if member_folder == folder_id),
            )
            for index, (folder_id, title) in enumerate(folders)
        )
        repository = SQLiteFolderSnapshotRepository(conn)
        observation = FolderRuleObservation(rules, "fixture", 1)
        repository.project_observation(observation, completed_at=1, observation_order=allocate_observation_order(conn))
        repository.invalidate()
