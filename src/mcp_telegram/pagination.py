from __future__ import annotations

import base64
import json


def encode_cursor(message_id: int, dialog_id: int) -> str:
    """Encode a message_id and dialog_id into an opaque cursor token."""
    payload = json.dumps({"id": message_id, "dialog_id": dialog_id})
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_cursor(token: str, expected_dialog_id: int) -> int:
    """Decode a cursor token and return the message_id.

    Raises ValueError if the token's dialog_id does not match expected_dialog_id.
    """
    data = json.loads(base64.urlsafe_b64decode(token.encode()))
    if data["dialog_id"] != expected_dialog_id:
        msg = f"Cursor belongs to dialog {data['dialog_id']}, not {expected_dialog_id}"
        raise ValueError(msg)
    return data["id"]
