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

[To be written]
