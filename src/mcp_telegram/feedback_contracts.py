"""Shared feedback vocabulary used by the application and persistence layers."""

from __future__ import annotations

VALID_SEVERITIES: frozenset[str] = frozenset({"bug", "suggestion", "question"})
VALID_STATUSES: frozenset[str] = frozenset({"open", "in_progress", "done", "dismissed"})

__all__ = ["VALID_SEVERITIES", "VALID_STATUSES"]
