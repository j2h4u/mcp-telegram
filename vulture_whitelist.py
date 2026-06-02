# Vulture whitelist — intentionally-retained symbols the static scan reports as unused.
#
# These are NOT dead code. They are the human-readable TEXT-RENDERING layer, kept on
# purpose after the Phase 52 migration to structured MCP output. Successful MCP tool
# calls are structured-only (all agent-facing data goes in `structuredContent`); the
# text formatters and error-text helpers below are reserved for non-MCP surfaces —
# notably a future CLI — and stay heavily test-covered (see tests/test_formatter*.py,
# tests/test_errors.py). Rationale is documented in AGENTS.md:
#   "Text rendering belongs to non-MCP surfaces such as a future CLI."
#
# Deleting this layer was considered and rejected: the text output was expensively
# tuned, so it is retained as the CLI substrate rather than re-derived later.
#
# Mechanics: vulture counts every name referenced in this file as "used", so the
# advisory dead-code scan surfaces only genuinely-NEW dead code instead of these
# known false positives. The `# unused ... (path:line)` comments keep the list
# regenerable — refresh with: `uv run vulture --make-whitelist`.
#
# Scope note: this whitelist covers ONLY the retained text layer. Other vulture
# findings (e.g. TypedDict fields read as "unused variable", the dotMD adapter API in
# daemon_client.py) are deliberately NOT whitelisted — triage those on their own merits.

# --- errors.py: actionable error-message helpers (text layer) ---
ambiguous_dialog_text  # unused function (src/mcp_telegram/errors.py:17)
deleted_topic_text  # unused function (src/mcp_telegram/errors.py:27)
inaccessible_topic_text  # unused function (src/mcp_telegram/errors.py:41)
topic_not_found_text  # unused function (src/mcp_telegram/errors.py:56)
ambiguous_topic_text  # unused function (src/mcp_telegram/errors.py:64)
ambiguous_deleted_topic_text  # unused function (src/mcp_telegram/errors.py:74)
dialog_topics_unavailable_text  # unused function (src/mcp_telegram/errors.py:84)
no_active_topics_text  # unused function (src/mcp_telegram/errors.py:93)
sender_not_found_text  # unused function (src/mcp_telegram/errors.py:109)
ambiguous_sender_text  # unused function (src/mcp_telegram/errors.py:117)
ambiguous_entity_text  # unused function (src/mcp_telegram/errors.py:127)
not_authenticated_text  # unused function (src/mcp_telegram/errors.py:152)
usage_stats_db_missing_text  # unused function (src/mcp_telegram/errors.py:168)
no_dialogs_text  # unused function (src/mcp_telegram/errors.py:184)
bootstrap_pending_text  # unused function (src/mcp_telegram/errors.py:192)
no_unread_personal_text  # unused function (src/mcp_telegram/errors.py:204)
no_unread_all_text  # unused function (src/mcp_telegram/errors.py:212)
search_no_hits_text  # unused function (src/mcp_telegram/errors.py:217)

# --- formatter.py: grouped text renderers (search + unread) ---
format_search_message_groups  # unused function (src/mcp_telegram/formatter.py:340)
format_unread_messages_grouped  # unused function (src/mcp_telegram/formatter.py:610)

# --- tools/reading.py: str-returning message/search text formatters ---
_format_daemon_messages  # unused function (src/mcp_telegram/tools/reading.py:64)
_format_search_results  # unused function (src/mcp_telegram/tools/reading.py:868)
