# Canonical Dialog Snapshot: Expert Panel Decision

Status: accepted design contract

The service needs one coherent, restart-safe view of the account's dialogs.
The view is a local product projection backed by Telegram observations; it is
not a second Telegram state machine and it does not claim that one observation
keeps every field fresh forever.

## Problem and product value

Several workers currently need the same account-wide dialog catalog. Dialog
bootstrap, full reconciliation, DM enrollment, folder projection, and a
name-based selector can each walk Telegram's dialog list. Their purposes and
durable cursors differ, so mechanically joining their loops can lose progress
or turn a partial walk into a false absence. It also causes repeated pages and
inconsistent answers when consumers observe different snapshots.

The product outcome is one canonical dialog directory. It gives folder views,
DM enrollment, and local natural-name resolution a common identity and an
honest coverage state. It reduces repeated account-wide traversal, lowers
latency and FloodWait pressure, and makes restart and failure behavior
reviewable. Exact-peer operations retain their direct RPC behavior for cases
where a caller already has an exact Telegram peer.

The directory owns the complete catalog. Consumers own their local use of the
catalog, including their freshness requirement and what they materialize from
it. A consumer must never infer a complete catalog from a partial page or from
the number of objects yielded by a client wrapper.

## Domain language and contract

| Term | Contract |
| --- | --- |
| Dialog directory | The domain that acquires, normalizes, stages, and publishes the account-wide dialog catalog. |
| Dialog fact | A typed local fact about one peer, with source, observation time, and coverage status. |
| Folder rule | The rule set received from Telegram for a folder: selectors, explicit includes/excludes, and pinned peers. |
| Observation | One bounded Telegram response, with start and completion times. Reusing it never changes either time. |
| Completeness receipt | A transactional claim that the declared source selection reached an authoritative terminal condition. |
| `present` | The source authoritatively supplied the fact or membership. |
| `absent` | A complete applicable source authoritatively proved that the fact or membership is absent. |
| `unknown` | No applicable complete receipt exists, the source omitted the fact, or the observation failed or was partial. |
| Fresh | A fact, rule, or completeness receipt whose original observation age satisfies the requesting consumer's limit. |
| Stale | A receipt that exists but is older than that consumer's limit. This is an evaluation, not a stored rewrite of history. |

The tri-state is mandatory. `unknown` must not be encoded as `false`, an empty
list, or a successful empty catalog. An empty list is authoritative only when
the applicable source completed and the source contract permits an empty
result to prove absence.

Every published dialog row has, at minimum, a canonical peer identity. Entity
classification, display name, placement facts, top-message identity and date,
and source-derived details are recorded when Telegram supplied them. An
unresolved entity does not let the directory infer a user type or eligibility
from `PeerUser`; those facts remain `unknown`. Read and unread facts retain
their own observation times and cannot be renewed by a later observation of
unrelated fields.

Folder rules carry a folder ID and title, category selectors, explicit included
and excluded peers, pinned peers, and applicable exclusion flags such as
muted, read, or archived. Their observation status and time are independent
from dialog facts. Telegram calls folders “dialog filters” in the API and
describes the selector and pinned-peer fields in its [dialog folders
documentation](https://core.telegram.org/api/folders).

The folder-rule source is `messages.getDialogFilters`. The adapter accepts the
`dialogFilter` and `dialogFilterChatlist` constructors, and recognizes
`dialogFilterDefault` as the default all-chats folder representation when it is
present in the source contract. It preserves each rule's source order for
`pinned_peers`; sorting pinned peers by ID would change the product order.
The adapter does not invent rules for a constructor or field Telegram omitted.
The accepted rules version is the exact successful rule observation used for a
publication; membership must not mix fields from different rule observations.
It is a staging/publication token, not a retained history of old rules.

Folder projection applies one explicit precedence, from strongest to weakest:

1. an explicit excluded peer is absent;
2. otherwise an explicit included peer or pinned peer is present;
3. otherwise category selectors and exclusion flags decide membership.

The category source is the canonical entity fact: users are classified from
the supplied contact/non-contact and bot properties, while chat and channel
entities provide the group or broadcast category. An unknown category,
`read`, or `muted` fact blocks only the rule decision that needs that fact. It
does not invalidate an explicit include, explicit pin, or an unrelated folder
membership decision. An explicit exclusion still wins when other facts are
unknown.

For `dialogFilterChatlist`, which is explicit-only, membership is determined by
the peer lists and the precedence above; it does not fall through to a missing
category selector. `dialogFilterDefault` represents the all-chats folder and
therefore uses the published catalog as its base membership, with its pinned
order kept separately.

The archive peer-folder source and a custom filter ID are separate namespaces
in the contract. The peer-folder observations for IDs 0 and 1 describe
Telegram's account placement source, including archive placement for folder 1;
custom filter IDs and their rules come from `messages.getDialogFilters`.
Equal numeric IDs must not be treated as the same source or rule. Membership
for each folder stores the folder's pinned order independently from the
unordered membership set.

## One owner of the full catalog

The Dialog Directory is the sole owner of account-wide catalog acquisition.
Only it may issue the catalog traversal and pinned-list requests, normalize
peer identity, checkpoint traversal state, and publish catalog membership.

Bootstrap and periodic reconciliation become directory operations over the same
source contract. They may have different schedules and demand priorities, but
they do not each own a competing full catalog. Folder projection consumes the
directory's facts and folder rules. DM enrollment consumes the directory
locally. Natural-name lookup consumes the published local index. No consumer
may reintroduce an account-wide fallback traversal to compensate for a stale
or never-published directory.

## Telegram source and raw pagination

The directory uses a small raw TL adapter. It must not use the number of
Telethon wrapper objects as an end-of-list signal. The ordinary catalog source
uses the equivalent of:

### Official API facts

```python
messages.GetDialogsRequest(
    offset_date=cursor.offset_date,
    offset_id=cursor.offset_id,
    offset_peer=cursor.offset_peer,
    limit=100,
    exclude_pinned=True,
    folder_id=None,
    hash=0,
)
```

The initial cursor uses the adapter's empty offset values. A continuation
uses the last committed raw cursor. Telegram documents `limit`,
`exclude_pinned`, `folder_id`, and `offset_peer` as parameters of
[`messages.getDialogs`](https://core.telegram.org/method/messages.getDialogs),
and identifies `top_message` as the message ID used for pagination.

### Dialog liveness amendment

Catalog membership completeness, optional row-fact availability, and
pagination ability are independent. Every raw dialog with a resolvable
canonical identity is retained. Missing entity-derived fields or
classification, a matched top message or date, and a reconstructible
`InputPeer` become `unknown` facts; they never block a terminal `Dialogs`
constructor, including an empty terminal response.

For a nonterminal `DialogsSlice`, the safe cursor is the last source-order row
with both a matched top-message date and a reconstructible input peer. Rows
after that candidate are staged and may repeat on retry. If no safe cursor
exists, or it equals the committed cursor, the ordinary source becomes
`incomplete` with `stalled:missing_safe_cursor` or
`stalled:non_advancing_cursor`, retains its staging and committed cursor, and
retries after 900 seconds. It does not publish partial acquisition. Unknown
constructors, unresolvable canonical identities, and contradictory identities
within one response remain blocking.

Successive observations of one canonical peer are not conflicts merely because
mutable facts differ. Later source order replaces the staged mutable bundle,
including source, top-message, matched date, and provenance; a lower
top-message ID is valid and a later missing date replaces the earlier date with
`unknown`. Pinned membership and source order remain separate from ordinary
facts, so ordinary observation cannot erase them.

The unread-sweep metadata describes the last published observation as one
receipt: `status`, `observed_count`, `completed_at`, and
`last_visible_count` are left intact while a generation starts or fails, while
`attempted_at` records the new attempt. A successful publication replaces all
four receipt fields in its publication transaction. Fresh installs have no
such receipt and therefore report unknown coverage.

### Project cursor algorithm

The following algorithm is project-owned. For each raw dialog in source order,
first resolve its canonical peer identity and look up a response message by the
pair `(peer, top_message)`. All identifiable rows are retained. For a
nonterminal page, the last row with a matched message date and reconstructible
`offset_peer` supplies the next cursor. The pair key prevents a message ID
belonging to another peer from being used. The selected cursor is compared with
the prior cursor before it is committed.

The adapter has these explicit edge rules:

- missing entity, classification, matched `(peer, top_message)` message, date,
  or reconstructible peer facts leave that row `unknown` and skip it as a
  cursor candidate; they do not prevent terminal completion;
- `DialogFolder` is a folder marker, not a dialog row. It is skipped for facts
  and cursor selection, does not count as EOF, and a page containing only such
  markers is stalled because it has no safe cursor candidate;
- an exact duplicate of a canonical peer ID with the same identity and
  `top_message` is skipped after the first occurrence; conflicting identity,
  type, or top-message data is invalid and cannot advance the cursor;
- equal dates are allowed when the message ID and peer make the cursor tuple
  distinct. An exact `(date, top_message, offset_peer)` collision with the
  prior cursor is a non-advancing stalled outcome, never an EOF signal.

Valid facts from a stalled page are staged for retry, but only a transaction
containing a safe next cursor can advance the source. These rules make every
skip explicit and prevent a missing raw object or tie from silently losing the
remainder of the catalog.

Pinned dialogs are acquired as a separate source, once for `folder_id=0` and
once for `folder_id=1`, through `messages.getPinnedDialogs`. They are not
silently recovered by changing the ordinary traversal's `exclude_pinned`
behavior. A pinned-source failure leaves pinned membership `unknown` and
cannot produce a complete directory receipt. The endpoint and its
`folder_id` parameter are defined in the official
[`messages.getPinnedDialogs`](https://core.telegram.org/method/messages.getPinnedDialogs)
documentation.

For a raw `messages.DialogsSlice`, the adapter continues until it receives an
empty raw `dialogs` page or a terminal `messages.Dialogs` result. The wrapper's
page length is not EOF. A terminal result needs no cursor and is authoritative
even when optional row facts are unknown or a hypothetical cursor would repeat.
The adapter must preserve the exact identity needed to reconstruct the next raw
request; it must not invent a peer or silently drop an unresolved canonical
identity.

The following outcomes are never complete:

- a nonterminal next cursor repeats the prior cursor or otherwise does not
  advance;
- a raw dialog's identity cannot be resolved to the canonical peer identity;
- a response is `messages.DialogsNotModified` and there is no cached response
  that can be used under this acquisition contract;
- transport, admission, FloodWait, validation, or persistence failure stops
  the source before its terminal condition.

Such an attempt is recorded as `incomplete` or `invalid` with the relevant
reason. It may retain useful facts, but it cannot authorize hiding a dialog or
claiming catalog completeness.

Each raw page and its next cursor are stored in one local transaction. A crash
before commit leaves the prior cursor and prior staged facts in force; a
successful commit makes both visible together. Publication is a separate short
transaction after every required source has a complete receipt.

## Minimal staging and atomic publication

Staging is deliberately small: one in-progress snapshot, its source status and
cursor, and the facts needed for the next publication. It is not a historical
event log. Publication replaces the current catalog and folder-membership
projection atomically with the complete staged result, while preserving any
newer realtime facts under the revision rules below.

There are no historical snapshot generations, feature flags, or publication
buses. A current generation marker is allowed for fencing and restart
diagnostics, but prior generations and their rows are discarded after
publication. Local callers observe either the prior published catalog or the
new complete publication, never a half-published mixture.

If staging fails, the prior publication remains usable and the new source is
reported partial, invalid, or unavailable. A complete receipt is created only
in the same transaction as the facts and membership it authorizes.

## Coexistence with realtime `dialogs.revision`

Realtime dialog events remain an active writer of the canonical dialog facts.
The snapshot generation does not replace `dialogs.revision` and does not make
an older snapshot authoritative over a newer event.

The baseline revision is read and fixed before the corresponding acquisition
starts, including before an ordinary page, a pinned source, or a rule source
is requested. For every candidate that could be absent after the walk, staging
also retains its canonical identity and the baseline revision observed for
that candidate. For a peer not yet present locally, the candidate carries an
explicit no-row marker that is rechecked at publication.

Publication has a required order. First, it merges staged positive facts with
the current canonical rows, applying snapshot fields only when their baseline
revision still matches and otherwise preserving the newer realtime facts.
Second, it rechecks absence candidates against the current row and revision;
an event-created row or a changed revision prevents the staged absence from
being applied. Third, it computes folder membership from those final merged
facts and the accepted rules version. Membership is never computed from stale
staging rows before this merge.

This order handles an event during RPC, a previously unseen row created while
the walk is running, and archive/read/mute changes before publication. It also
ensures that an older snapshot cannot hide a dialog or replay a fact that would
revive edited, deleted, or superseded state. Realtime updates continue to
advance the row revision after publication.

## Migration and resume: current generation 1

Migration initializes the directory's current lifecycle marker to
`max(1, legacy generation)` and preserves the existing published catalog,
pending status, valid cursor, folder facts, DM enrollment, and read cursors.
An in-progress operation resumes from its committed page and cursor; it is not
reset to an empty catalog.

The migration does not manufacture a completeness receipt, observation time,
or freshness claim for old rows. Existing rows remain usable with their actual
provenance, while the new directory status reports the coverage that has and
has not been proven. If a legacy cursor cannot be shown to represent the raw
cursor contract, the attempt is incomplete or invalid and requires the
directory's explicit recovery path; it must not be silently marked complete or
used to hide rows. No migration step clears catalog rows, enrollment, or read
cursors merely to make the new worker start.

Subsequent publications may advance the current lifecycle marker, but only the
current staging and current publication are retained. The marker is a fence,
not a history store.

## Separate completeness, fact freshness, and folder-rule freshness

These three dimensions are evaluated independently:

| Dimension | What it proves | What it cannot prove |
| --- | --- | --- |
| Catalog completeness | The required ordinary, pinned, and rule sources reached their declared terminal conditions for one publication. | That every field in every row is fresh now. |
| Dialog-fact freshness | A particular identity, name, placement, top-message, or read/unread fact was observed within the consumer's age limit. | That a missing dialog is absent, or that folder rules are current. |
| Folder-rule freshness | The rules used to compute folder membership were observed within the consumer's age limit. | That the directory traversal or each dialog fact is complete and fresh. |

A consumer declares which dimensions it needs. A complete but old catalog is
complete and stale; a fresh row does not make an incomplete catalog complete;
fresh rules do not refresh the facts to which they apply. The service reports
`unknown` whenever the required dimension has no applicable receipt.

For each fact, observation age starts at the beginning of the acquisition that
produced that fact. Publication time, page completion time, a later local
merge, and a restart do not move that boundary forward. A projection's age is
the age of its oldest required input; equivalently, it is evaluated from the
earliest acquisition start among all required facts and rules. A long-running
RPC may therefore produce a usable result that is already stale for a strict
consumer when it is published.

The strictest current consumer is the default-folder view, whose target
freshness is 900 seconds. The directory's normal background schedule must make
the required catalog facts and folder rules satisfy that target when capacity
allows. This is a consumer requirement, not a promise that every Telegram fact
has a universal 900-second TTL. The boundary is explicit: an age just below
900 seconds is fresh, age exactly 900 seconds is stale, and an age above 900
seconds is stale. Focused tests cover all three values, including a long RPC
that crosses the boundary and a restart that preserves the original age. A
future consumer may demand a shorter limit.

## DM enrollment as local consumption

DM enrollment reads the published directory locally and no longer performs its
own account-wide dialog traversal. It enrolls only rows whose identity and
private-dialog eligibility are known. A partial or stale directory may still
provide a positive fact for an individual known row, but it cannot be used to
claim that all eligible DMs were discovered.

Before an eligible DM is enrolled, the enrollment transaction captures the
published Telegram read-in and read-out cursors for that dialog and installs
those pre-enrollment cursors in the local sync state. Only after those cursors
are durable may it queue historical synchronization. This keeps the boundary
between already-read history and newly enrolled history explicit and makes a
restart idempotent. A missing read cursor is `unknown`; it must not be replaced
with an invented zero or a current-time value.

## Local natural-name resolver and exact peers

Natural-name resolution is local-only. It searches the published directory's
canonical names and normalized local lookup keys, including the existing
transliteration behavior. A unique local match can be returned with its
coverage metadata; ambiguous matches remain ambiguous.

When the directory is stale, the resolver may return a local candidate only
while saying that catalog coverage is stale. It must not present the result as
a current account-wide search. When the directory has never published, an
empty result means `unknown` coverage, not “Telegram has no such dialog”. The
resolver must not issue a hidden account-wide RPC to turn either state into a
false negative or an untracked new traversal.

Exact-peer RPCs remain available. A caller with a canonical dialog ID or exact
Telegram peer may continue to request the exact entity or other exact facts;
that operation has its own selection, freshness, and provenance contract and
does not become a natural-name catalog fallback.

## Telemetry boundary

Do not add overlap-specific telemetry yet. Existing aggregate demand and RPC
telemetry remains unchanged until a testable, disputed overlap is proven at
the exact endpoint and selection-identity level. A shared returned row or a
similar source label is evidence of materialization overlap, not evidence that
two RPCs were redundant.

If a later investigation proves a disputed overlap, define its event, source
identity, denominator, privacy boundary, and acceptance test before adding
fields. Telegram IDs, names, content, raw peers, stable fingerprints, and
behavioral identifiers are not telemetry payloads.

## Phased implementation

Each phase is independently safe to finish and verify. A phase may leave the
next consumer on its existing path, but it must not leave a partially writable
catalog or a migration that requires clearing state.

1. **State and raw adapter.** `sync_db.py` owns the generation-1 migration and
   current staging state; `dialog_sync.py` owns the directory state machine;
   the new raw-TL adapter module owns `GetDialogs` and `GetPinnedDialogs`
   decoding and cursor construction. Result: generation 1 resumes existing
   state, ordinary and pinned sources have explicit outcomes, and page/cursor
   commits are transactional. Focused verification covers migration without
   reset, exact request arguments, raw `DialogsSlice`/`Dialogs`/`NotModified`
   handling, matched-message cursor selection, duplicate/tie rules, and
   missing entity/message cases.
2. **Staging, publication, and realtime fencing.** `dialog_sync.py` and
   `sync_db.py` implement minimal staging and atomic publication;
   `event_handlers.py` remains the realtime writer protected by
   `dialogs.revision`. Result: publication first merges facts with revision
   fencing, then computes membership from final facts and the accepted rules
   version, while failed attempts leave the prior publication usable. Focused
   verification covers crash between page and cursor, event-during-RPC,
   unseen-row-changed, archive/read/mute-before-publication, stale-writer, and
   atomic publication scenarios.
3. **Consumer cutovers.** `folders/telegram_adapter.py` and
   `folders/worker.py` consume the directory's `GetDialogFilters` rules;
   `sync_worker.py` moves DM enrollment to local rows with pre-enrollment read
   cursors; `daemon_api.py` and the local resolver consume the published name
   index while exact-peer paths remain remote-capable. Result: no consumer
   performs a second account-wide traversal, and each response exposes its
   completeness, fact freshness, and rule freshness. Focused verification
   covers folder precedence and pinned order, unknown category/read/mute,
   archive peer-folder versus custom filter ID, stale/never-published name
   lookup, DM cursor ordering, and exact-peer behavior.

The release gate after phase 3 runs the focused suite, rebuilds the daemon,
checks health after restart, and exercises one live published-directory read.
These are delivery phases, not runtime feature flags. Each phase preserves the
previous published state until the next complete publication is ready.

## Acceptance Criteria

The following matrix is the minimum semantic acceptance set for folder
projection, revision fencing, raw pagination, and freshness:

| Scenario | Required result |
| --- | --- |
| Explicit exclude and explicit include/pin both match | Exclude wins; membership is absent. |
| Explicit include or pin matches, category or exclusion flags do not | Membership is present without consulting the weaker rule. |
| No explicit peer rule; category and all required flags are known | Membership follows category selectors and exclusion flags. |
| Category is unknown but explicit include/pin matches | Membership is present; only the category-based decision is unknown. |
| `read` or `muted` is unknown and the rule excludes by that flag | That folder decision is unknown; explicit peer decisions and unrelated folders remain evaluable. |
| `folder_id=1` archive peer-folder fact and custom filter with the same numeric ID | They remain separate sources and memberships; no namespace collision is inferred. |
| Pinned peers are returned in a non-ID order | Published pinned order exactly follows the source order for that folder. |
| `dialogFilter`, `dialogFilterChatlist`, and `dialogFilterDefault` are returned | `dialogFilter` uses its category/flag rules, `dialogFilterChatlist` remains explicit-only, and `dialogFilterDefault` uses all published dialogs; no omitted rule is invented. |
| Realtime event changes a row while a catalog RPC is in flight | Event facts and the newer revision survive publication. |
| A previously unseen row is created while the walk is in flight | The absence candidate is rejected and the new row remains visible. |
| Archive, read, or mute changes after baseline and before publication | Facts are merged first; membership is then computed from final facts and accepted rules version. |
| A slice has identifiable rows with missing entity or matched-message facts | Every row is staged with unknown optional facts; the slice advances from its last safe candidate when one exists. |
| A terminal response has missing optional facts or repeats a hypothetical cursor | It completes without a cursor and publishes once the required sources are complete. |
| A raw page contains `DialogFolder` markers | Markers are skipped as non-dialogs; they are not EOF, and a marker-only slice stalls as incomplete. |
| Raw IDs overlap or dates are equal | Exact identity duplicates are skipped only when equivalent; conflicting in-response identities are invalid. A non-advancing slice stalls; equal dates are accepted when peer and message ID disambiguate the tuple. |
| A nonterminal page repeats only already-staged canonical dialog IDs | Its safe cursor commits, then the owner cools down for the 900-second freshness interval without publishing partial acquisition. |
| Fact age is just below, exactly at, or above 900 seconds | Below is fresh; at and above are stale. |
| A projection has required inputs from different acquisition starts | Its age is the oldest required input age; publication and restart do not renew it. |

- Only the Dialog Directory owns account-wide ordinary and pinned catalog RPCs;
  no consumer retains a parallel full traversal.
- The ordinary adapter sends `GetDialogsRequest` with `limit=100`,
  `exclude_pinned=True`, `folder_id=None`, and `hash=0` and uses the agreed
  raw cursor fields.
- Pinned acquisition calls `GetPinnedDialogs` separately for folder IDs 0 and
  1, and a missing pinned source keeps its coverage unknown.
- Folder rules come from `GetDialogFilters`; `dialogFilter`,
  `dialogFilterChatlist`, and `dialogFilterDefault` are handled explicitly,
  with exclude > include/pin > category/flag precedence and source-ordered
  pinned peers.
- `DialogsSlice` pagination ends only at an empty raw page or terminal
  `messages.Dialogs`; wrapper count is never used as EOF.
- Repeated cursors, unresolved identities, and `DialogsNotModified` without a
  usable cache produce incomplete or invalid outcomes and never completion.
- Baseline revisions are fixed before acquisition, absence candidates retain
  their baseline revision, and publication merges facts before computing
  membership from final facts and the accepted rules version.
- Every committed page has its cursor in the same transaction. A crash cannot
  expose one without the other.
- Publication is atomic, retains the prior publication on failure, and stores
  no historical generations, feature flag, or publication-bus event.
- Realtime changes protected by `dialogs.revision` survive an older snapshot;
  incomplete enumeration cannot hide a changed or unseen dialog.
- Migration preserves `max(1, legacy generation)`, existing state, and a valid
  cursor without a reset or invented receipt.
- Completeness, dialog-fact freshness, and folder-rule freshness are separately
  represented and evaluated. Default-folder consumers use the current 900
  second target, with below/at/above boundary tests and age measured from
  acquisition start.
- DM enrollment consumes the local directory and persists pre-enrollment read
  cursors before queueing historical sync.
- Natural-name resolution is local-only and reports stale or never-published
  coverage honestly. Exact-peer RPC behavior remains available.
- No overlap telemetry is added before exact disputed overlap is demonstrated.

## Definition of Done

The contract is done when the implementation and focused source-contract tests
demonstrate all acceptance criteria, migration and restart resume from the
current generation 1 state, raw ordinary and pinned sources are live, and
publication races with realtime updates preserve the newest canonical facts.
The daemon is rebuilt from the resulting code, the live health checks pass, and
a post-restart scenario confirms that consumers see the published directory
with explicit fresh, stale, partial, and unknown coverage. The final review
also confirms that no parallel traversal, historical-generation store, feature
flag, publication bus, or premature overlap telemetry was introduced.

## Explicit BLOCK list

The following changes are blocked by this decision:

- using Telethon wrapper page length as EOF;
- treating a partial, invalid, unavailable, or pinned-incomplete source as a
  complete catalog or as proof of absence;
- letting bootstrap, reconciliation, folders, enrollment, or natural-name
  lookup own another full catalog traversal;
- replacing `unknown` with `false`, an empty list, or “not found”;
- mixing catalog completeness with field freshness or folder-rule freshness;
- publishing over a newer `dialogs.revision` or replaying stale facts through a
  writer that can revive deleted state;
- clearing existing rows, enrollment, read cursors, or pending work to perform
  the generation 1 migration;
- enrolling a DM before its pre-enrollment read cursors are durable;
- adding an account-wide remote fallback to natural-name resolution;
- removing or weakening exact-peer RPCs;
- retaining historical generations, introducing feature flags, a publication
  bus, a generic global fact cache, or durable leases for this contract;
- adding telemetry whose overlap claim has not been proven at exact endpoint
  and selection identity.

## Official Telegram references

- [messages.getDialogs](https://core.telegram.org/method/messages.getDialogs)
- [messages.getPinnedDialogs](https://core.telegram.org/method/messages.getPinnedDialogs)
- [Dialog folders](https://core.telegram.org/api/folders)
