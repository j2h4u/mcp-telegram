import logging

from pydantic import Field

logger = logging.getLogger(__name__)

from ..errors import (
    bootstrap_pending_text,
    no_active_topics_text,
    no_dialogs_text,
)
from ..resolver import parse_exact_dialog_id
from ._base import (
    DaemonNotRunningError,
    ToolAnnotations,
    ToolArgs,
    ToolResult,
    _check_daemon_response,
    _daemon_not_running_text,
    _text_response,
    daemon_connection,
    error_result,
    mcp_tool,
)


class ListDialogs(ToolArgs):
    """List available dialogs, chats and channels with type and last message timestamp.

    Returns both archived and non-archived dialogs by default (Telegram uses archiving as a UI
    organization tool, not data archival). Set exclude_archived=True to show only non-archived
    dialogs (equivalent to old archived=False behavior).

    Pass `filter` to narrow by dialog name: case- and script-insensitive fuzzy match.
    Order: substring in latinized space → word-initials acronym (for 2-4 char queries,
    e.g. "ЖС" → "KS x Женские Сезоны") → typo-tolerant partial ratio. Prefer a filter
    over loading the full list.

    DM rows include integer 'unread_in' (incoming unread by me) and 'unread_out' (outgoing
    unread by peer); non-DM rows omit both fields.

    sync_status values:
      - 'not_synced'  — no bulk fetch attempted
      - 'syncing'     — bulk fetch in progress
      - 'synced'      — full history mirrored locally, real-time events active
      - 'access_lost' — account no longer has access; read-only snapshot
      - 'fragment'    — no full sync; only point-fetched snippets from targeted
                        ListMessages(context_message_id=...) calls (Phase 999.1)
    """

    exclude_archived: bool = False
    ignore_pinned: bool = False
    filter: str | None = Field(default=None, max_length=200)


@mcp_tool(name="list_dialogs", title="List Dialogs", posture="secondary/helper", annotations=ToolAnnotations(readOnlyHint=True))
async def list_dialogs(args: ListDialogs) -> ToolResult:
    try:
        async with daemon_connection() as conn:
            response = await conn.list_dialogs(
                exclude_archived=args.exclude_archived,
                ignore_pinned=args.ignore_pinned,
                filter=args.filter,
            )
    except DaemonNotRunningError:
        return error_result(_daemon_not_running_text())

    if err := _check_daemon_response(response):
        return err

    data = response.get("data", {})
    dialogs = data.get("dialogs", [])

    if not dialogs:
        # Phase 44 (Plan 01 contract): bootstrap_pending=True => dialogs table is
        # empty (sync hasn't populated yet — SELECT COUNT(*) FROM dialogs = 0).
        # Render a sync-pending banner; bootstrap_pending=False => truly no
        # matches (e.g. caller's filter excluded all rows in a populated
        # table) -> preserve the existing no_dialogs_text behavior.
        if data.get("bootstrap_pending"):
            return ToolResult(
                content=_text_response(bootstrap_pending_text()),
                result_count=0,
            )
        return ToolResult(content=_text_response(no_dialogs_text()))

    entity_dicts: list[dict] = []
    lines: list[str] = []

    for d in dialogs:
        dialog_id = d.get("id")
        dialog_name = d.get("name", "")
        dialog_type = d.get("type", "unknown")
        last_at = d.get("last_message_at", "unknown")
        unread_count = d.get("unread_count", 0)
        sync_status = d.get("sync_status", "unknown")

        members = d.get("members")
        created = d.get("created")
        meta = ""
        if members is not None:
            meta += f" members={members}"
        if created is not None:
            meta += f" created={created}"

        sync_coverage_pct = d.get("sync_coverage_pct")
        access_lost_at_ts = d.get("access_lost_at")

        coverage_str = ""
        if sync_coverage_pct is not None:
            coverage_str = f" coverage={sync_coverage_pct}%"

        access_str = ""
        if access_lost_at_ts is not None:
            access_str = f" access_lost_at={access_lost_at_ts}"

        # Plan 39.3-03 Task 4 (AC-11, D-13): DM rows carry unread_in / unread_out.
        # Non-DM rows omit both fields from the daemon response; rendering is
        # conditional on key presence so we don't invent zeros for non-DMs.
        unread_rw_str = ""
        if "unread_in" in d and "unread_out" in d:
            unread_rw_str = f" unread_in={d['unread_in']} unread_out={d['unread_out']}"

        # Phase 44 DIFF-04: inline mentions/reactions/draft tokens.
        # Zero / empty values are SUPPRESSED (no `mentions=0` noise).
        # Note: draft text containing double quotes is rendered as-is — accepted
        # cosmetic behavior (threat-model T-44-07 in 44-02-PLAN.md). The renderer
        # output is text-only for an LLM; no parser interprets the format.
        diff_parts: list[str] = []
        mentions_n = d.get("unread_mentions_count", 0)
        reactions_n = d.get("unread_reactions_count", 0)
        draft = d.get("draft_text") or ""
        if mentions_n:
            diff_parts.append(f"mentions={mentions_n}")
        if reactions_n:
            diff_parts.append(f"reactions={reactions_n}")
        if draft:
            diff_parts.append(f'draft="{draft}"')
        diff_suffix = (" " + " ".join(diff_parts)) if diff_parts else ""

        lines.append(
            f"name='{dialog_name}' id={dialog_id} type={dialog_type} "
            f"last_message_at={last_at} unread={unread_count}{meta} "
            f"sync_status={sync_status}{coverage_str}{access_str}{unread_rw_str}{diff_suffix}"
        )

        # Upsert entities into daemon for future name resolution
        if isinstance(dialog_id, int) and isinstance(dialog_name, str):
            entity_dicts.append({"id": dialog_id, "type": dialog_type, "name": dialog_name, "username": None})

    # Phase 44 LISTDIALOGS-04: trailing snapshot-age annotation. None => fresh
    # (or unknown — same UX). One line, after all rows. Per RESEARCH.md
    # Assumption A2 the underlying MAX(snapshot_at) is optimistic; this is
    # documented in daemon_api.py near _SNAPSHOT_STALE_THRESHOLD_S.
    result_count = len(lines)

    snapshot_age_h = data.get("snapshot_age_h")
    if snapshot_age_h is not None:
        lines.append(f"[snapshot_age={snapshot_age_h}h — data may be stale]")

    if entity_dicts:
        try:
            async with daemon_connection() as upsert_conn:
                await upsert_conn.upsert_entities(entities=entity_dicts)
        except Exception:
            logger.debug("entity_upsert_skipped", exc_info=True)

    result_text = "\n".join(lines)
    return ToolResult(content=_text_response(result_text), result_count=result_count)


class ListTopics(ToolArgs):
    """
    List forum topics for one dialog.

    Use this before topic= when working with forum supergroups so you can choose an exact topic
    name or numeric topic_id instead of guessing via fuzzy match.
    """

    dialog: str = Field(max_length=500)


@mcp_tool(name="list_topics", title="List Topics", posture="secondary/helper", annotations=ToolAnnotations(readOnlyHint=True))
async def list_topics(args: ListTopics) -> ToolResult:
    # Try to resolve dialog_id from parsing as numeric/username first
    dialog_id: int | None = parse_exact_dialog_id(args.dialog)
    dialog_name: str | None = None if dialog_id is not None else args.dialog

    try:
        async with daemon_connection() as conn:
            if dialog_id is not None and dialog_id != 0:
                response = await conn.list_topics(dialog_id=dialog_id)
            else:
                response = await conn.list_topics(dialog=dialog_name)
    except DaemonNotRunningError:
        return error_result(_daemon_not_running_text(), has_filter=True)

    if not response.get("ok"):
        error_code = response.get("error", "")
        error_msg = response.get("message", "Request failed.")
        if error_code == "dialog_not_found":
            from ..errors import dialog_not_found_text

            return error_result(dialog_not_found_text(args.dialog, retry_tool="ListTopics"), has_filter=True)
        return error_result(f"Error: {error_msg}", has_filter=True)

    data = response.get("data", {})
    topics = data.get("topics", [])

    if not topics:
        dialog_display = args.dialog
        return ToolResult(
            content=_text_response(no_active_topics_text(dialog_display)),
            has_filter=True,
        )

    lines: list[str] = []
    for topic in topics:
        topic_id = topic.get("id")
        title = topic.get("title", "")
        lines.append(f'topic_id={topic_id} title="{title}"')

    result_text = "\n".join(lines)
    return ToolResult(content=_text_response(result_text), result_count=len(lines), has_filter=True)
