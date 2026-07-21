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
_MAX_SCOPE_DIALOG_IDS = 2


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
    sent_at: int | None = None
    message_state: str | None = None
    since_utc: int | None = None
    until_utc: int | None = None


@dataclass(frozen=True)
class AccountTraceNavigationToken:
    """Opaque keyset cursor for Account Trace continuation.

    The cursor is bound to the account, grouping mode, optional dialog/topic
    scope, and time bounds so callers cannot accidentally reuse a page token
    for a different trace.

    ``scope_dialog_ids`` carries the effective dialog-id set for a channel-
    scoped trace that has a linked discussion group (≤2 ids: channel +
    linked chat).  It is encoded in the token and fed back into
    ``_build_trace_account_messages_query(scope_dialog_ids=…)`` on decode so
    the expanded ``m.dialog_id IN (…)`` scope is preserved across pages
    WITHOUT any live ``GetFullChannelRequest`` re-resolution.

    When absent/None the decoder falls back to the scalar ``exact_dialog_id``
    path with no live call (legacy token or non-channel trace).

    The recompute fork (resolving the channel again on decode) is intentionally
    dropped here: a cache expiry between pages could FloodWait and silently
    collapse the scope back to the scalar channel id, causing page-boundary
    evidence loss (cycle-4 MEDIUM finding from OpenCode).
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
    scope_dialog_ids: list[int] | None = None


@dataclass(frozen=True)
class AccountTraceNavigationRequest:
    """Input parameters for encoding an Account Trace continuation cursor."""

    target_user_id: int
    sent_at: int
    dialog_id: int
    message_id: int
    group_by: AccountTraceGroupBy
    exact_dialog_id: int | None = None
    exact_topic_id: int | None = None
    sent_after: str | None = None
    sent_before: str | None = None
    scope_dialog_ids: list[int] | None = None


@dataclass(frozen=True)
class AccountTraceNavigationContext:
    """Expected context for decoding an Account Trace continuation cursor."""

    expected_target_user_id: int
    expected_group_by: AccountTraceGroupBy
    expected_exact_dialog_id: int | None = None
    expected_exact_topic_id: int | None = None
    expected_sent_after: str | None = None
    expected_sent_before: str | None = None


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
        result = cast(dict[str, object], json.loads(data))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid navigation token: {exc}") from exc
    if not isinstance(result, dict):
        raise ValueError("Invalid navigation token: payload must be an object")
    return result


def encode_navigation_token(navigation: NavigationToken) -> str:
    """Encode a NavigationToken as a URL-safe base64 JSON string."""
    _validate_navigation_shape(navigation)
    payload: dict[str, object] = {
        "kind": navigation.kind,
        "value": navigation.value,
        "dialog_id": navigation.dialog_id,
    }
    payload.update(_optional_navigation_fields(navigation))
    return _encode_payload(payload)


def _optional_navigation_fields(navigation: NavigationToken) -> dict[str, object]:
    return {
        key: value
        for key, value in {
            "topic_id": navigation.topic_id,
            "query": navigation.query,
            "direction": navigation.direction,
            "sent_at": navigation.sent_at,
            "message_state": navigation.message_state,
            "since_utc": navigation.since_utc,
            "until_utc": navigation.until_utc,
        }.items()
        if value is not None
    }


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

    topic_id, query, direction, sent_at, message_state, since_utc, until_utc = _decode_optional_navigation_fields(data)
    navigation = NavigationToken(
        kind=cast("NavigationKind", kind),
        value=value,
        dialog_id=dialog_id,
        topic_id=topic_id,
        query=query,
        direction=cast("HistoryDirection | None", direction),
        sent_at=sent_at,
        message_state=cast("str | None", message_state),
        since_utc=since_utc,
        until_utc=until_utc,
    )
    _validate_navigation_shape(navigation)
    return navigation


def _validate_navigation_shape(navigation: NavigationToken) -> None:
    """Reject signed cursors that are not fully bound to their request state."""
    if (
        navigation.since_utc is not None
        and navigation.until_utc is not None
        and navigation.since_utc >= navigation.until_utc
    ):
        raise ValueError("Invalid navigation token: since_utc must be earlier than until_utc")
    if navigation.message_state not in {"sent", "scheduled", "all"}:
        raise ValueError("Invalid navigation token: message_state must be sent, scheduled, or all")
    if navigation.kind == "search":
        if navigation.query is None:
            raise ValueError("Invalid navigation token: search cursor requires query")
        if any(value is not None for value in (navigation.topic_id, navigation.direction, navigation.sent_at)):
            raise ValueError("Invalid navigation token: search cursor contains history-only state")
        return
    if navigation.query is not None:
        raise ValueError("Invalid navigation token: history cursor contains search-only query state")


def _decode_optional_navigation_fields(
    data: dict[str, object],
) -> tuple[int | None, str | None, str | None, int | None, str | None, int | None, int | None]:
    topic_id = data.get("topic_id")
    if topic_id is not None and not isinstance(topic_id, int):
        raise ValueError("Invalid navigation token: topic_id must be an integer when present")

    query = data.get("query")
    if query is not None and not isinstance(query, str):
        raise ValueError("Invalid navigation token: query must be a string when present")

    direction = data.get("direction")
    if direction is not None and direction not in {"newest", "oldest"}:
        raise ValueError("Invalid navigation token: direction must be newest or oldest when present")

    sent_at = data.get("sent_at")
    if sent_at is not None and not isinstance(sent_at, int):
        raise ValueError("Invalid navigation token: sent_at must be an integer when present")

    message_state = data.get("message_state")
    if message_state is not None and message_state not in {"sent", "scheduled", "all"}:
        raise ValueError("Invalid navigation token: message_state must be sent, scheduled, or all when present")

    since_utc = data.get("since_utc")
    if since_utc is not None and not isinstance(since_utc, int):
        raise ValueError("Invalid navigation token: since_utc must be an integer when present")

    until_utc = data.get("until_utc")
    if until_utc is not None and not isinstance(until_utc, int):
        raise ValueError("Invalid navigation token: until_utc must be an integer when present")

    return (
        cast(int | None, topic_id),
        cast(str | None, query),
        cast(str | None, direction),
        cast(int | None, sent_at),
        cast(str | None, message_state),
        cast(int | None, since_utc),
        cast(int | None, until_utc),
    )


def encode_history_navigation(  # noqa: PLR0913
    message_id: int,
    dialog_id: int,
    *,
    topic_id: int | None = None,
    direction: HistoryDirection = HistoryDirection.NEWEST,
    sent_at: int | None = None,
    message_state: str,
    since_utc: int | None = None,
    until_utc: int | None = None,
) -> str:
    """Encode a history continuation cursor as a base64 token."""
    return encode_navigation_token(
        NavigationToken(
            kind="history",
            value=message_id,
            dialog_id=dialog_id,
            topic_id=topic_id,
            direction=direction,
            sent_at=sent_at,
            message_state=message_state,
            since_utc=since_utc,
            until_utc=until_utc,
        )
    )


def encode_search_navigation(  # noqa: PLR0913
    offset: int,
    dialog_id: int,
    query: str,
    message_state: str,
    *,
    since_utc: int | None = None,
    until_utc: int | None = None,
) -> str:
    """Encode a search continuation cursor as a base64 token."""
    return encode_navigation_token(
        NavigationToken(
            kind="search",
            value=offset,
            dialog_id=dialog_id,
            query=query,
            message_state=message_state,
            since_utc=since_utc,
            until_utc=until_utc,
        )
    )


def encode_account_trace_navigation(request: AccountTraceNavigationRequest) -> str:
    """Encode an Account Trace keyset continuation cursor.

    ``scope_dialog_ids`` encodes the effective dialog-id set so the next page
    rebuilds the same ``m.dialog_id IN (…)`` scope from the token alone — no
    live ``GetFullChannelRequest`` re-resolution on decode.  Bounded at ≤2 ids.
    """
    payload: dict[str, object] = {
        "kind": "account_trace",
        "target_user_id": request.target_user_id,
        "sent_at": request.sent_at,
        "dialog_id": request.dialog_id,
        "message_id": request.message_id,
        "group_by": request.group_by,
    }
    if request.exact_dialog_id is not None:
        payload["exact_dialog_id"] = request.exact_dialog_id
    if request.exact_topic_id is not None:
        payload["exact_topic_id"] = request.exact_topic_id
    if request.sent_after is not None:
        payload["sent_after"] = request.sent_after
    if request.sent_before is not None:
        payload["sent_before"] = request.sent_before
    if request.scope_dialog_ids is not None:
        payload["scope_dialog_ids"] = request.scope_dialog_ids
    return _encode_payload(payload)


def _require_navigation_int_field(data: dict[str, object], field_name: str) -> int:
    value = data.get(field_name)
    if not isinstance(value, int):
        raise ValueError(f"Invalid navigation token: {field_name} must be an integer")
    return value


def _require_navigation_optional_int_field(data: dict[str, object], field_name: str) -> int | None:
    value = data.get(field_name)
    if value is not None and not isinstance(value, int):
        raise ValueError(f"Invalid navigation token: {field_name} must be an integer when present")
    return cast("int | None", value)


def _require_navigation_optional_str_field(data: dict[str, object], field_name: str) -> str | None:
    value = data.get(field_name)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"Invalid navigation token: {field_name} must be a string when present")
    return cast("str | None", value)


def _require_navigation_value_matches(actual: object, expected: object, message: str) -> None:
    if actual != expected:
        raise ValueError(message)


def _require_account_trace_group_by(value: object) -> AccountTraceGroupBy:
    if value not in {"timeline", "dialog"}:
        raise ValueError("Invalid navigation token: group_by must be timeline or dialog")
    return cast("AccountTraceGroupBy", value)


def _require_scope_dialog_ids(value: object) -> list[int] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("Invalid navigation token: scope_dialog_ids must be a list when present")
    if len(value) > _MAX_SCOPE_DIALOG_IDS:
        raise ValueError(
            f"Invalid navigation token: scope_dialog_ids must have at most {_MAX_SCOPE_DIALOG_IDS} elements"
        )
    for item in value:
        if not isinstance(item, int):
            raise ValueError("Invalid navigation token: all scope_dialog_ids elements must be integers")
    return [int(item) for item in value]


def decode_account_trace_navigation(
    token: str,
    context: AccountTraceNavigationContext,
) -> AccountTraceNavigationToken:
    """Decode an Account Trace cursor and reject context mismatches."""
    data = _decode_payload(token)
    kind = data.get("kind")
    if kind != "account_trace":
        raise ValueError(f"Navigation token is for {kind}, not account_trace")

    target_user_id = _require_navigation_int_field(data, "target_user_id")
    _require_navigation_value_matches(
        target_user_id,
        context.expected_target_user_id,
        f"Navigation token belongs to account {target_user_id}, not {context.expected_target_user_id}",
    )

    group_by = _require_account_trace_group_by(data.get("group_by"))
    _require_navigation_value_matches(
        group_by,
        context.expected_group_by,
        f"Navigation token belongs to group_by {group_by}, not {context.expected_group_by}",
    )

    sent_at = _require_navigation_int_field(data, "sent_at")
    dialog_id = _require_navigation_int_field(data, "dialog_id")
    message_id = _require_navigation_int_field(data, "message_id")

    exact_dialog_id = _require_navigation_optional_int_field(data, "exact_dialog_id")
    _require_navigation_value_matches(
        exact_dialog_id,
        context.expected_exact_dialog_id,
        f"Navigation token belongs to dialog scope {exact_dialog_id}, not {context.expected_exact_dialog_id}",
    )

    exact_topic_id = _require_navigation_optional_int_field(data, "exact_topic_id")
    _require_navigation_value_matches(
        exact_topic_id,
        context.expected_exact_topic_id,
        f"Navigation token belongs to topic scope {exact_topic_id}, not {context.expected_exact_topic_id}",
    )

    sent_after = _require_navigation_optional_str_field(data, "sent_after")
    _require_navigation_value_matches(
        sent_after,
        context.expected_sent_after,
        f"Navigation token belongs to sent_after {sent_after}, not {context.expected_sent_after}",
    )

    sent_before = _require_navigation_optional_str_field(data, "sent_before")
    _require_navigation_value_matches(
        sent_before,
        context.expected_sent_before,
        f"Navigation token belongs to sent_before {sent_before}, not {context.expected_sent_before}",
    )

    scope_dialog_ids = _require_scope_dialog_ids(data.get("scope_dialog_ids"))

    return AccountTraceNavigationToken(
        target_user_id=target_user_id,
        sent_at=sent_at,
        dialog_id=dialog_id,
        message_id=message_id,
        group_by=group_by,
        exact_dialog_id=exact_dialog_id,
        exact_topic_id=exact_topic_id,
        sent_after=sent_after,
        sent_before=sent_before,
        scope_dialog_ids=scope_dialog_ids,
    )
