import base64
import binascii
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, cast

# Process-local secret — tokens are HMAC-signed to prevent forgery.
# Not valid across restarts; the LLM must start fresh navigation if the
# daemon restarts (tokens would fail validation with "signature mismatch").
_TOKEN_SECRET: bytes = os.urandom(32)


NavigationKind = Literal["history", "search"]
AccountTraceGroupBy = Literal["timeline", "dialog"]


class HistoryDirection(StrEnum):
    """Internal history page-selection direction; presentation stays chronological."""

    NEWEST = "newest"
    OLDEST = "oldest"


@dataclass(frozen=True)
class NavigationToken:
    """Base64-encoded JSON cursor shared by history and search navigation.

    ``value`` carries a message_id for history navigation or a search offset
    for search navigation — interpret based on ``kind``.
    """

    kind: NavigationKind
    value: int
    dialog_id: int
    topic_id: int | None = None
    query: str | None = None
    direction: HistoryDirection | None = None


@dataclass(frozen=True)
class AccountTraceNavigationToken:
    """Opaque keyset cursor for Account Trace continuation.

    The cursor is bound to the account, grouping mode, optional dialog/topic
    scope, and time bounds so callers cannot accidentally reuse a page token
    for a different trace.
    """

    target_user_id: int
    sent_at: int
    dialog_id: int
    message_id: int
    group_by: AccountTraceGroupBy
    exact_dialog_id: int | None = None
    exact_topic_id: int | None = None
    sent_after: str | None = None
    sent_before: str | None = None


def _encode_payload(payload: dict[str, object]) -> str:
    data = json.dumps(payload, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(data).decode()
    mac = hmac.new(_TOKEN_SECRET, data, hashlib.sha256).hexdigest()[:16]
    return f"{encoded}.{mac}"


def _decode_payload(token: str) -> dict[str, object]:
    if "." not in token:
        raise ValueError("Invalid navigation token: missing signature")
    encoded, _, mac = token.rpartition(".")
    try:
        data = base64.urlsafe_b64decode(encoded.encode())
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"Invalid navigation token: {exc}") from exc
    expected_mac = hmac.new(_TOKEN_SECRET, data, hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(mac, expected_mac):
        raise ValueError("Invalid navigation token: signature mismatch")
    try:
        result = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid navigation token: {exc}") from exc
    if not isinstance(result, dict):
        raise ValueError("Invalid navigation token: payload must be an object")
    return result


def encode_navigation_token(navigation: NavigationToken) -> str:
    """Encode a NavigationToken as a URL-safe base64 JSON string."""
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
    """Decode a base64 token into a NavigationToken.

    Raises ``ValueError`` on malformed input, unknown kind, or wrong field types.
    """
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
        kind=cast("NavigationKind", kind),
        value=value,
        dialog_id=dialog_id,
        topic_id=topic_id,
        query=query,
        direction=cast("HistoryDirection | None", direction),
    )


def encode_history_navigation(
    message_id: int,
    dialog_id: int,
    *,
    topic_id: int | None = None,
    direction: HistoryDirection = HistoryDirection.NEWEST,
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


def encode_account_trace_navigation(
    *,
    target_user_id: int,
    sent_at: int,
    dialog_id: int,
    message_id: int,
    group_by: AccountTraceGroupBy,
    exact_dialog_id: int | None = None,
    exact_topic_id: int | None = None,
    sent_after: str | None = None,
    sent_before: str | None = None,
) -> str:
    """Encode an Account Trace keyset continuation cursor."""
    payload: dict[str, object] = {
        "kind": "account_trace",
        "target_user_id": target_user_id,
        "sent_at": sent_at,
        "dialog_id": dialog_id,
        "message_id": message_id,
        "group_by": group_by,
    }
    if exact_dialog_id is not None:
        payload["exact_dialog_id"] = exact_dialog_id
    if exact_topic_id is not None:
        payload["exact_topic_id"] = exact_topic_id
    if sent_after is not None:
        payload["sent_after"] = sent_after
    if sent_before is not None:
        payload["sent_before"] = sent_before
    return _encode_payload(payload)


def decode_account_trace_navigation(
    token: str,
    *,
    expected_target_user_id: int,
    expected_group_by: AccountTraceGroupBy,
    expected_exact_dialog_id: int | None = None,
    expected_exact_topic_id: int | None = None,
    expected_sent_after: str | None = None,
    expected_sent_before: str | None = None,
) -> AccountTraceNavigationToken:
    """Decode an Account Trace cursor and reject context mismatches."""
    data = _decode_payload(token)

    kind = data.get("kind")
    if kind != "account_trace":
        raise ValueError(f"Navigation token is for {kind}, not account_trace")

    target_user_id = data.get("target_user_id")
    if not isinstance(target_user_id, int):
        raise ValueError("Invalid navigation token: target_user_id must be an integer")
    if target_user_id != expected_target_user_id:
        msg = f"Navigation token belongs to account {target_user_id}, not {expected_target_user_id}"
        raise ValueError(msg)

    group_by = data.get("group_by")
    if group_by not in {"timeline", "dialog"}:
        raise ValueError("Invalid navigation token: group_by must be timeline or dialog")
    if group_by != expected_group_by:
        msg = f"Navigation token belongs to group_by {group_by}, not {expected_group_by}"
        raise ValueError(msg)

    sent_at = data.get("sent_at")
    if not isinstance(sent_at, int):
        raise ValueError("Invalid navigation token: sent_at must be an integer")

    dialog_id = data.get("dialog_id")
    if not isinstance(dialog_id, int):
        raise ValueError("Invalid navigation token: dialog_id must be an integer")

    message_id = data.get("message_id")
    if not isinstance(message_id, int):
        raise ValueError("Invalid navigation token: message_id must be an integer")

    exact_dialog_id = data.get("exact_dialog_id")
    if exact_dialog_id is not None and not isinstance(exact_dialog_id, int):
        raise ValueError("Invalid navigation token: exact_dialog_id must be an integer when present")
    if exact_dialog_id != expected_exact_dialog_id:
        msg = f"Navigation token belongs to dialog scope {exact_dialog_id}, not {expected_exact_dialog_id}"
        raise ValueError(msg)

    exact_topic_id = data.get("exact_topic_id")
    if exact_topic_id is not None and not isinstance(exact_topic_id, int):
        raise ValueError("Invalid navigation token: exact_topic_id must be an integer when present")
    if exact_topic_id != expected_exact_topic_id:
        msg = f"Navigation token belongs to topic scope {exact_topic_id}, not {expected_exact_topic_id}"
        raise ValueError(msg)

    sent_after = data.get("sent_after")
    if sent_after is not None and not isinstance(sent_after, str):
        raise ValueError("Invalid navigation token: sent_after must be a string when present")
    if sent_after != expected_sent_after:
        msg = f"Navigation token belongs to sent_after {sent_after}, not {expected_sent_after}"
        raise ValueError(msg)

    sent_before = data.get("sent_before")
    if sent_before is not None and not isinstance(sent_before, str):
        raise ValueError("Invalid navigation token: sent_before must be a string when present")
    if sent_before != expected_sent_before:
        msg = f"Navigation token belongs to sent_before {sent_before}, not {expected_sent_before}"
        raise ValueError(msg)

    return AccountTraceNavigationToken(
        target_user_id=target_user_id,
        sent_at=sent_at,
        dialog_id=dialog_id,
        message_id=message_id,
        group_by=cast("AccountTraceGroupBy", group_by),
        exact_dialog_id=exact_dialog_id,
        exact_topic_id=exact_topic_id,
        sent_after=sent_after,
        sent_before=sent_before,
    )
