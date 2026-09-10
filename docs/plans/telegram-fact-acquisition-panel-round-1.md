# Telegram Fact Acquisition: Research Panel Round 1

## Status

This document preserves the findings of the first panel round as research input.
That round used Luna and Terra, so its conclusions are evidence and candidate
recommendations rather than the final expert decision.

## Problem

The demand coordinator serializes and budgets durable Telegram work by demand
kind. It does not identify a concrete fact, entity, query, or history range, so
it cannot prevent independent consumers from acquiring the same information.
SQLite primary keys remove duplicate rows only after the Telegram request has
already happened.

The product question is whether the service can use its local database as a
cache-first source when it contains sufficiently fresh and complete facts, and
whether consumers should be consolidated without weakening their distinct
freshness, completeness, and recovery promises.

## Confirmed overlap

- Hot and cold own-activity sweeps can start from the same newest per-peer page.
- Full user profile and personal-channel sections independently acquire the
  same full-user response.
- Bootstrap, reconciliation, enrollment, and folder-related work can perform
  independent dialog traversals without sharing a recent catalog snapshot.
- Repeated context requests can reacquire the same history fragment because
  persisted messages do not establish reusable range coverage.
- Reaction aggregates acquired through realtime or history ingestion do not
  provide the freshness receipt expected by the reaction refresh path.
- Global own-activity, per-peer activity, full history, delta, realtime, and
  account trace can observe the same messages through distinct acquisitions.

## Important corrections and distinctions

- Not every Telegram acquisition is persisted. Direct interactive history
  fallback can return data without creating reusable coverage metadata.
- Different consumers are often legitimate domain intentions even when they
  share a physical loader. Realtime, protocol difference recovery, delta sync,
  and full-history backfill have different correctness guarantees and should
  remain independent intentions.
- Read-position reconciliation is not proven to be a simple duplicate of dialog
  traversal. Fresh traversal data is applied locally, while independent probes
  cover dialogs that are due outside such a traversal.
- Database presence alone cannot authorize reuse. Reuse requires explicit
  freshness, coverage, completeness, and provenance semantics.

## Candidate domain model

The first round proposed domain capabilities such as Dialog Catalog, History
Mirror and Repair, Own Activity, Entity Profile, Reaction Snapshot, Read State,
Folders, and Interactive Reads. Telegram transport would sit behind an
anti-corruption boundary, while domain intentions would request facts rather
than invoke Telethon directly.

A candidate acquisition contract contains:

- a canonical fact family and subject;
- an exact normalized selection, such as message IDs or a directional range;
- maximum acceptable age;
- required completeness;
- observation time, covered selection, continuation, and provenance;
- explicit fresh, stale, partial, unavailable, and unknown outcomes;
- in-flight joining only for requests whose semantics are safely equivalent.

Freshness must be committed atomically with the corresponding projection.
FloodWait, timeout, transport failure, omitted objects, or a crash before commit
must not produce a fresh or complete receipt.

## Options carried into the expert round

1. Add measurement only and defer behavior changes.
2. Make targeted fixes for proven duplicates within existing domains.
3. Add a small cache-first acquisition registry and singleflight mechanism for
   exact scalar facts, then extend it cautiously.
4. Introduce domain acquisition services that own shared Telegram loaders while
   existing workers retain scheduling intent.
5. Restructure the repository around full DDD bounded contexts.

The first round favored a staged combination of targeted fixes, exact scalar
fact coordination, and domain acquisition services. It warned against beginning
with a repository-wide DDD rewrite or semantic range subsumption.

## Risks for expert adjudication

- False completeness can silently hide missing messages.
- A global cache policy can erase legitimate domain-specific freshness needs.
- Process-only singleflight does not provide restart correctness.
- Durable leases can add more complexity than the duplicate traffic justifies.
- Merging repair intentions can damage recovery after disconnects or crashes.
- Raw acquisition identity in telemetry can expose Telegram identifiers or
  behavioral information.
- File and module restructuring can consume effort without reducing requests.

## Measurements still needed

- Remote attempts, returned rows, newly materialized rows, and latency per fact
  family and acquisition role.
- Overlap rates for hot, cold, and global own-activity acquisition.
- Repeated full-user and full-channel acquisitions inside each TTL window.
- Reaction refreshes preceded by a realtime or history observation.
- Repeated dialog traversals within the same useful freshness window.
- Repeated history-fragment selections and their actual cacheability.
- Leader, follower, cache-hit, partial-result, lease-recovery, and remote-attempt
  counters without raw Telegram identifiers or content.

## Questions for the expert panel

1. Is a shared acquisition layer the right architectural boundary, or should
   deduplication remain domain-local?
2. Which bounded contexts are real product boundaries, and which would be
   artificial abstractions around a single writer and transport?
3. Which first slice proves product value with the smallest correctness risk?
4. When may one Telegram response refresh several projections with different
   TTL and completeness requirements?
5. Which overlaps are essential redundancy and which are removable waste?
6. What evidence is sufficient before implementation, and what should be
   learned through an observe-only production phase?
