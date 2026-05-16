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
from .discovery import ListDialogs, ListTopics, list_dialogs, list_topics
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
from .activity import GetMyRecentActivity, get_my_recent_activity  # noqa: F401
from .unread import GetInbox, get_inbox
from .entity_info import GetEntityInfo, get_entity_info
from .feedback import SubmitFeedback, submit_feedback
from .account_trace import TraceAccountMessages, trace_account_messages

__all__ = [
    "DaemonNotRunningError",
    "GetDialogStats",
    "GetEntityInfo",
    "GetInbox",
    "GetMyRecentActivity",
    "GetSyncAlerts",
    "GetSyncStatus",
    "GetUsageStats",
    "ListDialogs",
    "ListMessages",
    "ListTopics",
    "MarkDialogForSync",
    "SearchMessages",
    "SubmitFeedback",
    "TOOL_REGISTRY",
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
