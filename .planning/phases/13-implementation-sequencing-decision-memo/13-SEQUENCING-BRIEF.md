# Phase 13 Sequencing Brief

## Purpose

This brief turns the locked Medium-path recommendation into the concrete execution order that the
next implementation milestone should follow. The ordering is chosen to remove the highest-burden
public leaks first, front-load internal seams that make a later Maximal pass cheaper, and keep
runtime validation attached to every contract-affecting step instead of treating rollout as a final
afterthought.

## Recommended Sequencing

### Rationale For The Order

The next milestone should start by cleaning the parts of the current surface that make every later
change harder to validate: boundary-level failure collapse, split continuation vocabulary, and
helper-first workflow expectations. Once those are named and reduced, the implementation can add a
capability-layer/internal-boundary seam behind the surface, reshape the primary read/search flows,
and then demote helper tools without trapping the codebase in tool-name-shaped internals.

### Must Land For Medium

- `error-surface cleanup`: narrow the gap between handler-local recovery and `server.py` boundary
  wrapping so the next contract is not built on top of `Tool <name> failed` collapse.
- `capability-layer/internal-boundary preparation`: introduce internal execution paths that are
  capability-oriented rather than permanently mirroring `ListDialogs`, `ListMessages`,
  `ListTopics`, and `SearchMessages` as the only organizing boundary.
- `continuation-model unification`: define one coherent continuation vocabulary for read/search
  navigation so Medium stops teaching both `next_cursor` and `next_offset` as adjacent concepts.
- `read/search/topic workflow reshaping`: make the common user job more direct, especially where
  forum reads currently require `ListTopics` before `ListMessages(topic=...)`.
- `helper-tool secondary posture or demotion decisions`: keep helper/operator tools available where
  they still add value, but stop treating discovery-first choreography as the default path for
  ordinary reads and searches.
- `rollout verification`: every public-schema move must include reflected-surface checks and
  restarted-runtime verification, not just local tests.

### Prepare Now To Make Maximal Cheaper

- Keep public adapters separate from the new capability-layer/internal-boundary so a later Maximal
  merge can collapse roles without a second deep refactor.
- Normalize continuation framing and result metadata in a way that can later support more strongly
  structured outputs without forcing that bigger contract jump now.
- Preserve topic/entity cache access, ambiguity handling, and privacy-safe telemetry as reusable
  internals so Maximal can change the surface without throwing away recovery-critical state.
- Document which current tools are `primary`, `secondary`, `merge`, or `future-removal` candidates
  as code comments, tests, or migration notes during implementation rather than rediscovering that
  posture later.

### Defer To Later Maximal

- Full role merging across read/search/inspect surfaces once the Medium migration has proven the
  new capability boundaries.
- Aggressive surface compression that removes most helper/operator tools entirely rather than
  demoting them.
- Bigger result-shape redesigns that would move the product from readable text plus light metadata
  into a much more structured contract.
- Any compatibility window, alias tool, or dual-surface rollout unless a separate decision later
  reintroduces backward-compatibility as a requirement.

### Recommended Order

1. `error-surface cleanup`
   Tighten `server.py` boundary behavior first so unexpected failures preserve actionable recovery
   signals instead of collapsing to the generic wrapper. This gives the rest of the migration a
   cleaner acceptance target and reduces ambiguity during schema changes.
2. `capability-layer/internal-boundary preparation`
   Add internal seams that separate public tool adapters from the underlying read/search/topic
   capabilities. Do this before renaming or merging external contracts so Medium does not hard-code
   another generation of tool-name-shaped internals.
3. `continuation-model unification`
   Replace the split `next_cursor` / `next_offset` / `from_beginning` teaching burden with one
   navigation model that can serve both `ListMessages` and `SearchMessages`. This is the main
   contract cleanup that later workflow reshaping depends on.
4. `read/search/topic workflow reshaping`
   Rework the primary user-task surfaces so common reads and searches no longer assume
   `ListDialogs -> ListTopics -> ListMessages` choreography. Topic fidelity stays preserved, but
   common forum reading should become more direct.
5. `helper-tool secondary posture or demotion decisions`
   After the primary workflows are clear, decide which tools remain visible as secondary operator or
   discovery helpers and which are on the future-removal path. Doing this earlier would freeze
   helper roles before the primary workflow contract is stable.
6. `rollout verification`
   Finish each contract-affecting slice with reflected-schema checks, restart validation, and
   brownfield contract tests. The rollout step is last in sequence but mandatory after every public
   move, because this runtime snapshots discovery at process start.

## Validation And Rollout Gates

The next implementation milestone should treat this section as an acceptance gate, not a generic QA
appendix. Any change that moves public tool names, parameters, continuation fields, or result
framing must prove repository correctness and restarted-runtime correctness together.

### Runtime Verification Is Not Optional Once Public Schemas Move

`server.py` enumerates tools by reflection and snapshots the tool mapping at process start. Because
discovery is snapshotted, local edits are not enough: after any public-schema move, run local
`list-tools`, then restart or rebuild the runtime and run `list-tools` again against the fresh
process. If the live surface is stale after restart, the milestone is not done.

Minimum reflected tool-schema check:

```bash
UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools
```

Required rollout freshness sequence for future contract-affecting work:

1. Run local `list-tools` before the change and capture the baseline tool inventory and schema
   shape.
2. Land the code and test changes.
3. Re-run local `list-tools` and inspect the reflected schema for the changed tools.
4. Restart the long-lived runtime; if containerized deployment is in scope, rebuild and restart it.
5. Run `list-tools` against the restarted runtime and confirm the public surface matches the local
   expectation.

### Named Validation Checks

#### Check 1: `ListMessages` Contract Gate

- Confirm the new read contract still preserves topic fidelity, explicit ambiguity handling, and
  readable transcript output while reducing continuation burden.
- Use `tests/test_tools.py` as the brownfield contract anchor for topic recovery, cross-topic
  reads, unread flows, and `from_beginning` behavior.
- Any continuation change must explicitly replace the old `next_cursor` and `from_beginning`
  expectations with new assertions rather than silently dropping coverage.

#### Check 2: `SearchMessages` Contract Gate

- Confirm the search path remains hit-local and dialog-scoped while moving toward the unified
  continuation model.
- Use `tests/test_tools.py` anchors for `[HIT]` formatting, context-window behavior, and the
  existing `next_offset` pagination expectations.
- If search adopts the shared navigation contract, the test suite must make that contract visible
  in assertions instead of relying on prose-only output inspection.

#### Check 3: `ListTopics` Fidelity Gate

- Confirm `ListTopics` still protects forum-topic fidelity, including exact topic choice,
  inaccessible-topic reporting, and deleted-topic recovery paths.
- Use `tests/test_tools.py` topic coverage as the source-of-truth anchor before any helper-tool
  demotion decision is considered safe.
- If common forum reads become more direct, prove that the underlying topic-state semantics are
  still preserved somewhere explicit in the contract.

#### Check 4: `server.py` Boundary Behavior Gate

- Verify that `server.py` boundary behavior no longer throws away useful handler-local recovery when
  unexpected errors escape.
- Reflection, schema exposure, and boundary wrapping must be checked together because `server.py`
  is both the discovery boundary and the place where generic `Tool <name> failed` collapse happens
  today.
- This check is complete only when the reflected runtime and the repository code agree after
  restart.

#### Check 5: Tool-Test Contract Gate

- `tests/test_tools.py` is mandatory for the primary workflow contract: `ListMessages`,
  `SearchMessages`, `ListTopics`, ambiguity handling, continuation behavior, and helper-step
  recovery text.
- Future Medium work can replace assertions, but it may not leave the new workflow contract
  untested.

#### Check 6: Privacy-Safe Telemetry Gate

- `tests/test_analytics.py` must keep proving that telemetry remains aggregate, useful, and aligned
  with the changed tool boundaries.
- `tests/privacy_audit.sh` must keep proving that privacy-safe telemetry does not widen into
  message-content logging or user-identifying payload capture.
- If helper tools move to a secondary posture, telemetry changes should reflect that without losing
  the privacy-safe telemetry guarantee.

### Rollout Guidance For The Real Runtime

- Treat local tests and local `list-tools` as necessary but insufficient. The runtime used by
  clients is long-lived, and discovery is snapshotted at process start.
- If the later implementation milestone touches public schemas, runtime verification must include a
  restart, and containerized deployment should include a rebuild when the image packages source at
  build time.
- The acceptance question is not "did the tests pass?" It is "did the restarted runtime expose the
  intended `ListMessages`, `SearchMessages`, and `ListTopics` behavior and schema, while preserving
  `server.py` boundary behavior and privacy-safe telemetry evidence?"
