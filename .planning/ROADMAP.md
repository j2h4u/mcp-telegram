# Roadmap: mcp-telegram

## Milestones

- ✅ **v1.0 Core API** — Phases 1-5 (shipped 2026-03-11)
- ✅ **v1.1 Observability & Completeness** — Phases 6-9 (shipped 2026-03-13)
- ✅ **v1.2 MCP Surface Research** — Phases 10-13 (shipped 2026-03-13)
- ✅ **v1.3 Medium Implementation** — Phases 14-18 (shipped 2026-03-14)
- ✅ **v1.4 Message Cache** — Phases 19-23 (shipped 2026-03-20)
- ✅ **v1.5 Persistent Sync** — Phases 24-39.4 (shipped 2026-04-22)
- 🔄 **v1.6 Local Mirror as Source of Truth** — Phases 40-46 (in progress)

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions between integers

<details>
<summary>✅ v1.5 Persistent Sync (Phases 24-39.4) — SHIPPED 2026-04-22</summary>

Full details: `.planning/milestones/v1.5-ROADMAP.md`

- Phase 24: sync.db Foundation (1/1)
- Phase 25: SyncDaemon Skeleton (1/1)
- Phase 26: FullSyncWorker (2/2)
- Phase 27: Event Handlers (2/2)
- Phase 28: DeltaSyncWorker (2/2)
- Phase 29: MCP Capability Routing (2/2)
- Phase 30: Sync MCP Tools (3/3)
- Phase 31: Deployment Wiring (3/3)
- Phase 32: Complete daemon API migration (2/2)
- Phase 33: Consolidate persistent state (2/2)
- Phase 34: Code Quality Kaizen (2/2)
- Phase 35: Daemon API Feature Parity (2/2)
- Phase 36: Sync Coverage & Access Recovery (2/2)
- Phase 37: Normalized Data Model (2/2)
- Phase 38: ListUnreadMessages Zero-API Rewrite (2/2)
- Phase 39: Entity-as-SoT Sender Rendering Fix (1/1)
- Phase 39.1: DM sender SoT (INSERTED) (3/3)
- Phase 39.2: Reactions live-sync + JIT freshen (INSERTED) (3/3)
- Phase 39.3: Bidirectional DM read-state (INSERTED) (4/4)
- Phase 39.4: Resolver silent collision fix (INSERTED) (3/3)

</details>

### v1.6 Local Mirror as Source of Truth (Phases 40-46)

- [ ] **Phase 40: dialogs Snapshot Schema** — Schema migration v12→v13, `dialogs` table + differentiator columns
- [ ] **Phase 41: Bootstrap Sweep** — Daemon-start `iter_dialogs()` sweep populates snapshot, resumable + FloodWait-tolerant
- [ ] **Phase 42: Real-time Event Handlers** — Raw event handlers keep `dialogs` and `forum_topics` current without API calls
- [ ] **Phase 43: Reconciliation Loop** — `dialog_sync.py` hourly/daily passes, dirty-flag processing, soft-delete
- [ ] **Phase 44: ListDialogs SQL Migration** — Pure-SQL `ListDialogs`, filter pushdown, snapshot-age annotation, differentiator fields in output
- [ ] **Phase 45: ListTopics SQL Migration** — `forum_topics` snapshot table, pure-SQL `ListTopics`
- [ ] **Phase 46: Tool Surface Audit** — `TOOL-SURFACE-AUDIT.md` classifying every remaining live `self._client.*` call

## Phase Details

### Phase 40: dialogs Snapshot Schema
**Goal**: sync.db schema v13 is in place with a `dialogs` table ready to receive snapshot data and differentiator fields
**Depends on**: Nothing (first phase of v1.6)
**Requirements**: MIRROR-01, MIRROR-02, MIRROR-03, MIRROR-04, MIRROR-05, DIFF-01, DIFF-02, DIFF-03
**Success Criteria** (what must be TRUE):
  1. `sync.db` migrates cleanly from v12 to v13 on daemon start with no data loss to existing tables
  2. `dialogs` table exists with all required columns: `dialog_id`, `name`, `type`, `archived`, `pinned`, `members`, `created`, `last_message_at`, `snapshot_at`, `hidden`, `needs_refresh`, `unread_mentions_count`, `unread_reactions_count`, `draft_text`
  3. `dialogs` is structurally separate from `synced_dialogs` and `entities` — no FK coupling that would block independent evolution
  4. `_dialogs_snapshot_populated()` predicate returns False on a fresh v13 schema and True after at least one row is inserted
  5. Running migration twice on the same DB is a no-op (idempotent)
**Plans**: TBD

### Phase 41: Bootstrap Sweep
**Goal**: On first daemon start after v1.6, the `dialogs` table is populated via a background `iter_dialogs()` sweep that is resumable, FloodWait-tolerant, and never blocks health checks
**Depends on**: Phase 40 (schema must exist)
**Requirements**: BOOTSTRAP-01, BOOTSTRAP-02, BOOTSTRAP-03, BOOTSTRAP-04, BOOTSTRAP-05, BOOTSTRAP-06
**Success Criteria** (what must be TRUE):
  1. Daemon `/health` endpoint responds during bootstrap — the sweep does not block the event loop
  2. If the daemon is killed mid-sweep, the next start continues from the last processed dialog (not from scratch)
  3. A simulated FloodWait during the sweep causes a sleep (not a crash or restart), and the sweep resumes after the wait
  4. Bootstrap uses `INSERT OR IGNORE` + recency guard — rows written by event handlers before bootstrap reaches that dialog are not overwritten with stale data
  5. Event handlers are registered before the bootstrap task is created (handler-first invariant holds at startup)
**Plans**: TBD

### Phase 42: Real-time Event Handlers
**Goal**: `dialogs` and `forum_topics` tables stay current via Raw event handlers — pinned state, dirty flags, last_message_at, and topic mutations all land without any Telegram API call at read time
**Depends on**: Phase 40 (tables to write to); NOTE: must be deployed before Phase 41's bootstrap runs in production to preserve handler-first invariant
**Requirements**: EVENTS-01, EVENTS-02, EVENTS-03, EVENTS-04, EVENTS-05
**Success Criteria** (what must be TRUE):
  1. Pinning/unpinning a dialog in Telegram is reflected in `dialogs.pinned` within one event cycle (no daemon restart)
  2. `dialogs.needs_refresh=1` is set when an `UpdateChannel` or `UpdateChat` event arrives — reconciliation loop can pick it up
  3. `dialogs.last_message_at` advances monotonically when new messages arrive — it never decreases from a concurrent event
  4. Forum topic creation and deletion events write to `forum_topics` table without triggering any `GetForumTopicsRequest` call
**Plans**: TBD

### Phase 43: Reconciliation Loop
**Goal**: A new `dialog_sync.py` module runs hourly light passes and daily full passes that keep the snapshot fresh, process dirty flags, and soft-delete dialogs the account has left or been kicked from
**Depends on**: Phase 40 (schema), Phase 42 (events set `needs_refresh` flags consumed here)
**Requirements**: RECON-01, RECON-02, RECON-03, RECON-04, RECON-05
**Success Criteria** (what must be TRUE):
  1. `dialog_sync.py` exists as a standalone module with its own lifecycle mirroring `delta_sync.py` — it can be started/stopped independently
  2. Hourly pass processes only dialogs with `needs_refresh=1` — it does not call `iter_dialogs()` in full
  3. Daily pass calls `iter_dialogs()` in full and sets `hidden=1` on dialogs no longer returned (left/kicked)
  4. When `synced_dialogs.status` transitions to `access_lost`, `dialogs.hidden=1` is set in the same SQLite transaction
  5. A FloodWait during reconciliation causes a sleep then resume — it does not abort the pass or raise to the caller
**Plans**: TBD

### Phase 44: ListDialogs SQL Migration
**Goal**: `ListDialogs` makes zero Telegram API calls per invocation — all filtering is pure SQL on indexed columns, with fuzzy fallback on the SQL-filtered subset and a stale-snapshot annotation when data is old
**Depends on**: Phase 40 (schema), Phase 41 (data populated), Phase 42 (data current)
**Requirements**: LISTDIALOGS-01, LISTDIALOGS-02, LISTDIALOGS-03, LISTDIALOGS-04, DIFF-04
**Success Criteria** (what must be TRUE):
  1. `ListDialogs` with any filter parameter produces correct results without any `iter_dialogs()` or Telegram RPC call
  2. Filtering by name substring uses a SQL `LIKE` / `COLLATE NOCASE` predicate — the Python fuzzy-match loop runs only over the SQL result set, not all dialogs
  3. When the snapshot is older than 12 hours, output includes a `snapshot_age=Xh` annotation; when fresh, no annotation appears
  4. Rows with non-zero `unread_mentions_count`, `unread_reactions_count`, or non-empty `draft_text` display those values inline (`mentions=N`, `reactions=N`, `draft="..."`)
**Plans**: TBD
**UI hint**: no

### Phase 45: ListTopics SQL Migration
**Goal**: `ListTopics` makes zero Telegram API calls per invocation — it reads exclusively from a `forum_topics` snapshot table kept current by event handlers and targeted reconciliation
**Depends on**: Phase 40 (`forum_topics` table), Phase 42 (forum topic events)
**Requirements**: LISTTOPICS-01, LISTTOPICS-02, LISTTOPICS-03
**Success Criteria** (what must be TRUE):
  1. `ListTopics` returns topic data without calling `GetForumTopicsRequest` at read time
  2. `forum_topics` table contains `dialog_id`, `topic_id`, `title`, `icon_emoji_id`, `pinned`, `date`, `hidden`, `snapshot_at` columns
  3. A topic title change is reflected in `forum_topics` after the next targeted reconciliation pass (not after a full daemon restart)
**Plans**: TBD

### Phase 46: Tool Surface Audit
**Goal**: `.planning/TOOL-SURFACE-AUDIT.md` gives a complete, classified map of every remaining live `self._client.*` call site in `daemon_api.py` post-v1.6, with rationale and disposition for each
**Depends on**: Phases 44 and 45 complete (so the audit reflects the post-migration state)
**Requirements**: AUDIT-01, AUDIT-02
**Success Criteria** (what must be TRUE):
  1. `TOOL-SURFACE-AUDIT.md` lists every `self._client.*` call site in `daemon_api.py` — no call site is unaccounted for
  2. Each entry is classified as exactly one of: `mirror-to-db`, `push-via-event`, or `inherently-live`
  3. Every `inherently-live` entry has a written rationale explaining why caching or event-driving is not appropriate
**Plans**: TBD

## Progress Table

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 40. dialogs Snapshot Schema | 0/? | Not started | - |
| 41. Bootstrap Sweep | 0/? | Not started | - |
| 42. Real-time Event Handlers | 0/? | Not started | - |
| 43. Reconciliation Loop | 0/? | Not started | - |
| 44. ListDialogs SQL Migration | 0/? | Not started | - |
| 45. ListTopics SQL Migration | 0/? | Not started | - |
| 46. Tool Surface Audit | 0/? | Not started | - |

## Backlog


### Phase 999.1: Track own group messages for replies and reactions (BACKLOG)

**Goal:** Surface any activity on messages the user sent in groups — direct replies, reactions, and contextual follow-ups. Currently there is no way to know if someone reacted or replied in a group without re-opening it. Telegram's built-in "Replies" chat only shows @mentions and direct replies, not reactions.

**Context:**
- User sends messages in various groups and loses track of responses
- Reactions on own messages are completely invisible unless revisiting the group
- "Contextual replies" (messages sent right after, without explicit reply) are also missed
- Telegram desktop/mobile "Replies" chat is insufficient — no reactions, incomplete coverage

**Requirements:** TBD
**Plans:** 0 plans

**Prerequisites:** Group sync must be solved first — group messages need to be in sync.db for this to work.

Plans:
- [ ] TBD (promote with /gsd-review-backlog when ready)

### Phase 999.2: Saved Messages journal for sync events (BACKLOG)

**Goal:** Send system events (access_lost, access_restored, re-enrollment, sync milestones) as Telegram messages to user's own "Saved Messages", making the MCP server self-reporting without requiring active polling of GetSyncAlerts.

**Context:**
- User has Telegram Premium → tag all journal entries with a dedicated tag (Premium allows tagging messages in Saved Messages)
- access_lost and access_restored are the primary events; sync milestones secondary
- Daemon already owns TelegramClient — `send_message("me", ...)` is trivial to add
- Archived/access_lost groups sit deep out of sight; push notification via Saved Messages surfaces them naturally on mobile

**Requirements:** TBD
**Plans:** 0 plans

Plans:
- [ ] TBD (promote with /gsd-review-backlog when ready)

### Phase 999.3: Profile change tracking for synced DM peers (BACKLOG)

**Goal:** Detect and persist profile updates (first_name/last_name, username, phone, photo_id, bio, premium/deleted flags) for every peer of a synced DM. Integrate into existing lazy reconciliation loop (delta_sync / heartbeat) — no new scheduler. Batch via GetUsersRequest (≤100 users/RPC) to respect API budget. Persist history in profile_versions table (analog of message_versions) so we capture when/what changed.
**Requirements:** TBD
**Plans:** 0 plans

**Motivation:**
- `entities.name` snapshot drifts silently; the sender-rendering fix accepted "current name wins" but drift still hurts search/UX.
- Parity with real Telegram clients that always show current profile data.
- Foundation for future "who renamed" audit, contact-book sync.

**Out of scope:** group members (unbounded cost).
**Depends on:** sender-rendering fix (entities as SoT) ships first.

Plans:
- [ ] TBD (promote with /gsd-review-backlog when ready)

### Phase 999.4: GetDialogInfo — group metadata + known-members intersection (BACKLOG)

**Goal:** Inverse of GetUserInfo's `common_chats`. When the user asks about a group, return metadata plus the subset of participants the user already has a relationship with (DM, contact, common-chat appearance, prior message-sender) — not the full roster, which is unbounded for big channels.
**Requirements:** TBD
**Plans:** 0 plans

**Motivation:**
- Today `ListDialogs` reports only `members=N` counter; nothing about who's inside.
- OSINT / situational awareness: "who that I already know is in chat X" is the 80% useful answer, full roster is rarely needed.
- Symmetric with `GetUserInfo.common_chats` ("for this user, which shared groups") — intersection is cheap once participants are cached.

**Proposed shape:**
- New table `dialog_participants(dialog_id, user_id, role, joined_at)` with `(dialog_id, user_id)` unique index and TTL-based refresh (24h default).
- Fetch via `channels.GetParticipantsRequest` / `client.iter_participants(entity)`; honor FloodWait.
- Telegram hard-caps iter for large channels without admin (~200 for megagroups of non-admins); surface as `participants_sample_size=N of M`.
- New MCP tool `GetDialogInfo(dialog, show_known_members=true, known_limit=30)`:
  - header: id, name, type, members, created, sync_status, coverage, access_lost
  - body: `people_you_know (K of N):` list of user_ids with relation tags (`DM`, `contact`, `common_chat(X)`, `message_sender(in Y)`), sorted by tag priority
- Intersection query is a LEFT JOIN against `entities` (DM/contact), `synced_dialogs`, and `messages` (distinct sender_id).

**Known-member sources (all already in sync.db, no new data needed beyond participants):**
1. DM peer: synced_dialogs row with type=User, out-flow exists
2. Contact: entity flagged as contact
3. Historical common_chat: materialized by prior GetUserInfo calls
4. Message sender in any other synced dialog: `SELECT DISTINCT sender_id FROM messages WHERE dialog_id != :target`

**Access-lost edge case:** for groups where the account was kicked (access_lost), participants API returns ChatAdminRequired / empty. Fall back to "observed known members" — distinct senders of already-synced messages in that dialog.

**Out of scope:**
- Full raw_json per participant (deferred along with the raw-blob entities decision, 2026-04-22).
- Background prefetch of participants for all synced groups — demand-driven only, per-dialog, honoring TTL.

**Depends on:** nothing blocking; can ship independently.

Plans:
- [ ] TBD (promote with /gsd-review-backlog when ready)

### Phase 999.5: Рефактор слоя данных — StoredMessage/ReadMessage + satellite dataclasses (BACKLOG)

**Goal:** Устранить structural maintenance tax: сейчас добавление любого нового поля сообщения требует синхронного изменения 6 файлов. Рефактор сводит это к одному месту.

**Requirements:** TBD
**Plans:** 0 plans

**Контекст и мотивация (2026-04-23):**

В ходе работы над forward-атрибуцией и `post_author` обнаружили, что каждое новое поле требует прохода по одному и тому же маршруту:
1. `sync_worker.py` — `INSERT_MESSAGE_SQL` + `extract_message_row()` + позиционный tuple
2. `daemon_api.py` — `_DB_MESSAGE_COLUMNS` + `_LIST_MESSAGES_BASE_SQL` SELECT
3. `tools/_adapters.py` — `DaemonMessage.__slots__` + `__init__`
4. `formatter.py` — `getattr(msg, "field", None)` + рендеринг
5. Тесты — `_make_db()` в 4 файлах + счётчик колонок

Четыре места хранят одно и то же поле без формальной связи. Позиционный tuple в ExtractedMessage.row — главная боль: порядок нигде не задокументирован кроме комментария, ошибка в порядке = тихий баг.

**Архитектурное решение:**

**Два dataclass'а для сообщений:**

```python
@dataclass
class StoredMessage:
    # только поля таблицы messages — из них генерируется INSERT_MESSAGE_SQL
    dialog_id: int
    message_id: int
    sent_at: int
    text: str | None
    sender_id: int | None
    sender_first_name: str | None
    media_description: str | None
    reply_to_msg_id: int | None
    forum_topic_id: int | None
    edit_date: int | None
    grouped_id: int | None
    reply_to_peer_id: int | None
    out: int
    is_service: int
    post_author: str | None

@dataclass
class ReadMessage:
    # StoredMessage-поля + JOIN-resolved поля + инжектируемые post-query
    # из него генерируется _DB_MESSAGE_COLUMNS
    message_id: int
    dialog_id: int
    sent_at: int
    text: str | None
    sender_id: int | None
    sender_first_name: str | None  # COALESCE(e_raw.name, e_eff.name, m.sender_first_name)
    media_description: str | None
    reply_to_msg_id: int | None
    forum_topic_id: int | None
    is_deleted: int
    deleted_at: int | None
    edit_date: int | None          # COALESCE из message_versions
    topic_title: str | None        # из topic_metadata JOIN
    effective_sender_id: int | None  # CASE-выражение
    is_service: int
    out: int
    dialog_id_: int                # дублируется для совместимости
    fwd_from_name: str | None      # из message_forwards JOIN
    post_author: str | None
    reactions_display: str = ""    # инжектируется после запроса
    dialog_name: str | None = None # только в FTS all-search пути
```

### Phase 999.6: Резолвинг имён для пересылок — fix в sync-воркере (BACKLOG)

**Goal:** Пересланные сообщения с `peer_id` без `from_name` корректно отображают имя отправителя — в новых сообщениях сразу, в исторических при следующей синхронизации.

**Requirements:** TBD
**Plans:** 0 plans

**Контекст (2026-04-24):**

Бэкфил существующих записей бессмысленен — покроет только то, что есть в БД сейчас, но не поможет с будущими форвардами. Правильный фикс: в sync-воркере при обходе истории (`iter_messages`), когда встречаем forward с `peer_id` но без `from_name` — резолвить entity через `GetUsersRequest`/`GetChannelsRequest` и класть результат в таблицу `entities`. Тогда и будущие сообщения покрываются, и исторические при следующем полном обходе.

Plans:
- [ ] TBD (promote with /gsd-review-backlog when ready)

**Satellite dataclasses (сейчас — позиционные tuple):**

```python
@dataclass
class ForwardRecord:
    dialog_id: int; message_id: int
    fwd_from_peer_id: int | None; fwd_from_name: str | None
    fwd_date: int | None; fwd_channel_post: int | None

@dataclass
class ReactionRecord:
    dialog_id: int; message_id: int; emoji: str; count: int

@dataclass
class EntityRecord:
    dialog_id: int; message_id: int
    offset: int; length: int; type: str; value: str | None

@dataclass
class ExtractedMessage:
    stored: StoredMessage          # было: row: tuple
    reactions: list[ReactionRecord]
    entities: list[EntityRecord]
    forward: ForwardRecord | None
```

**Ключевые механизмы:**

```python
# INSERT SQL генерируется автоматически из полей StoredMessage
INSERT_MESSAGE_SQL = _build_insert_sql("messages", StoredMessage, skip={"is_deleted"})

# _DB_MESSAGE_COLUMNS генерируется из ReadMessage
_DB_MESSAGE_COLUMNS = tuple(f.name for f in fields(ReadMessage))

# Coupling SELECT-порядок vs ReadMessage — устраняется sqlite3.Row:
conn.row_factory = sqlite3.Row
# row["sender_first_name"] вместо row[4] — порядок перестаёт иметь значение
```

**Что исчезает:**
- `DaemonMessage` в `tools/_adapters.py` — formatter работает с `ReadMessage` напрямую
- `MessageLike` Protocol в `models.py` — у нас нет внешних потребителей
- Ручная синхронизация `_DB_MESSAGE_COLUMNS` / `INSERT_MESSAGE_SQL` / `__slots__`
- Позиционные tuple в `extract_fwd_row`, `extract_reactions_rows`, `extract_entity_rows`

**Про ORM:** не нужен. `_build_list_messages_query()` — динамический SQL с conditional WHERE, CASE, subquery; FTS5 со snowball-стеммером. ORM с этим не справляется. `sqlite3.Row` + `dataclasses.fields()` решают задачу без зависимостей.

**Валидация при старте:**
```python
assert tuple(f.name for f in fields(ReadMessage)) == _query_column_names()
# Падает при рассинхроне, не в рантайме при первом запросе
```

**Затронутые файлы:** `sync_worker.py`, `sync_db.py`, `daemon_api.py`, `tools/_adapters.py`, `formatter.py`, `models.py`, все тесты с `_make_db`.

**Backlog context:** нет обязательств обратной совместимости кроме сохранности данных в sync.db. Схема БД (колонки) не меняется — это рефактор Python-кода вокруг неё.

Plans:
- [ ] TBD (promote with /gsd:review-backlog when ready)
