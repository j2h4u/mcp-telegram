"""Authenticated topic acquisition fixtures."""

import sqlite3

from mcp_telegram.auth_scope import TelegramAuthScope
from mcp_telegram.image_authorization import ScopeCell


def topic_scope() -> ScopeCell:
    cell = ScopeCell()
    assert cell.publish(TelegramAuthScope(1, 1, 1, 1))
    return cell


def seed_topic_account(conn: sqlite3.Connection, *dialog_ids: int) -> None:
    conn.execute("INSERT OR IGNORE INTO dialog_directory_state(singleton,account_id) VALUES (1,1)")
    conn.execute("UPDATE dialog_directory_state SET account_id=1 WHERE singleton=1")
    conn.executemany(
        "INSERT OR REPLACE INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES (?,1,1)",
        ((dialog_id,) for dialog_id in dialog_ids),
    )
    conn.commit()
