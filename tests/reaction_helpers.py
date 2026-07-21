"""Shared construction helpers for reaction application-service tests."""

from __future__ import annotations

import sqlite3

from mcp_telegram.config import ReactionsConfig
from mcp_telegram.reactions.refresh import ReactionFreshener
from mcp_telegram.reactions.sqlite_repository import SQLiteReactionSnapshotRepository
from mcp_telegram.reactions.telegram_adapter import TelethonTelegramReactionGateway


def make_reaction_freshener(conn: sqlite3.Connection, client: object) -> ReactionFreshener:
    """Build the production reaction service for a test-owned database/client."""
    return ReactionFreshener(
        SQLiteReactionSnapshotRepository(conn),
        TelethonTelegramReactionGateway(client),
        freshness_ttl_seconds=ReactionsConfig().freshness_ttl_seconds,
    )
