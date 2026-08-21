"""Tool package — aggregates domain modules and triggers registration."""

# Infrastructure (used by server.py + tests)
from ._base import (
    TOOL_REGISTRY,
    DaemonNotRunningError,
    ToolArgs,
    ToolResult,
    daemon_connection,
    mcp_tool,
    normalize_output_schema,
    omit_none_mapping_values,
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
from .folders import ListFolderMessages, ListFolders, list_folder_messages, list_folders
from .important_events import ListImportantEvents, list_important_events
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
from .unread import GetInbox, GetUnreadSummary, get_inbox, get_unread_summary

__all__ = [
    "TOOL_REGISTRY",
    "DaemonNotRunningError",
    "GetDialogStats",
    "GetEntityInfo",
    "GetInbox",
    "GetMyRecentActivity",
    "GetSyncAlerts",
    "GetSyncStatus",
    "GetUnreadSummary",
    "GetUsageStats",
    "ListDialogs",
    "ListFolderMessages",
    "ListFolders",
    "ListImportantEvents",
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
    "get_unread_summary",
    "get_usage_stats",
    "list_dialogs",
    "list_folder_messages",
    "list_folders",
    "list_important_events",
    "list_messages",
    "list_topics",
    "mark_dialog_for_sync",
    "mcp_tool",
    "normalize_output_schema",
    "omit_none_mapping_values",
    "search_messages",
    "submit_feedback",
    "tool_args",
    "tool_description",
    "tool_runner",
    "trace_account_messages",
    "verify_tool_registry",
]
