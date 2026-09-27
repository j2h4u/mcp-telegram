# Telegram receive-only under an account stop: research decision

Status: research decision, not implementation authorization. TG19 / AC1.
Evidence date: 2026-09-28. Source baseline: `7e41f38159c75174be39f1e55ec8d1c541dcd370`.

## Decision

Preserving incoming updates while stopping application requests is a useful
product goal, but **removing the protective disconnect is not safe with the
current Telethon/Gate boundary**. Keep the owned fail-closed disconnect as the
bounded feasible behavior. Do not implement receive-only by retaining the
socket, allowing selected application methods, or disabling only high-level
producers.

The decisive limitation is below the application call boundary: Telethon can
requeue previously admitted application requests on reconnect and on
same-connection protocol errors without calling the Gate's sender adapter
again. Its reconnect probe reaches the Gate only after raw pending requests
have been requeued. The Gate owns completion Futures, not that raw queue.
Consequently, an open latch plus a live socket does not establish zero new
application payload transmissions or retransmissions.

This does **not** classify a late response to an application request already
transmitted before the latch as a new RPC. Receiving that response and retaining
its existing completion/FloodWait accounting are legitimate. Re-emitting the
application payload after the latch is the separate behavior that cannot be
authorized implicitly as a transport control.

This research makes no runtime change, opens no implementation work, and does
not authorize rearm, restart, bootstrap or reconnect. The reported healthy
runtime from the previous session is historical context, not a fresh check.
No live account, session database, Telegram RPC or MCP endpoint was queried.

## Evidence and architecture boundary

The source and installed dependency are the executable canon. The lock selects
Telethon 1.44.0 ([uv.lock](../../uv.lock#L1446)); the project allows
`telethon>=1.44.0` ([pyproject.toml](../../pyproject.toml#L10)). Vendor references
below are to the inspected installation under
`.venv/lib/python3.14/site-packages/telethon/`, abbreviated `V/`. Online Telethon
`stable` documentation currently identifies itself as 1.45.0; it is useful
background, not evidence for the exact installed lifecycle.

This decision follows the existing [domain ports roadmap](telegram-domain-ports-roadmap.md)
and [demand-control ownership](telegram-demand-control.md): domain adapters own
facts and durable progress, the daemon owns the client and local writer,
Telethon owns its protocol machinery, and one `TelegramRpcGate` owns application
admission and account protection. The latter document's descriptions are design
context; the source references below determine current behavior. No second
client, second gate, generic event broker or protocol fork is proposed.

### Primary protocol facts

Telegram sends updates through an authorized active connection. Starting update
delivery requires connection initialization and an API call. Missing update
sequences require API recovery; ordinary push is not a substitute for that
recovery. These facts support best-effort reception on an existing connection,
not uninterrupted reception after disconnect or gaps.
([Telegram: Working with Updates](https://core.telegram.org/api/updates))

Receive-only cannot mean zero outbound bytes. MTProto requires receipt
acknowledgments, including standalone acknowledgments when there is no
application request to carry them. Protocol error handling may require resending
the original payload; if that payload is an application request, calling the
trigger a service message does not make its replay harmless under a latch.
([Telegram: Service Messages about Messages](https://core.telegram.org/mtproto/service_messages_about_messages))

Ping/Pong and deferred-disconnect ping maintain/test a connection; the latter
sets a server-side disconnect timer. These are distinct from application API
queries. Repeated connection establishment and excessive service traffic can
also trigger transport flood errors. Therefore transport controls need their
existing protocol semantics, not an arbitrary application-method allowlist or
unbounded reconnect loop.
([Telegram: Service Messages](https://core.telegram.org/mtproto/service_messages),
[Telegram: MTProto transports](https://core.telegram.org/mtproto/mtproto-transports#transport-errors))

### Current lifecycle and outbound paths

| Boundary | Verified source behavior | Consequence under a latch |
|---|---|---|
| Protective monitor | `_monitor_flood_wait_kill_switch` cancels and awaits all `ctx.background_tasks`, disconnects the client, then flushes observations: [daemon.py:987](../../src/mcp_telegram/daemon.py#L987). Composition starts the monitor, registers handlers and connects at [1834](../../src/mcp_telegram/daemon.py#L1834); demand/reconnect tasks are tracked producers at [1666](../../src/mcp_telegram/daemon.py#L1666) and [1890](../../src/mcp_telegram/daemon.py#L1890). | The current stop intentionally ends incoming transport too. Canceling producers is not itself a proof that raw transmission has stopped. |
| Gate application entry | `TelegramRpcGate.connect` checks the circuit before bootstrap at [telegram_rpc.py:609](../../src/mcp_telegram/telegram_rpc.py#L609); application dispatch checks are at [899](../../src/mcp_telegram/telegram_rpc.py#L899) and [1029](../../src/mcp_telegram/telegram_rpc.py#L1029). | No application-method exemption is needed or permitted for inbound-related work. |
| Bootstrap | `V/client/telegrambaseclient.py:605–619`, `TelegramBaseClient.connect`, initializes `GetConfig`, obtains self information where required, and starts update/keepalive tasks. `V/client/auth.py:394–396`, `_on_login`, obtains `GetState` and `GetDifference`. | A disconnected account cannot be made receiving again by a nominally read-only connect operation under the latch. |
| Difference recovery | `V/client/updates.py`, `_update_loop`, asks for account/channel differences before delivering the recovered updates. Gate's ordinary `TELETHON_UPDATE_DIFFERENCE` policy retries throttled/deferred work: [telegram_rpc.py:899](../../src/mcp_telegram/telegram_rpc.py#L899), [1029](../../src/mcp_telegram/telegram_rpc.py#L1029). | Retaining the loop does not make a gap locally solvable. The present monitor ends this activity; a hypothetical retained loop must not spin or silently accumulate an unlimited backlog. |
| Event dispatch preconditions | `V/client/updates.py:556`, `_dispatch_update`, may obtain self information and resolve event builders before calling project handlers. Event methods can fetch missing entities; [Telethon's explanation](https://docs.telethon.dev/en/stable/concepts/updates.html#properties-vs-methods) distinguishes local properties from potentially remote getters. | Even a project handler whose body is local does not prove the whole dispatch path is RPC-free. Check warm-cache and missing-cache paths separately. |
| Healthy-connection keepalive | Main sender adapter forwards raw keepalive in READY at [telegram_rpc.py:304](../../src/mcp_telegram/telegram_rpc.py#L304), [338](../../src/mcp_telegram/telegram_rpc.py#L338). `V/network/mtprotosender.py:438`, `_keepalive_ping`, uses the vendor protocol path. | A hypothetical healthy receive-only mode still transmits Ping and acknowledgments. Current disconnect ends it. |
| Raw reconnect | Gate defaults `auto_reconnect` to true at [telegram_rpc.py:429](../../src/mcp_telegram/telegram_rpc.py#L429). `V/network/mtprotosender.py:354–415`, `_reconnect`, extends the send queue with pending states at 407–411 **before** scheduling the reconnect callback. Gate's high-level reconnect probe is at [telegram_rpc.py:1120](../../src/mcp_telegram/telegram_rpc.py#L1120). | Rejecting callback `get_me()` is too late to authorize or prevent the raw requeue. Disabling auto-reconnect alone does not cover the next row. |
| Same-connection repair | `V/network/mtprotosender.py:750–798`, `_handle_bad_server_salt` / `_handle_bad_notification`, requeue request states below the adapter. At 876–883, state/resend requests generate `MsgsStateInfo`; at 603–640 RPC-result handling queues acknowledgments. | Preserve the distinction between control replies and application payload replay. Do not classify every action from the receive loop as a permissible control. |
| Original dispatch ownership | [telegram_rpc.py:158](../../src/mcp_telegram/telegram_rpc.py#L158)–236 owns the scalar raw Future and admission lifetime. `V/network/mtprotosender.py:83–89` owns `_pending_state`; its send loop at 472–499 populates it and can initiate reconnect. | Existing attempt/completion evidence is not a count or authorization of every later raw transmission. No current proof exposes a quiescent raw packer queue through the Gate. |
| Explicit teardown | [telegram_rpc.py:991](../../src/mcp_telegram/telegram_rpc.py#L991)–1018 awaits owned sender teardown and settles tracked completion Futures. `V/network/mtprotosender.py:320–346` cancels pending requests and send/receive loops; `V/client/telegrambaseclient.py:724–756` cancels callback tasks and saves session state. | Confirmed teardown is the present transport stop. An overlapping reconnect before teardown completes remains a race to test, not a demonstrated live incident. Never report failed/unconfirmed teardown as a safe disconnected state. |

## Inbound events to local persistence

The inventory boundary is every handler registered by
`EventHandlerManager.register`, [event_handlers.py:691](../../src/mcp_telegram/event_handlers.py#L691)–760,
plus the vendor preconditions above. “Local” below describes application work
after vendor dispatch has reached the handler. A durable offer is local, but its
later acquisition is outbound work and stays blocked by the same Gate.

All 15 registrations use the existing `REALTIME_EVENT_ACQUISITION` root; a root
is attribution, not evidence that a request was sent. Conditional remote entity
resolution uses the cache-first adapter at
[messages/telegram_adapter.py:407](../../src/mcp_telegram/messages/telegram_adapter.py#L407)–450.

| Registered input → handler | Local fact / commit boundary | Outbound dependencies, including later work |
|---|---|---|
| `NewMessage` → `on_new_message` | Coverage/enrollment checks, message and FTS writes in a local transaction: [1016](../../src/mcp_telegram/event_handlers.py#L1016)–1073. | Forwarded-peer `get_entity` may run **before** commit. Private-DM auto-enrollment can need `get_sender`/`get_chat`: [1080](../../src/mcp_telegram/event_handlers.py#L1080)–1114, and enrollment offers `FULL_SYNC_PAGE` at [978](../../src/mcp_telegram/event_handlers.py#L978). Successful ingestion offers the four acquisition kinds listed below; scheduled-origin handling also offers scheduled repair/discovery. |
| Raw `UpdateNewMessage`, `UpdateNewChannelMessage` → `on_raw_topic_message` | Topic create/edit metadata transaction: [1143](../../src/mcp_telegram/event_handlers.py#L1143)–1243. | No direct RPC or durable offer in this path. The separate NewMessage registration still has the dependencies above. |
| `MessageEdited` → `on_message_edited` | Existing message/version/FTS update or insert of a missing message: [1255](../../src/mcp_telegram/event_handlers.py#L1255). | Entity resolution for normalization can precede the transaction ([1325](../../src/mcp_telegram/event_handlers.py#L1325)–1354, [1390](../../src/mcp_telegram/event_handlers.py#L1390)–1424). Successful insertion/update offers the same four ingestion acquisitions; not every missing-message/forwarded-message branch is local. |
| `MessageDeleted` → `on_message_deleted` | Scoped/uniquely attributable tombstones in SQLite: [1427](../../src/mcp_telegram/event_handlers.py#L1427)–1480. | No direct Telegram request. Present peerless deletion coverage is intentionally narrower than all archive rows; this document does not broaden it or substitute remote verification. |
| `MessageRead(inbox=False)` → `on_outbox_read` | Monotonic outbox-read cursor and event timestamp transaction: [1521](../../src/mcp_telegram/event_handlers.py#L1521)–1602. | No direct RPC or durable offer. |
| `UpdateMessageReactions` → `on_raw_reaction_update` | Reaction aggregate/body-event timestamps and best-effort observations: [1605](../../src/mcp_telegram/event_handlers.py#L1605)–1729. | No direct RPC or durable offer; an observation is not a network refresh. |
| `UpdateTranscribedAudio` → `on_raw_transcribed_audio` | Local transcription update/staging: [1731](../../src/mcp_telegram/event_handlers.py#L1731)–1833. | No direct RPC or durable offer in this callback; receipt of transcription is not permission to request transcription. |
| `UpdateNewScheduledMessage` → `on_raw_new_scheduled_message` | Scheduled-message upsert: [1483](../../src/mcp_telegram/event_handlers.py#L1483). | Local offers of `SCHEDULED_REPAIR` and `SCHEDULED_DISCOVERY`; later durable acquisition is under `SCHEDULED_MESSAGES`, [registry:793](../../src/mcp_telegram/telegram_rpc_consumers.py#L793)–805. No callback RPC. |
| `UpdateDeleteScheduledMessages` → `on_raw_delete_scheduled_messages` | Local scheduled-message removal: [1506](../../src/mcp_telegram/event_handlers.py#L1506). | Same scheduled repair/discovery dependency; offers do not authorize execution under the latch. |
| `UpdateDialogPinned`, `UpdatePinnedDialogs`, `UpdateDialogUnreadMark` → `on_raw_dialog_pinned` | Local pin/full-pin rewrite or exact unread-mark mutation: [1937](../../src/mcp_telegram/event_handlers.py#L1937)–2036, [2122](../../src/mcp_telegram/event_handlers.py#L2122). | Unread mutation additionally offers `DIALOG_LIGHT_RECONCILIATION` (`DIALOG_SYNC`, [registry:739](../../src/mcp_telegram/telegram_rpc_consumers.py#L739)). No direct callback RPC. |
| `UpdateChannel`, `UpdateChat` → `on_raw_channel_chat_update` | Marks local dialog refresh state: [2197](../../src/mcp_telegram/event_handlers.py#L2197)–2231. | Offers `DIALOG_LIGHT_RECONCILIATION` and, conditionally, `LINKED_CHAT_REFRESH`. Later linked-chat adapter calls its fact acquisition owner at [2253](../../src/mcp_telegram/event_handlers.py#L2253)–2318; neither offer is an RPC exemption. |
| `UpdateUserName`, `UpdateNotifySettings` → `on_raw_identity_or_notify` | Identity, eligibility, notification and folder-rule projections: [2051](../../src/mcp_telegram/event_handlers.py#L2051)–2119. | No direct RPC or durable offer. |
| `UpdateChannelParticipant`, `UpdateChatParticipant` → `on_raw_participant` | Self-membership access-loss evidence transaction: [2169](../../src/mcp_telegram/event_handlers.py#L2169). | `_resolve_self_id` may call `get_me` when self identity is absent: [794](../../src/mcp_telegram/event_handlers.py#L794). After a changed access-loss fact, offers `DELTA_ACCESS_PROBE` at [2189](../../src/mcp_telegram/event_handlers.py#L2189). |
| `UpdateReadHistoryInbox`, `UpdateReadChannelInbox` → `on_raw_inbox_read` | Observation plus local inbox cursor/unread facts: [2387](../../src/mcp_telegram/event_handlers.py#L2387)–2473. | No direct RPC or durable offer. |
| `UpdatePinnedForumTopic`, `UpdatePinnedForumTopics` → `on_raw_forum_topic_pinned` | Local topic pin metadata transactions: [2476](../../src/mcp_telegram/event_handlers.py#L2476)–2553. | No direct RPC or durable offer. |

`_offer_message_ingestion`, [event_handlers.py:679](../../src/mcp_telegram/event_handlers.py#L679)–685,
offers `LIVE_HYDRATION_BATCH`, `BACKFILL_HYDRATION_BATCH`,
`MESSAGE_FACT_REFRESH` and `READ_RECEIPT_BATCH`. Those are deferred outbound
dependencies of both NewMessage and edit ingestion, not additional synchronous
RPCs needed to commit every incoming message. Successful base-message storage
does not imply these optional facts are fresh.

Deferred rows deliberately name the owning demand rather than invent a fixed
remote method selected by the event. Execution belongs to the existing domain
adapter and Gate; a local invalidation/offer can outlive the callback without
making its eventual network work part of receive-only. Incoming event families
not registered above are not claimed as persisted or complete by this map.

## Update cursors, cancellation and durability

Telethon and `sync.db` do not share a transaction. `MessageBox.process_updates`
in `V/_updates/messagebox.py:405–505` processes pts/seq and detects gaps before
dispatch. Account/channel difference application updates MessageBox state before
preprocessing and callback enqueueing (`V/client/updates.py:360–366,472–478`).
`_preprocess_updates` at 507–514 extends the entity cache and calls
`session.process_entities`; non-sequential callback tasks are created/tracked
separately at 275–292. A project's transaction runs only after that dispatch.

`_save_states_and_entities` copies MessageBox state into the session at
`V/client/telegrambaseclient.py:702–716`. Cache pressure and each keepalive cycle
can save it (`V/client/updates.py:295–302,516–554`); explicit disconnect cancels
and awaits callback tasks and then saves state
(`V/client/telegrambaseclient.py:724–756`). There is no per-update transaction or
commit acknowledgment coupling these saves to the domain callback's transaction.

A completed `sync.db` transaction remains durable. A received packet, protocol
acknowledgment, advanced vendor update cursor, scheduled callback, or successful
entity-cache write does **not** establish that the corresponding domain fact was
committed. Cancellation before a handler's transaction can leave no local fact;
cancellation after commit cannot undo that committed fact. Handler failures and
the vendor cursor may diverge, so later reconnect is not a guarantee that every
uncommitted callback will be replayed.

There is no demonstrated durable raw-update ingestion log or lossless callback
drain. The monitor flushes operational observations; that flush does not drain
message callbacks. Waiting indefinitely for callbacks before stopping transport
would keep replay-capable outbound machinery alive and is not a safe substitute.
Do not introduce a spool merely to describe this limitation, and do not rewrite
vendor sequence handling or rewind session cursors as an improvised recovery.

Any future change must name its actual guarantee: preserve already committed
facts; allow only locally executable work under a proven transport boundary;
report gaps/uncommitted coverage honestly. Exactly-once or lossless ingestion is
not the guarantee of this decision. Authorized future catch-up may repair some
coverage, but it consumes API capacity and does not follow automatically from a
process restart.

## Product states and their limits

These are observable meanings, not a proposed new state-machine framework.
Account admission, transport connectivity and domain freshness are separate
facts; a healthy socket alone is not healthy sync.

| State | What can be true | What must not be promised |
|---|---|---|
| Normal healthy sync | Authorized transport and Gate available; local processing and admitted acquisition run. | A past health report is not current verification. |
| Healthy receive-only — desired, not currently established | Existing authorized connection remains usable, all application emissions including replays are fenced, protocol controls continue, and locally complete updates may commit. | Continuous real-time coverage, zero outbound bytes, or recovery after disconnect. Current raw sender ownership does not meet this entry condition. |
| Gapped | Transport may still exist, but vendor sequence recovery needs account/channel difference API work. Previously dispatched local callbacks may still finish. | Skipping ordering checks to deliver raw updates as complete history, allowing GetDifference under the latch, or claiming all later push events reach handlers. |
| Degraded local ingestion | A dispatch or domain handler lacks required self/entity facts, or a local write fails; some independent complete facts may still commit. | Treating a failed enrichment/identity dependency as confirmed absence or marking its cursor/coverage complete. |
| Disconnected/protectively stopped | No new push reception; committed local data remains available in principle. Confirmed teardown is distinct from failed/uncertain teardown. | Autonomous reconnect/rearm, current Telegram freshness, or recovery of memory-only callbacks without evidence. |

Under the current implementation the latch heads toward the final row, not the
receive-only row. In a hypothetical receive-only implementation a gap or
connection failure ends the claim of healthy receive-only. Conservative stop is
acceptable; hidden bootstrap or protocol-gap API exemptions are not.

## Alternatives and recommendation

| Alternative | Benefit | Cost / decision |
|---|---|---|
| Retain the owned protective disconnect | Uses the existing supported lifecycle and stops vendor transport machinery; preserves account priority. | Incoming updates stop and memory-only work can be lost. **Recommended feasible behavior now**, with honest connectivity/freshness reporting. |
| Remove disconnect; stop producers and reject high-level calls | Appears small and retains some pushes. | Fails the raw replay boundary; vendor dispatch can also block on required API work. **Reject.** |
| Disable auto-reconnect and retain the socket | Avoids one replay trigger. | Same-connection repair and queued application payloads remain; not a complete receive-only contract. **Insufficient alone.** |
| Add a supported, owned final-transmission boundary | Could make conditional receive-only defensible while retaining one Gate. | Missing capability today; requires bounded evidence below. It is a prerequisite for a future decision, not approval for a vendor fork, arbitrary allowlist or new framework. |

The desired boundary is conceptually clean: transport reception/control,
application RPC acquisition, and canonical local fact application are different
responsibilities. But naming those responsibilities does not give the current
wrapper control over vendor internals. Preserve the existing safe lifecycle
until the dependency can support the actual boundary.

### Public and package-managed options actually checked

The installed constructor accepts a public `connection: Type[Connection]`,
`auto_reconnect`, `receive_updates` and retry settings
(`V/client/telegrambaseclient.py:244–271,351–353`). It creates the raw
`MTProtoSender` internally at 430–445; the inspected sources expose no
`sender_factory` or `before_send` hook for all TLRequest emissions.

The public connection injection is real, but is not the required selective
request boundary. The sender packs and encrypts a batch before calling
`Connection.send(data)` (`V/network/mtprotosender.py:452–501`); that method queues
opaque packet bytes (`V/network/connection/connection.py:290–300`). A custom
connection can provide transport behavior, but cannot decide which enclosed
payload is a Ping, acknowledgment or application replay without taking over
protocol/private state. Rejecting all such bytes also rejects necessary
acknowledgments; it is not a sustained receive-only solution. A public enqueue
method alone additionally says nothing about bytes already queued below it.

`auto_reconnect=False` removes one trigger, not same-connection replay.
`request_retries=0` concerns high-level request retry, not the raw sender's repair
queue. `receive_updates=False` removes the desired input; `sequential_updates`
changes callback scheduling, not outbound authorization or transaction coupling.
Custom session storage likewise changes persistence, not raw emission ownership.
The existing `disconnect()` remains the minimal public lifecycle operation that
ends transport rather than trying to classify encrypted packets.

The official [current client API](https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.telegramclient.TelegramClient)
and [connection API](https://docs.telethon.dev/en/stable/modules/network.html)
were checked as a bounded package-upgrade alternative. Their 1.45 documentation
does not establish the required request-aware emission hook. No upgraded wheel
was installed or tested. Thus an ordinary package update is **not an evidenced
solution**, rather than being declared impossible for every future version.
Changing client libraries or patching/forking Telethon would be a much broader
ownership migration and is not the minimal next step justified here.

### Capabilities needed before a receive-only proposal can be accepted

The existing owner or a supported vendor API would need all of the following:

1. An atomic stop/authorization point for **every application payload emission**,
   including queued initial sends, same-connection repairs and reconnect replay.
   A hook only at `client(request)` or the public `sender.send()` entry is
   insufficient. It must preserve ownership of already transmitted requests and
   their eventual responses without giving permission to retransmit them.
2. A reliable way to establish and maintain raw queued/pending transmission
   quiescence or rejection under the latch. An empty set of high-level Futures
   is not an established substitute. Do not cancel Futures and infer that the
   underlying request can no longer be transmitted.
3. Latch-aware control of raw reconnect before it requeues or emits anything,
   while preserving normal Ping/ack handling on the original connection. A
   reconnect callback after the requeue cannot provide this boundary.
4. A bounded way to stop at gaps/required remote identity without bypassing
   vendor ordering, leaking unlimited queued updates, or retrying expired
   protocol scopes forever. Complete local application still uses existing
   domain writers; new shared facts are not manufactured by transport code.

No such complete supported capability was established in the inspected version.
If satisfying these conditions requires copying vendor update/sender loops,
mutating private packer state, or adding a durable event platform, the minimal
decision remains disconnect. The dependency version must be revalidated before
any later implementation; mutable online stable documentation cannot substitute
for that check.

## Critical recommendations for the final-plan synthesis

**Do not confuse a correct safety fallback with a completed product recovery.**
Keeping disconnect is warranted by the actual replay paths, but it leaves the
operator's inbound-availability complaint unresolved. The product promise must
say so. Conversely, a connected indicator or several passing local-handler tests
cannot establish a safe receive-only mode. Both overclaims would hide the real
decision.

**The strongest finding is source-grounded, not a measured incident.** The raw
requeue ordering and lack of a supported selective hook are verified. Whether
the current monitor ever allows a post-latch emission before teardown has not
been reproduced here. The final plan must not re-label previous late responses
or admissions as observed extra wire calls. Its first technical validation, if
authorized, should be a small installed-vendor/fake-transport test of pending
reconnect and bad-salt replay with the latch held. This tests the deciding
boundary before any production refactor.

**Do not make a final implementation plan conditional on an undefined hook.**
The listed capabilities are a go/no-go requirement, not a design placeholder a
worker must fill by touching vendor internals. The public options checked above
do not satisfy them. A later supported upstream/package capability could change
the decision, but only an exact API/version and bounded offline evidence would
justify that branch. Without it, retain disconnect; do not schedule a receive-only
implementation disguised as a small monitor edit.

**Local ingestion independence is worthwhile but is not transport independence.**
New/edit enrichment, self identity and auto-enrollment currently mix local facts
with acquisition. A future narrowly scoped change could commit complete facts
from existing payload/cache before optional enrichment, retaining the same domain
owners and freshness semantics. It must not invent required identity, skip gap
ordering, or claim that reducing handler RPCs solves raw replay. This is a
separate possible product improvement, not required implementation to accept
TG19 or permission to start another ticket.

**Keep durability claims smaller than the evidence.** Draining callback tasks
does not by itself align Telethon's cursor with `sync.db`, and waiting for them
before stopping replay-capable transport can worsen the protection interval.
Decide explicitly whether the product needs only already-committed facts or a
new lossless-ingestion guarantee. The latter would require separate scope and
evidence; it must not silently introduce a spool, cursor rewrite or distributed
workflow into this incident response.

For the Sol panel, the concrete synthesis questions are: accept confirmed
disconnect as today's safety boundary; agree on truthful disconnected/gapped/
degraded coverage; decide whether to authorize only the bounded wire-level
validation above; and require a named supported capability before considering
retained-transport implementation. Local-service availability and DM historical
recovery remain their existing independent decisions. Panel ideas are inputs,
not automatic implementation scope, account rearm or new work items.

## Bounded offline validation and proposed implementation acceptance

These are recommendations for a separately authorized change, not completed
tests or new tickets. Use the locked Telethon classes, an in-memory session,
fake connection/sender transport and disposable local SQLite. Deny actual
network connection in the harness. Count application payload emissions at the
raw transport boundary as well as Gate admissions; observing only Gate calls
would miss the deciding failure mode.

| Scenario | Required observable result |
|---|---|
| Cached, complete NewMessage and deletion updates | Exercise installed vendor dispatch through actual project handlers; verify the correct local rows/FTS/tombstones, or truthful unsupported/degraded behavior. A hypothetical receive-only success requires no application emission; protocol controls are separately identified. |
| Missing self, forwarded entity, DM auto-enrollment or required builder resolution | No application call escapes the latch. Missing required facts do not become false completed ingestion. Optional enrichment may be deferred only through an existing valid domain contract. |
| Account/channel gap and UpdatesTooLong | No GetDifference/GetChannelDifference under latch, no hot retry or unbounded queue growth, no forced sequence advancement. Safe disconnected/gapped state is an acceptable outcome. |
| Latch while an original request is queued or pending | Distinguish already-transmitted request completion from new emission. Late success/FloodWait retains the existing completion accounting; no newly queued or replayed application payload is authorized. Verify at raw send, not merely `adapter.send`. |
| IO drop/reconnect and bad-salt/bad-notification repair | Use the installed raw sender paths, including pending-state requeue before callback. The proposed boundary must prevent application replay and reconnect bootstrap. If it cannot, retain disconnect; a skipped test is not approval. |
| Ping, acknowledgments and control-induced repair | Identify the actual wire constructor/action, not only its initiating function. Pure necessary controls may continue only in a proven healthy receive-only mode; a control that causes application replay cannot bypass the application stop. |
| Cancellation before/after local commit and vendor cursor save | Committed facts survive, incomplete work is not marked complete, and neither session pts nor telemetry is presented as an application commit receipt. Check both callback cancellation and interrupted local writes. |
| Protective teardown concurrent with reconnect | Confirm termination before reporting disconnected or releasing transport ownership. A failed/uncertain teardown stays fail-closed. Measure the existing monitor ordering; do not infer a live race incident from source alone. |

Existing tests are starting evidence, not proof of receive-only:
[monitor cancellation/disconnect/flush](../../tests/test_daemon.py#L660),
[FloodWait threshold](../../tests/test_flood.py#L122), and
[update-difference retry](../../tests/test_telegram_rpc.py#L3591).
The source inventory found no existing test that combines installed update
delivery, an open latch, a retained connection, and zero raw application replay.

Implementation DoD, if later authorized: the selected boundary passes the above
focused scenarios; unchanged Gate threshold/cooldown/deadline/budget protections
and single ownership remain covered; operational state distinguishes transport,
admission and local freshness; pending callbacks are described truthfully; the
final candidate passes the repository's required quality gates. A source/test
result does not authorize account activation. No live check is required to
accept this research document.

## Scope dependencies and research acceptance

TG15 concerns local daemon/MCP availability and feedback while Telegram is
stopped. Those local services can in principle remain useful with the Telegram
transport disconnected, but their API guards and ownership must be decided in
that separate scope; this document does not remove a global guard.

TG17 concerns the paused DM deletion generation and historical freshness.
Receiving future deletion updates can help ongoing coverage but cannot recover
missed events, resume the blocked generation, or justify repeating its old page.
Its recovery/work-reduction decision remains separate. Neither dependency
requires a receive-only bypass to make progress.

Research DoD: one document identifies the evidence version, complete registered
inbound/dependency map, protocol controls and below-Gate replay, lifecycle and
commit limits, all requested product states, alternatives, and bounded future
acceptance. An independent acceptance reader can trace each decisive claim to
source or a primary protocol reference. Unknown runtime behavior is labeled;
there is no claim of a fresh runtime check, implemented receive-only, or lossless
delivery. No production source/tests/runtime/tracker changes accompany it.
