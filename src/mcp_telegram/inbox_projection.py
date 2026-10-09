"""Shared bounded inbox projection, applied before Unix IPC for MCP requests."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Protocol, cast

from .budget import unread_chat_tier
from .entity_identity import project_entity_identity
from .formatter import _render_read_state_header
from .message_view import project_message_view, project_read_markers
from .models import DialogType, ReadMessage, ReadState
from .structured import StructuredWarning, structured_warning
from .temporal import project_temporal_response
from .topic_identity import project_topic


class InboxProjectionOptions(Protocol):
    limit: int
    group_size_threshold: int
    dialogs_per_page: int
    messages_per_dialog: int
    preview_chars: int
    response_chars: int
    include_dialog_types: list[DialogType] | None
    timezone: str


INBOX_MESSAGE_FIELDS = frozenset(
    {
        "dialog_id",
        "msg_id",
        "sent_at",
        "sender",
        "out",
        "is_service",
        "composition_is_telegram_content",
        "topic",
        "content",
        "media",
        "reply_context_ref",
        "edit_date",
        "read_at",
        "is_deleted",
        "deleted_at",
        "read_markers",
    }
)


_MAX_INBOX_USERNAME_CHARS = 64
_MAX_INBOX_TOPIC_CHARS = 256


def _read_state_scalar(key: str, value: object) -> bool:
    if key not in ReadState.__annotations__:
        return False
    return (
        isinstance(value, str)
        if key.endswith("cursor_state")
        else isinstance(value, int) and not isinstance(value, bool)
    )


def _read_state_payload(read_state: ReadState | dict | None, dialog_type: str | None) -> dict[str, object] | None:
    if read_state is None and dialog_type is None:
        return None
    state = {key: value for key, value in (read_state or {}).items() if _read_state_scalar(key, value)}
    for key in ("inbox_cursor_state", "outbox_cursor_state"):
        if key in state:
            state[key] = str(state[key])[:32]
    return {
        "dialog_type": dialog_type,
        "state": state if read_state is not None else None,
        "header_lines": [line[:400] for line in _render_read_state_header(state, dialog_type, int(time.time()))[:2]],
    }


def _read_position_pending_warnings(read_position_pending_count: int) -> list[StructuredWarning]:
    if read_position_pending_count <= 0:
        return []

    warning_message = (
        f"read_position_pending_count={read_position_pending_count} dialog(s) have no inbox read position yet. "
        "Results may be incomplete until the sync daemon reconciles them."
    )
    return [
        structured_warning(
            "read_position_pending",
            warning_message,
            severity="warning",
            action="Retry shortly while the sync daemon reconciles read positions.",
        )
    ]


def _display_name_source(value: object) -> str:
    return value if isinstance(value, str) and value in {"name", "username", "numeric"} else "numeric"


def _project_read_position_pending_entity(
    raw: object,
) -> tuple[tuple[str, str | int], dict[str, object]] | None:
    if not isinstance(raw, Mapping):
        return None
    dialog_id = raw.get("dialog_id")
    if isinstance(dialog_id, bool) or not isinstance(dialog_id, int):
        return None
    identity = _bounded_identity(
        display_name=_identity_text_fact(raw.get("display_name")),
        username=_identity_text_fact(raw.get("username")),
        telegram_id=dialog_id,
    )
    username_value = cast(str | None, identity.get("username"))
    key = (
        ("username", username_value)
        if username_value is not None
        else ("telegram_id", cast(int, identity.get("telegram_id")))
    )
    return key, {
        "entity": identity,
        "display_name_source": _display_name_source(raw.get("display_name_source")),
    }


def _project_read_position_pending_entities(raw_entities: object) -> list[dict[str, object]]:
    """Project and deduplicate bounded pending identities for the MCP contract."""
    if not isinstance(raw_entities, list):
        return []
    projected: list[dict[str, object]] = []
    seen: set[tuple[str, str | int]] = set()
    for raw in raw_entities[:20]:
        candidate = _project_read_position_pending_entity(raw)
        if candidate is None:
            continue
        key, entity = candidate
        if key in seen:
            continue
        seen.add(key)
        projected.append(entity)
    return projected


def _structured_inbox_group(
    group: dict, preview_chars: int = 400, messages_per_dialog: int = 5
) -> tuple[dict[str, object], dict[str, object] | None, int]:
    message_rows = group.get("messages", [])[:messages_per_dialog]
    total_in_chat = int(group.get("total_in_chat", group.get("unread_count", 0)) or 0)
    hidden_count = max(0, total_in_chat - len(message_rows))
    read_state = group.get("read_state")
    read_state_payload = read_state if isinstance(read_state, dict) else None
    telegram_id = int(group.get("dialog_id", 0) or 0)
    entity = _bounded_identity(
        display_name=group.get("display_name"),
        username=group.get("username"),
        telegram_id=telegram_id,
    )
    dialog_type = DialogType.parse(group.get("dialog_type")).value if group.get("dialog_type") is not None else None
    category = DialogType.parse(group.get("category")).value if group.get("category") is not None else None
    dialog = {
        "entity": entity,
        "display_name_source": _display_name_source(group.get("display_name_source")),
        "category": category,
        "dialog_type": dialog_type,
        "unread_count": group.get("unread_count", 0),
        "unread_mentions_count": group.get("unread_mentions_count", 0),
        "total_in_chat": total_in_chat,
        "is_channel": DialogType.parse(group.get("category")) == DialogType.CHANNEL,
        "is_bot": DialogType.parse(group.get("category")) == DialogType.BOT,
        "read_state": _read_state_payload(read_state_payload, dialog_type),
        "budget": {
            "shown_count": len(message_rows),
            "total_in_chat": total_in_chat,
            "hidden_count": hidden_count,
        },
        "messages": _structured_messages(
            message_rows,
            read_state=read_state_payload,
            dialog_type=group.get("dialog_type"),
            preview_chars=preview_chars,
        ),
    }
    hidden_entry: dict[str, object] | None = None
    if hidden_count:
        hidden_entry = {
            "entity": entity,
            "display_name_source": _display_name_source(group.get("display_name_source")),
            "hidden_count": hidden_count,
        }
    return dialog, hidden_entry, len(message_rows)


def _structured_inbox_groups(
    groups: list[dict],
    preview_chars: int = 400,
    messages_per_dialog: int = 5,
) -> tuple[list[dict[str, object]], list[dict[str, object]], int]:
    structured_dialogs: list[dict[str, object]] = []
    hidden_count_by_dialog: list[dict[str, object]] = []
    result_message_count = 0
    for group in groups:
        dialog, hidden_entry, message_count = _structured_inbox_group(group, preview_chars, messages_per_dialog)
        structured_dialogs.append(dialog)
        result_message_count += message_count
        if hidden_entry is not None:
            hidden_count_by_dialog.append(hidden_entry)
    return structured_dialogs, hidden_count_by_dialog, result_message_count


def _structured_messages(
    rows: list[dict], *, read_state: dict | None, dialog_type: str | None, preview_chars: int = 400
) -> list[dict[str, object]]:
    if not rows:
        return []
    ordered_rows = sorted(
        rows,
        key=lambda row: (
            int(row.get("sent_at") or 0),
            int(row.get("message_id") or 0),
        ),
    )
    messages = [ReadMessage(**row) for row in ordered_rows]
    marker_by_message = project_read_markers(messages, read_state=read_state, dialog_type=dialog_type)
    return [
        _project_inbox_message(
            message, project_message_view(message, read_marker=marker_by_message.get(message.id)), preview_chars
        )
        for message in messages
    ]


def _project_inbox_message(message: ReadMessage, full: dict[str, object], preview_chars: int) -> dict[str, object]:
    item = {key: value for key, value in full.items() if key in INBOX_MESSAGE_FIELDS}
    actor_id = message.effective_sender_id or message.sender_id
    if "sender" in item and actor_id is not None:
        sender = cast(dict[str, object], item["sender"])
        item["sender"] = _bounded_identity(
            display_name=cast(str, sender["display_name"]),
            username=cast(str | None, sender.get("username")),
            telegram_id=actor_id,
        )
    topic = project_topic(
        topic_id=message.forum_topic_id,
        title=message.topic_title if len(message.topic_title or "") <= _MAX_INBOX_TOPIC_CHARS else None,
    )
    item.pop("topic", None)
    if topic is not None:
        item["topic"] = topic
    source_length, truncated = _clip_inbox_content(item, preview_chars)
    item.update({"content_truncated": truncated, "content_source_length": source_length})
    return item


def _clip_inbox_content(item: dict[str, object], preview_chars: int | None) -> tuple[int, bool]:
    source_length = 0
    truncated = False
    for container, field in (("content", "text"), ("media", "description")):
        value = item.get(container)
        if not isinstance(value, dict) or not isinstance(value.get(field), str):
            continue
        original = value[field]
        source_length += len(original)
        cap = preview_chars if preview_chars is not None else max(32, len(original) // 2)
        if len(original) > cap:
            value[field] = original[: cap - 3] + "..."
            truncated = True
    return source_length, truncated


def _inbox_size(payload: Mapping[str, object]) -> int:
    return len(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))


def _drop_largest_inbox_preview(dialogs: list[object]) -> bool:
    candidates = [
        (position, dialog)
        for position, dialog in enumerate(dialogs)
        if isinstance(dialog, dict) and dialog.get("messages")
    ]
    if not candidates:
        return False
    _, dialog = max(
        candidates, key=lambda item: (unread_chat_tier(item[1]), len(cast(list[object], item[1]["messages"])), item[0])
    )
    messages = cast(list[object], dialog["messages"])
    oldest = messages[0]
    if isinstance(oldest, dict) and _clip_inbox_content(oldest, None)[1]:
        oldest["content_truncated"] = True
        return True
    messages.pop(0)
    budget = dialog.get("budget")
    if isinstance(budget, dict):
        total = int(budget.get("total_in_chat", 0) or 0)
        budget["shown_count"] = len(messages)
        budget["hidden_count"] = max(0, total - len(messages))
    return True


def _inbox_valid_dialogs(dialogs: list[object]) -> list[dict[str, object]]:
    return [dialog for dialog in dialogs if isinstance(dialog, dict)]


def _inbox_message_count(dialogs: list[dict[str, object]]) -> int:
    return sum(
        len(cast(list[object], dialog.get("messages", [])))
        for dialog in dialogs
        if isinstance(dialog.get("messages", []), list)
    )


def _inbox_hidden_count(dialogs: list[dict[str, object]]) -> int:
    return sum(
        int(cast(int, cast(dict[str, object], dialog["budget"]).get("hidden_count", 0) or 0))
        for dialog in dialogs
        if isinstance(dialog.get("budget"), dict)
    )


def _inbox_hidden_entry(dialog: dict[str, object]) -> dict[str, object] | None:
    budget = dialog.get("budget")
    if not isinstance(budget, dict) or not budget.get("hidden_count"):
        return None
    return {
        "entity": dialog["entity"],
        "display_name_source": dialog["display_name_source"],
        "hidden_count": budget["hidden_count"],
    }


def _inbox_hidden_by_dialog(dialogs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [entry for dialog in dialogs if (entry := _inbox_hidden_entry(dialog)) is not None]


def _reconcile_inbox_receipts(payload: dict[str, object], dialogs: list[object]) -> None:
    budget = payload.get("budget")
    if not isinstance(budget, dict):
        return
    valid_dialogs = _inbox_valid_dialogs(dialogs)
    budget["result_message_count"] = _inbox_message_count(valid_dialogs)
    budget["hidden_count"] = _inbox_hidden_count(valid_dialogs)
    budget["hidden_count_by_dialog"] = _inbox_hidden_by_dialog(valid_dialogs)


def _count_inbox_truncated_content(dialogs: list[object]) -> int:
    return sum(
        1
        for dialog in dialogs
        if isinstance(dialog, dict)
        for message in cast(list[dict[str, object]], dialog.get("messages", []))
        if message.get("content_truncated") is True
    )


def _finalize_inbox_payload(payload: dict[str, object], response_chars: int = 24000) -> int | None:
    """Measure the final wire projection with all receipts and warnings reconciled."""
    dialogs = payload.get("dialogs")
    if not isinstance(dialogs, list):
        return 0
    while True:
        _reconcile_inbox_receipts(payload, dialogs)
        payload["count"] = len(dialogs)
        budget = cast(dict[str, object], payload["budget"])
        shown_messages = _inbox_int(budget, "result_message_count", 0)
        payload["shown_message_count"] = shown_messages
        budget["dialog_count"] = len(dialogs)
        budget["hidden_count"] = max(0, _inbox_int(payload, "page_message_count", 0) - shown_messages)
        payload["selection_complete"] = (
            payload.get("total_dialog_count") == len(dialogs) and payload.get("total_message_count") == shown_messages
        )
        payload["content_truncated_count"] = _count_inbox_truncated_content(dialogs)
        warnings = _read_position_pending_warnings(_inbox_int(payload, "read_position_pending_count", 0))
        if budget["hidden_count"]:
            warnings.append(
                structured_warning(
                    "inbox_previews_hidden",
                    f"{budget['hidden_count']} current-page messages are not shown in these previews.",
                    action="Read full messages with list_messages using the dialog entity username or telegram_id; inspect deleted-message tombstones with list_conversation_changes.",
                )
            )
        payload["warnings"] = warnings
        if _inbox_size(payload) <= response_chars:
            return shown_messages
        if not _drop_largest_inbox_preview(dialogs):
            return None


def _bounded_identity(*, display_name: str | None, username: str | None, telegram_id: int) -> dict:
    identity = project_entity_identity(display_name=display_name, username=username, telegram_id=telegram_id)
    canonical_username = identity.get("username")
    if isinstance(canonical_username, str) and len(canonical_username) > _MAX_INBOX_USERNAME_CHARS:
        identity = project_entity_identity(display_name=display_name, username=None, telegram_id=telegram_id)
    identity["display_name"] = identity["display_name"][:128]
    return dict(identity)


def _identity_text_fact(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _inbox_int(data: Mapping[str, object], key: str, default: int) -> int:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _inbox_paging_payload(data: Mapping[str, object], dialog_count: int, message_count: int) -> dict[str, object]:
    return {
        "page": _inbox_int(data, "page", 1),
        "total_dialog_count": _inbox_int(data, "total_dialog_count", dialog_count),
        "shown_dialog_count": _inbox_int(data, "shown_dialog_count", dialog_count),
        "remaining_dialog_count": _inbox_int(data, "remaining_dialog_count", 0),
        "next_page": data.get("next_page"),
        "total_message_count": _inbox_int(data, "total_message_count", message_count),
        "page_message_count": _inbox_int(data, "page_message_count", message_count),
        "shown_message_count": _inbox_int(data, "shown_message_count", message_count),
    }


def _inbox_coverage_payload(pending_count: int, pending_entities: list[dict[str, object]]) -> dict[str, object]:
    complete = pending_count == 0
    return {
        "complete": complete,
        "state": "complete" if complete else "partial",
        "scope": "DB read-cursor coverage only; it does not mean all unread messages were selected.",
        "read_position_pending_count": pending_count,
        "read_position_pending_entities": pending_entities,
    }


def _inbox_budget_payload(
    args: InboxProjectionOptions,
    dialogs: list[dict[str, object]],
    hidden_by_dialog: list[dict[str, object]],
    message_count: int,
    page_message_count: int,
) -> dict[str, object]:
    return {
        "requested_limit": args.limit,
        "result_message_count": message_count,
        "dialog_count": len(dialogs),
        "hidden_count": max(0, page_message_count - message_count),
        "hidden_count_by_dialog": hidden_by_dialog,
        "allocation_policy": (
            f"ranked round-robin limit={args.limit}, messages_per_dialog={args.messages_per_dialog}, "
            f"dialogs_per_page={args.dialogs_per_page}, preview_chars={args.preview_chars}, "
            f"response_chars={args.response_chars}; shorten then remove lower-priority previews first"
        ),
    }


def project_inbox_payload(
    args: InboxProjectionOptions,
    data: Mapping[str, object],
    *,
    applied_since_utc: str | None,
) -> dict[str, object] | None:
    groups = cast(list[dict], data.get("groups", []))
    # The daemon contract is atomic: missing read-position coverage fields are
    # a protocol defect, never an implicit "no pending work" result.
    read_position_pending_count = int(cast(int | str, data["read_position_pending_count"]))
    read_position_pending_entities = _project_read_position_pending_entities(data["read_position_pending_entities"])
    warnings = _read_position_pending_warnings(read_position_pending_count)
    structured_dialogs, hidden_count_by_dialog, result_message_count = _structured_inbox_groups(
        groups[: args.dialogs_per_page], args.preview_chars, args.messages_per_dialog
    )
    content_truncated_count = _count_inbox_truncated_content(cast(list[object], structured_dialogs))
    page_message_count = sum(_inbox_int(dialog, "total_in_chat", 0) for dialog in structured_dialogs)
    structured_content: dict[str, object] = {
        "limit": args.limit,
        "group_size_threshold": args.group_size_threshold,
        "applied_since_utc": applied_since_utc,
        "read_position_pending_count": read_position_pending_count,
        "read_position_pending_entities": read_position_pending_entities,
        "coverage": _inbox_coverage_payload(read_position_pending_count, read_position_pending_entities),
        "warnings": warnings,
        "budget": _inbox_budget_payload(
            args,
            structured_dialogs,
            hidden_count_by_dialog,
            result_message_count,
            _inbox_int(data, "page_message_count", page_message_count),
        ),
        "selection_complete": False,
        "content_truncated_count": content_truncated_count,
        "dialogs": structured_dialogs,
        "count": len(structured_dialogs),
        "result_count_semantics": "count is the number of unread dialogs returned; budget.result_message_count is the number of message rows shown",
        **_inbox_paging_payload(data, len(structured_dialogs), page_message_count),
    }
    if args.include_dialog_types is not None:
        structured_content["applied_dialog_types"] = list(
            dict.fromkeys(item.value for item in args.include_dialog_types)
        )

    structured_content = project_temporal_response(structured_content, args.timezone)
    if _finalize_inbox_payload(structured_content, args.response_chars) is None:
        return None
    return structured_content
