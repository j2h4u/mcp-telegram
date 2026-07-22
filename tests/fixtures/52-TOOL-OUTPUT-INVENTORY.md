# Phase 52 Tool Output Inventory

Canonical test-readable copy for Phase 52 Plan 01. The local GSD artifact is mirrored at `.planning/phases/52-agent-first-structured-tool-output/52-TOOL-OUTPUT-INVENTORY.md`, but `.planning/` is intentionally ignored and must not be required for tests in a clean checkout.

Inventory source command:

```bash
uv run python -c "from mcp_telegram import server; print(len(server.tool_by_name)); print('\n'.join(sorted(server.tool_by_name)))"
```

Runtime registry count: 16 tools.

Current Phase 52 completion status:
- All 16 registered tools declare `outputSchema` in the live MCP tool descriptors.
- Every successful tool path returns `structuredContent`; recoverable error paths may stay text-only.
- The table below is the Plan 52-01 pre-implementation baseline and lossless migration map. Its
  `Baseline` columns intentionally preserve the state observed before later Phase 52 plans ran.

| Tool | Title | Posture | ReadOnly | Baseline outputSchema | Baseline successful structuredContent | Baseline text-only facts | Target structured fields | Exception |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `get_dialog_stats` | Dialog Stats | secondary/helper | true | false | no | section counts for top reactions, mentions, hashtags, forwards; per-entry labels/counts; empty-section `(none)` state; not-synced and dialog-not-found actionable errors | `dialog`, `top_reactions[] {emoji,count}`, `top_mentions[] {value,count}`, `top_hashtags[] {value,count}`, `top_forwards[] {peer_id,name,count}`, `section_counts`, `limits.top_n`, `is_synced_required`, `result_count_semantics` | none |
| `get_entity_info` | Entity Info | primary | true | false | no | resolved display name; id/type/name/username; about/bot description/business/note/restriction text framed as untrusted_content; avatar history with relative age; user/bot flags, status, relationship, phone country, language, birthday, folder, business info, common chats; channel/supergroup/group counts, linked ids, slow mode, reactions, topics, invite/migration, contacts_subscribed partiality | `resolved_entity`, `profile.type`, `profile.common`, `profile.user`, `profile.bot`, `profile.channel`, `profile.supergroup`, `profile.group`, `avatar_history[]`, `membership`, `contacts_subscribed`, `framed_fields[] {field,text,untrusted_content:true}`, `restrictions[]`, `available_reactions`, `topics.has_topics`, `lookup.candidates` | none |
| `get_inbox` | Inbox | primary | true | true | yes | bootstrap_pending warning; per-chat Russian header with unread count, mentions, bot/channel labels, dialog id; read_state headers; inline_markers; media/reactions/replies/edit_date rendered through message formatter; hidden remainder marker/truncation `[и ещё N]`; scope/limit semantics | `dialogs[] {dialog_id,name,category,unread_count,unread_mentions_count,total_in_chat,is_channel,is_bot,read_state,messages[]}`, `messages[] {msg_id,sender,date,text,media,reactions,replies,edit_date,inline_markers,untrusted_content}`, `bootstrap_pending`, `coverage.complete`, `limits {scope,limit,group_size_threshold}`, `truncation.hidden_count_by_dialog[]` | none |
| `get_my_recent_activity` | Recent Activity | primary | true | false | no | scan_status (`never_run`, `in_progress`, complete with scanned_at); no-activity window; per-comment dialog label, timestamp, framed untrusted_content snippet, nav dialog_id/message_id, sync status, reactions; limits since_hours/limit | `scan_status`, `scanned_at`, `since_hours`, `limits.limit`, `comments[] {dialog_id,dialog_name,message_id,sent_at,text,untrusted_content:true,sync_status,reactions[],navigation}`, `next_navigation` or explicit absence, `result_count_semantics` | none |
| `get_sync_alerts` | Sync Alerts | secondary/helper | true | true | yes | deleted/edit/access_lost section headings with counts; deleted message ids and deleted_at; edit versions and edit_date; access_lost lost_at; empty-state since echo; limit/since semantics | `alerts[] {kind,severity,dialog_id,message_id,deleted_at,version,edit_date,access_lost_at,message,action}`, `counts {deleted,edits,access_lost,total}`, `filters.since`, `limits.limit`, `empty_state.since` | none |
| `get_sync_status` | Sync Status | secondary/helper | true | true | yes | dialog_id; status; message_count; sync_progress; total_messages; last_synced_at; last_event_at; delete_detection reliability; action | `dialog_id`, `status`, `is_syncing`, `message_count`, `sync_progress`, `total_messages`, `last_synced_at`, `last_event_at`, `delete_detection`, `action`, `progress {current,total,percent}` | none |
| `get_usage_stats` | Usage Stats | secondary/helper | true | false | no | natural-language summary: most active tools, deep scrolling max_page_depth, error_distribution, filtered query percentage, latency median/p95; no-data state; 30-day window | `window.days`, `total_calls`, `tool_distribution`, `error_distribution`, `max_page_depth`, `filter_count`, `filter_percent`, `latency {median_ms,p95_ms}`, `summary`, `empty_state` | none |
| `list_dialogs` | List Dialogs | secondary/helper | true | true | yes | type; last_message_at; unread; members/created; sync_status; sync coverage; access_lost_at; DM unread_in/unread_out; diff tokens mentions/reactions/draft_text; snapshot_age_h stale warning; bootstrap_pending/no matches; filter/exclude_archived/ignore_pinned semantics | `dialogs[] {id,name,type,last_message_at,unread_count,unread_in,unread_out,members,created,sync_status,sync_coverage_pct,access_lost_at,diff {mentions,reactions,draft_text},synced}`, `snapshot_age_h`, `bootstrap_pending`, `filters`, `count` | none |
| `list_folders` | List Folders | secondary/helper | true | n/a | yes | custom Telegram folder id and title; Archive is represented separately on dialog placement | `folders[] {id,title}` | none |
| `list_folder_messages` | List Folder Messages | primary | true | n/a | yes | local-only merged newest-first folder feed with bounded limit and explicit partial coverage | `folder_id`, `messages[] {dialog_id,message_id,sent_at,dialog_name,content}`, `count`, `partial`, `incomplete_dialog_ids`, `next_navigation:null` | none |
| `list_messages` | List Messages | primary | true | false | no | archived_warning; source; fragment_coverage header; read_state header; inline_markers; date/session breaks; sender labels; topics; forwards; replies; media; reactions; edit_date; next_navigation; no-results state; navigation/sender/topic/unread/anchor/context limits | `dialog {id,name,type,access}`, `source`, `coverage {state,fragment_coverage,sync_coverage_pct,archived_message_count,last_synced_at,last_event_at,archived_warning}`, `read_state`, `messages[] {msg_id,sent_at,sender,effective_sender_id,out,is_service,text,media,reactions,topic,forward,reply_to,edit_date,inline_markers,untrusted_content:true}`, `next_navigation`, `limits {limit,context_size}`, `filters`, `truncation`, `result_count_semantics` | none |
| `list_topics` | List Topics | secondary/helper | true | false | no | topic_id/title pairs; no_active_topics text with dialog label; dialog resolution path; filtered result semantics | `dialog {id,selector}`, `topics[] {topic_id,title,untrusted_content:true}`, `count`, `empty_state`, `filters.dialog` | none |
| `mark_dialog_for_sync` | Mark Sync | primary | false | false | no | dialog_id; action marked/unmarked; enable=true follow-up note that full history will be fetched shortly; idempotent write semantics | `dialog_id`, `enabled`, `status`, `action`, `message`, `side_effects {full_history_fetch_queued}`, `idempotent` | none |
| `search_messages` | Search Messages | primary | true | true | yes | query; global dialog prefix; date/sender/msg_id/snippet framed as untrusted_content; per-dialog read_state headers; archived_warning; next_navigation; no-hit text; limits/navigation offset | `query`, `scope {dialog_id,dialog}`, `results[] {dialog_id,dialog_name,msg_id,date,sender,snippet,untrusted_content:true}`, `read_state_per_dialog`, `coverage {archived_warning,sync_coverage_pct,last_synced_at,last_event_at}`, `next_navigation`, `limits.limit`, `truncation`, `result_count_semantics` | none |
| `submit_feedback` | Submit Feedback | primary | false | false | no | acknowledgement only; accepted severity/context/model/harness omitted from result; fire-and-forget write semantics; no tracking id by design | `accepted {message,severity,context_present,model,harness}`, `status`, `message`, `tracking_id:null`, `readback_available:false`, `side_effects {feedback_db_write:true}` | none |
| `trace_account_messages` | Account Trace | primary | false | true | yes | text summary of resolved account, evidence count, coverage_state, gaps count, first five evidence snippets framed as untrusted_content, more-count, gap_summary, next_navigation; coverage_gaps/provenance already structured | `resolved_account`, `groups[]`, `groups[].evidence[] {text,media_description,untrusted_content:true}`, `coverage`, `gaps`, `provenance`, `coverage_gaps`, `next_navigation`, `result_count_semantics`, `is_error_conditions`, `preview {shown_count,hidden_count,gap_summary}` | none |

## Lossless Signal Checklist

| Signal family | Current text source | Target structured path |
| --- | --- | --- |
| `read_state` | `formatter._render_read_state_header`, `reading._format_search_results`, `unread.format_unread_messages_grouped` | `read_state` and `read_state_per_dialog.{dialog_id}` with cursor states, unread counts, anchors, oldest unread timestamps, rendered state |
| `inline_markers` | `formatter._compute_inline_markers` appended to message lines | `messages[].inline_markers[] {kind,label,side,anchor_message_id}` |
| `next_navigation` | `list_messages`, `search_messages`, `trace_account_messages` text suffix | top-level `next_navigation` plus `navigation.kind`, `has_more`, `result_count_semantics` |
| `archived_warning` | `reading._format_archived_warning` | `coverage.archived_warning {is_archived,last_sync_date,last_synced_at,last_event_at,sync_coverage_pct,archived_message_count}` |
| `fragment_coverage` | `Coverage: fragment` header in `list_messages` | `coverage.state="fragment"`, `coverage.fragment_coverage=true`, `coverage.description` |
| `snapshot_age_h` | `list_dialogs` stale snapshot suffix | `snapshot_age_h`, `snapshot {age_h,is_stale}` |
| `bootstrap_pending` | `list_dialogs` bootstrap text; `get_inbox` warnings | `bootstrap_pending`, `coverage.bootstrap_pending_count`, `coverage.complete=false` |
| `access_lost` | `list_dialogs` access_lost_at; `get_sync_alerts` access lost section; archived warning | `dialogs[].access_lost_at`, `alerts[].kind="access_lost"`, `coverage.access_lost` |
| `media` | `formatter._describe_media` and `media_description` fallbacks | `messages[].media {description,type,attributes}`, `evidence[].media_description` |
| `reactions` | `formatter._format_reactions`, activity reaction lines, stats top reactions | `messages[].reactions[]`, `comments[].reactions[]`, `top_reactions[]` |
| `draft_text` | `list_dialogs` `draft="..."` diff token | `dialogs[].diff.draft_text` with `untrusted_content=true` |
| `topics` | topic prefixes, `list_topics`, entity `has_topics` | `topics[]`, `messages[].topic {id,title}`, `profile.supergroup.has_topics` |
| `forwards` | message forward prefix and dialog stats top forward sources | `messages[].forward {from_name,from_id}`, `top_forwards[]` |
| `replies` | message reply prefix | `messages[].reply_to {msg_id,sender,sent_at}` |
| `edit_date` | `[edited HH:mm]`, sync alert edit sections | `messages[].edit_date`, `alerts[].edit_date`, `alerts[].version` |
| `limits` | tool args and prose such as default limit/context/top_n/since_hours | `limits` object on every paged or capped tool |
| `truncation` | hidden-count markers and snippet/more evidence text | `truncation {is_truncated,shown_count,hidden_count,reason}` |
| `scan_status` | `get_my_recent_activity` header | `scan_status`, `scanned_at`, `coverage.complete` |
| `coverage_gaps` | account trace gaps, archived/fragment/bootstrap warnings | `coverage_gaps[]`, `coverage.state`, `gaps[]` |
| `untrusted_content` | `[Telegram content]` frames around messages/snippets/about fields | per-field `untrusted_content=true` plus `framing {open,close}` metadata |

## Structured Parity Notes

- Baseline rows are retained as historical evidence; current completion is enforced by registry and smoke tests, not by the baseline status columns.
- Existing baseline `outputSchema` tools were not accepted as complete; rows above preserve the gaps that later migration plans had to close.
- Error results may remain text-only per D-05. This inventory is scoped to successful tool calls.
- Exceptions are `none` for all tools in Plan 52-01; later migration plans must either fill the target fields or update this inventory with a concrete exception and justification.
