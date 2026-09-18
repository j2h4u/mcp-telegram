"""Shared construction helpers for reaction application-service tests."""

from __future__ import annotations

import sqlite3


def make_reaction_freshener(conn: sqlite3.Connection, client: object) -> object:
    """Compatibility fixture for constructors whose read path is SQLite-only."""
    del conn, client
    return object()
