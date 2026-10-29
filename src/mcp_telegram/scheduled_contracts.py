"""Shared scalar contracts for scheduled queue reads and native commands."""

import hashlib
import json

SCHEDULED_OPERATION_KEY_LIMIT = 128


def scheduled_message_version(
    account_id: int,
    dialog_id: int,
    message_id: int,
    observation_order: int,
) -> str:
    """Return the stable opaque revision shared by queue reads and mutations."""
    if account_id <= 0 or dialog_id == 0 or message_id <= 0 or observation_order <= 0:
        raise ValueError(
            "scheduled message version requires a positive account, nonzero dialog, and positive queue/order ids"
        )
    payload = json.dumps([account_id, dialog_id, message_id, observation_order], separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()[:24]
