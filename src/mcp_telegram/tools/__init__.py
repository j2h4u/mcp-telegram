"""Tool package — aggregates domain modules and triggers registration."""

# Infrastructure (used by server.py + tests)
from ._base import (
    TOOL_REGISTRY,
    DaemonNotRunningError,
    ToolArgs,
    ToolResult,
    daemon_connection,
    mcp_tool,
    tool_args,
    tool_description,
    tool_runner,
    verify_tool_registry,
)

# --- Domain modules (import triggers @mcp_tool registration) ---
from .discovery import GetMyAccount, ListDialogs, ListTopics, get_my_account, list_dialogs, list_topics
from .reading import ListMessages, SearchMessages, list_messages, search_messages
from .stats import GetDialogStats, GetUsageStats, get_dialog_stats, get_usage_stats
from .sync import (
    GetSyncAlerts,
    GetSyncStatus,
    MarkDialogForSync,
    get_sync_alerts,
    get_sync_status,
    mark_dialog_for_sync,
)
from .unread import ListUnreadMessages, list_unread_messages
from .user_info import GetUserInfo, get_user_info

__all__ = [
    "TOOL_REGISTRY",
    "DaemonNotRunningError",
    "GetDialogStats",
    "GetMyAccount",
    "GetSyncAlerts",
    "GetSyncStatus",
    "GetUsageStats",
    "GetUserInfo",
    "ListDialogs",
    "ListMessages",
    "ListTopics",
    "ListUnreadMessages",
    "MarkDialogForSync",
    "SearchMessages",
    "ToolArgs",
    "ToolResult",
    "daemon_connection",
    "get_dialog_stats",
    "get_my_account",
    "get_sync_alerts",
    "get_sync_status",
    "get_usage_stats",
    "get_user_info",
    "list_dialogs",
    "list_messages",
    "list_topics",
    "list_unread_messages",
    "mark_dialog_for_sync",
    "mcp_tool",
    "search_messages",
    "tool_args",
    "tool_description",
    "tool_runner",
    "verify_tool_registry",
]
