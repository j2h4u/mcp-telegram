"""Canonical SQL policy for durable human-DM change alerts."""

from __future__ import annotations


def incoming_human_dm_sql(message_alias: str) -> str:
    """Return a positive-confirmation predicate for a persisted message alias."""
    if not message_alias.isidentifier():
        raise ValueError("message alias must be an identifier")
    return f"""{message_alias}.out = 0
AND {message_alias}.is_service = 0
AND {message_alias}.sender_id = {message_alias}.dialog_id
AND EXISTS (
    SELECT 1 FROM dialogs d
    WHERE d.dialog_id = {message_alias}.dialog_id AND d.type = 'user'
      AND EXISTS (
          SELECT 1 FROM entities e
          WHERE e.id = d.dialog_id AND e.type IN ('user', 'User')
      )
)"""


__all__ = ["incoming_human_dm_sql"]
