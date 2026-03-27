"""Tool package — aggregates domain modules and triggers registration."""
from __future__ import annotations

# Infrastructure (used by server.py + tests)
from ._base import (
    TOOL_REGISTRY,
    ToolArgs,
    ToolResult,
    daemon_connection,
    DaemonNotRunningError,
    get_entity_cache,
    mcp_tool,
    tool_args,
    tool_description,
    tool_runner,
    verify_tool_registry,
)

# --- Domain modules (import triggers @mcp_tool registration) ---
from .discovery import GetMyAccount, ListDialogs, ListTopics, get_my_account, list_dialogs, list_topics
from .reading import ListMessages, SearchMessages, list_messages, search_messages
from .stats import GetUsageStats, get_usage_stats
from .unread import ListUnreadMessages, list_unread_messages
from .user_info import GetUserInfo, get_user_info
from .sync import GetSyncAlerts, GetSyncStatus, MarkDialogForSync, get_sync_alerts, get_sync_status, mark_dialog_for_sync
