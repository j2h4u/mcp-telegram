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
from .account_trace import TraceAccountMessages, trace_account_messages
from .activity import GetMyRecentActivity, get_my_recent_activity

# --- Domain modules (import triggers @mcp_tool registration) ---
from .discovery import ListDialogs, ListTopics, list_dialogs, list_topics
from .entity_info import GetEntityInfo, get_entity_info
from .feedback import SubmitFeedback, submit_feedback
from .folders import ListFolders, list_folders
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
from .unread import GetInbox, get_inbox

__all__ = [
    "TOOL_REGISTRY",
    "DaemonNotRunningError",
    "GetDialogStats",
    "GetEntityInfo",
    "GetInbox",
    "GetMyRecentActivity",
    "GetSyncAlerts",
    "GetSyncStatus",
    "GetUsageStats",
    "ListDialogs",
    "ListFolders",
    "ListMessages",
    "ListTopics",
    "MarkDialogForSync",
    "SearchMessages",
    "SubmitFeedback",
    "ToolArgs",
    "ToolResult",
    "TraceAccountMessages",
    "daemon_connection",
    "get_dialog_stats",
    "get_entity_info",
    "get_inbox",
    "get_my_recent_activity",
    "get_sync_alerts",
    "get_sync_status",
    "get_usage_stats",
    "list_dialogs",
    "list_folders",
    "list_messages",
    "list_topics",
    "mark_dialog_for_sync",
    "mcp_tool",
    "search_messages",
    "submit_feedback",
    "tool_args",
    "tool_description",
    "tool_runner",
    "trace_account_messages",
    "verify_tool_registry",
]
