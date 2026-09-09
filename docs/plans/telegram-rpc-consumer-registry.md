# Telegram RPC Consumer Registry

## Goal

Make every application-owned reason for contacting Telegram visible in one
immutable, code-owned registry. The transport scheduler consumes only admission
classification. A later semantic audit can use the acquisition metadata to
find overlapping realtime, catch-up, reconciliation, and backfill paths.

The registry is descriptive policy, not a task scheduler. It does not introduce
runtime registration, mutable configuration, persistence, or a generic job
queue.

## Considered designs

1. Extend the existing source-to-class mapping. This is small, but mixes
   transport policy and acquisition semantics in one flat structure.
2. Use one exhaustive registry whose records compose narrow admission and
   acquisition specifications. This keeps one source of truth while giving
   each consumer a purpose-specific view. This is the selected design.
3. Register consumers dynamically from worker decorators. This hides omissions
   until imports run and makes completeness depend on runtime composition.

## Contract

Each `TelegramRpcSource` has exactly one `TelegramRpcConsumerSpec` with:

- an `AdmissionSpec`, containing the service class used by transport admission;
- an `AcquisitionSpec`, containing controlled semantic dimensions: fact
  domains, acquisition role, trigger, fan-out scope, demand-policy owner, and
  any consumer whose gaps it repairs;
- a `DemandSpec`, declaring whether demand is bounded by a request, event,
  Telethon, a producer policy, or is presently unbounded and requires review;
- a stable operator label and concise purpose.

The registry and every nested collection are immutable. The legacy
`RPC_SOURCE_SERVICE_CLASS` name remains only as a derived read-only view while
callers migrate; it is not another policy source.

`rpc_scope()` resolves its service class through the registry. Missing entries
therefore fail before any Telegram request. The existing transport-boundary AST
check continues to reject direct Telethon transport access outside the gate.

## Security and operational impact

The change adds no input, network surface, permissions, persistence, or
sensitive data. Registry strings are code-owned and contain no Telegram data.
Fail-closed behavior becomes stronger because an enum value without a registry
record cannot create a valid RPC scope.

The main risks are stale semantic declarations and accidental duplicate policy.
Controlled enums, immutable structures, exact registry coverage, derived
transport mapping, and tests that exercise every record mitigate them. An
unbounded producer is represented explicitly rather than hidden behind a
periodic loop; the first audit can then prioritize it for a concrete slice or
cadence policy.

## Implementation

1. Add a dependency-free registry module that owns the source and classification
   enums plus immutable composed specifications.
2. Import and re-export those public names from the scheduler, preserving the
   current call sites while making the registry canonical.
3. Resolve scopes from the canonical record and retain the derived compatibility
   mapping for current reporting and tests.
4. Add tests for exhaustive coverage, immutability, semantic completeness,
   derived-view parity, and fail-closed unknown sources.
5. Run focused scheduler, transport-boundary, config, and integration tests;
   then run the full suite before release.

## Rollback

This is a code-only change with no database or configuration migration. Revert
the code commit and rebuild the container.
