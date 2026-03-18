from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Literal


NavigationKind = Literal["history", "search"]
HistoryDirection = Literal["newest", "oldest"]


@dataclass(frozen=True)
class NavigationToken:
    """Base64-encoded JSON cursor shared by history and search navigation."""

    kind: NavigationKind
    value: int
    dialog_id: int
    topic_id: int | None = None
    query: str | None = None
    direction: HistoryDirection | None = None


def _encode_payload(payload: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def _decode_payload(token: str) -> dict[str, object]:
    try:
        data = json.loads(base64.urlsafe_b64decode(token.encode()))
    except (json.JSONDecodeError, ValueError, binascii.Error) as exc:
        raise ValueError(f"Invalid navigation token: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Invalid navigation token: payload must be an object")
    return data


def encode_navigation_token(navigation: NavigationToken) -> str:
    payload: dict[str, object] = {
        "kind": navigation.kind,
        "value": navigation.value,
        "dialog_id": navigation.dialog_id,
    }
    if navigation.topic_id is not None:
        payload["topic_id"] = navigation.topic_id
    if navigation.query is not None:
        payload["query"] = navigation.query
    if navigation.direction is not None:
        payload["direction"] = navigation.direction
    return _encode_payload(payload)


def decode_navigation_token(token: str) -> NavigationToken:
    data = _decode_payload(token)

    kind = data.get("kind")
    if kind not in {"history", "search"}:
        raise ValueError("Invalid navigation token: kind must be history or search")

    value = data.get("value")
    if not isinstance(value, int):
        raise ValueError("Invalid navigation token: value must be an integer")

    dialog_id = data.get("dialog_id")
    if not isinstance(dialog_id, int):
        raise ValueError("Invalid navigation token: dialog_id must be an integer")

    topic_id = data.get("topic_id")
    if topic_id is not None and not isinstance(topic_id, int):
        raise ValueError("Invalid navigation token: topic_id must be an integer when present")

    query = data.get("query")
    if query is not None and not isinstance(query, str):
        raise ValueError("Invalid navigation token: query must be a string when present")

    direction = data.get("direction")
    if direction is not None and direction not in {"newest", "oldest"}:
        raise ValueError("Invalid navigation token: direction must be newest or oldest when present")

    return NavigationToken(
        kind=kind,
        value=value,
        dialog_id=dialog_id,
        topic_id=topic_id,
        query=query,
        direction=direction,
    )


def encode_history_navigation(
    message_id: int,
    dialog_id: int,
    *,
    topic_id: int | None = None,
    direction: HistoryDirection = "newest",
) -> str:
    """Encode a history continuation cursor as a base64 token."""
    return encode_navigation_token(
        NavigationToken(
            kind="history",
            value=message_id,
            dialog_id=dialog_id,
            topic_id=topic_id,
            direction=direction,
        )
    )


def decode_history_navigation(
    token: str,
    *,
    expected_dialog_id: int,
    expected_topic_id: int | None = None,
    expected_direction: HistoryDirection | None = None,
) -> int:
    """Decode a history cursor, raising ``ValueError`` on dialog/topic/direction mismatch."""
    navigation = decode_navigation_token(token)
    if navigation.kind != "history":
        raise ValueError(f"Navigation token is for {navigation.kind}, not history")
    if navigation.dialog_id != expected_dialog_id:
        msg = f"Navigation token belongs to dialog {navigation.dialog_id}, not {expected_dialog_id}"
        raise ValueError(msg)
    if navigation.topic_id != expected_topic_id:
        msg = f"Navigation token belongs to topic {navigation.topic_id}, not {expected_topic_id}"
        raise ValueError(msg)
    direction = navigation.direction or "newest"
    if expected_direction is not None and direction != expected_direction:
        msg = f"Navigation token belongs to {direction} history, not {expected_direction}"
        raise ValueError(msg)
    return navigation.value


def encode_search_navigation(offset: int, dialog_id: int, query: str) -> str:
    """Encode a search continuation cursor as a base64 token."""
    return encode_navigation_token(
        NavigationToken(
            kind="search",
            value=offset,
            dialog_id=dialog_id,
            query=query,
        )
    )


def decode_search_navigation(token: str, *, expected_dialog_id: int, expected_query: str) -> int:
    """Decode a search cursor, raising ``ValueError`` on dialog/query mismatch."""
    navigation = decode_navigation_token(token)
    if navigation.kind != "search":
        raise ValueError(f"Navigation token is for {navigation.kind}, not search")
    if navigation.dialog_id != expected_dialog_id:
        msg = f"Navigation token belongs to dialog {navigation.dialog_id}, not {expected_dialog_id}"
        raise ValueError(msg)
    if navigation.query != expected_query:
        msg = f'Navigation token belongs to query "{navigation.query}", not "{expected_query}"'
        raise ValueError(msg)
    return navigation.value


