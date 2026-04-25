from telethon.errors import RPCError  # type: ignore[import-untyped]


def action_text(summary: str, action: str) -> str:
    """Return a short action-oriented response body."""
    return f"{summary}\nAction: {action}"


def dialog_not_found_text(dialog_name: str, *, retry_tool: str) -> str:
    """Return an action-oriented response for missing dialogs."""
    return action_text(
        f'Dialog "{dialog_name}" was not found.',
        f"Call ListDialogs, then retry {retry_tool} with dialog set to an exact dialog id, @username, or full dialog name.",
    )


def ambiguous_dialog_text(dialog_name: str, match_lines: list[str], *, retry_tool: str) -> str:
    """Return an action-oriented response for ambiguous dialogs."""
    matches = "\n".join(match_lines)
    return (
        f'Dialog "{dialog_name}" matched multiple dialogs.\n'
        f"Action: Retry {retry_tool} with dialog set to one of the numeric ids from the matches below.\n"
        f"{matches}"
    )


def deleted_topic_text(topic_name: str, *, retry_tool: str) -> str:
    """Return the explicit user-facing message for deleted topics."""
    return action_text(
        f'Topic "{topic_name}" was deleted and can no longer be fetched.',
        f"Call ListTopics for this dialog, then retry {retry_tool} with an active topic title, or omit topic to read across all topics.",
    )


def rpc_error_detail(exc: RPCError) -> str:
    """Return the stable Telegram RPC detail for one exception."""
    detail = getattr(exc, "message", None) or str(exc)
    return str(detail)


def inaccessible_topic_text(topic_name: str, exc: RPCError, *, resolved: bool, retry_tool: str) -> str:
    """Return a readable user-facing message for inaccessible topics."""
    detail = rpc_error_detail(exc)
    if resolved:
        return action_text(
            f'Topic "{topic_name}" resolved, but Telegram rejected thread fetch ({detail}).',
            f"Retry {retry_tool} without topic to read dialog-wide messages, or call ListTopics and choose another active topic.",
        )

    return action_text(
        f'Topic "{topic_name}" could not be loaded because Telegram rejected topic access ({detail}).',
        f"Call ListTopics for this dialog, then retry {retry_tool} with an exact active topic title, or omit topic.",
    )


def topic_not_found_text(topic_name: str, *, retry_tool: str) -> str:
    """Return an action-oriented response for missing topics."""
    return action_text(
        f'Topic "{topic_name}" was not found.',
        f"Call ListTopics for this dialog, then retry {retry_tool} with an exact topic title.",
    )


def ambiguous_topic_text(topic_name: str, match_lines: list[str], *, retry_tool: str) -> str:
    """Return an action-oriented response for ambiguous topics."""
    matches = "\n".join(match_lines)
    return (
        f'Topic "{topic_name}" matched multiple topics.\n'
        f"Action: Retry {retry_tool} with topic set to one exact topic title from the matches below.\n"
        f"{matches}"
    )


def ambiguous_deleted_topic_text(topic_name: str, match_lines: list[str], *, retry_tool: str) -> str:
    """Return an action-oriented response for ambiguous deleted topics."""
    matches = "\n".join(match_lines)
    return (
        f'Deleted topic query "{topic_name}" matched multiple deleted topics.\n'
        f"Action: Call ListTopics for this dialog, then retry {retry_tool} with an active topic title instead of a deleted one.\n"
        f"{matches}"
    )


def dialog_topics_unavailable_text(dialog_name: str, exc: RPCError) -> str:
    """Return a readable message when one dialog cannot expose a topic catalog."""
    detail = rpc_error_detail(exc)
    return action_text(
        f'Dialog "{dialog_name}" does not expose a readable forum-topic catalog ({detail}).',
        "Do not use ListTopics for this dialog. Retry ListMessages without topic if you want dialog messages, or choose another forum-enabled dialog.",
    )


def no_active_topics_text(dialog_name: str) -> str:
    """Return an action-oriented response for dialogs without active topics."""
    return action_text(
        f'No active forum topics found for "{dialog_name}".',
        "Retry ListMessages without topic to read dialog-wide messages, or choose another forum-enabled dialog.",
    )


def invalid_navigation_text(detail: str, *, retry_tool: str) -> str:
    """Return an action-oriented response for malformed shared navigation tokens."""
    return action_text(
        f"Navigation token is invalid: {detail}",
        f"Retry {retry_tool} without navigation to start from the first page, or reuse the exact next_navigation value from the previous {retry_tool} response.",
    )


def sender_not_found_text(sender_name: str, *, retry_tool: str) -> str:
    """Return an action-oriented response for missing senders."""
    return action_text(
        f'Sender "{sender_name}" was not found.',
        f"Retry {retry_tool} without sender, or use an exact sender name or @username that appears in this dialog.",
    )


def ambiguous_sender_text(sender_name: str, match_lines: list[str], *, retry_tool: str) -> str:
    """Return an action-oriented response for ambiguous senders."""
    matches = "\n".join(match_lines)
    return (
        f'Sender "{sender_name}" matched multiple users.\n'
        f"Action: Retry {retry_tool} with sender set to one exact match from the list below.\n"
        f"{matches}"
    )


def ambiguous_entity_text(query: str, match_lines: list[str], *, retry_tool: str = "GetEntityInfo") -> str:
    """Multi-candidate disambiguation prompt for entity resolution."""
    joined = "\n  ".join(match_lines)
    return (
        f"Multiple entities match {query!r}. Pick one and re-run {retry_tool}:\n"
        f"  {joined}"
    )


def entity_not_found_text(query: str, *, retry_tool: str = "GetEntityInfo") -> str:
    """Single-candidate not-found prompt for entity resolution."""
    return action_text(
        f'No entity matches {query!r}.',
        f"Call ListDialogs to discover available entities, then re-run {retry_tool} with a more specific name.",
    )


def fetch_entity_info_error_text(query: str, error: str) -> str:
    """Daemon-side fetch error wrapper for entity info."""
    return action_text(
        f'Could not fetch entity info for {query!r} ({error}).',
        "Retry GetEntityInfo later. If this persists, verify the Telegram session has access to this entity.",
    )


def not_authenticated_text(retry_tool: str) -> str:
    """Return an action-oriented response for missing Telegram auth."""
    return action_text(
        "Telegram session is not authenticated.",
        f"Authenticate the Telegram session, then retry {retry_tool}.",
    )


def no_usage_data_text() -> str:
    """Return an action-oriented response when telemetry exists but has no recent rows."""
    return action_text(
        "No usage data in the past 30 days.",
        "Use any Telegram tools to generate telemetry, then retry GetUsageStats.",
    )


def usage_stats_db_missing_text() -> str:
    """Return an action-oriented response when telemetry DB is missing."""
    return action_text(
        "Analytics database not yet created.",
        "Use other tools first to generate telemetry, then retry GetUsageStats.",
    )


def usage_stats_query_error_text(error_type: str) -> str:
    """Return an action-oriented response for usage-stats query failures."""
    return action_text(
        f"Could not query usage stats ({error_type}).",
        "Retry GetUsageStats later.",
    )


def no_dialogs_text() -> str:
    """Return an action-oriented response when no dialogs are visible."""
    return action_text(
        "No dialogs were returned.",
        "Retry ListDialogs with exclude_archived=False and ignore_pinned=False, or verify that the Telegram session is authenticated and has visible dialogs.",
    )


def no_unread_personal_text() -> str:
    """Return text for empty unread in personal scope."""
    return action_text(
        "No unread messages (scope=personal).",
        'Try scope="all" for a full overview.',
    )


def no_unread_all_text() -> str:
    """Return text for empty unread in all scope."""
    return "No unread messages."


def search_no_hits_text(dialog_name: str | None, query: str) -> str:
    """Return an action-oriented response when search finds no hits.

    Pass dialog_name=None for global (all-dialogs) searches.
    """
    if dialog_name is None:
        scope = "across all synced dialogs"
        hint = "Retry SearchMessages with a broader query or without navigation."
    else:
        scope = f'in dialog "{dialog_name}"'
        hint = "Retry SearchMessages with a broader query, without navigation, or in a different dialog."
    return action_text(f'No messages matched query "{query}" {scope}.', hint)
