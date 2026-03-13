# Project Retrospective

*A living document updated after each milestone. Lessons feed forward into future planning.*

## Milestone: v1.0 — Core API

**Shipped:** 2026-03-11
**Phases:** 5 | **Plans:** 14 | **Sessions:** ~5

### What Was Built
- Fuzzy name resolver (WRatio) — dialogs and senders, Resolved/Candidates/NotFound tagged union, numeric bypass, ambiguity detection
- Unified `format_messages()` — HH:mm, date headers, session breaks, reactions, replies, media — no Telethon dep at import
- Complete tool surface: `ListDialogs`, `ListMessages` (cursor pagination, sender/unread filters), `SearchMessages` (±3 context, offset pagination), `GetMe`, `GetUserInfo`
- SQLite entity cache (WAL, TTL: users 30d / groups 7d) with TTL enforcement, search upsert, cursor error hardening

### What Worked
- **TDD throughout**: Every plan started with RED stubs, then GREEN, then clean-up. Zero regressions across 14 plans (57 tests at end).
- **Phase audit before archiving**: Running the milestone audit mid-milestone (after Phase 3) caught TOOL-06 (context window never shipped) before closure — Phases 4–5 were added cleanly to close it.
- **Incremental hardening phases**: Adding Phases 4–5 as gap-closure work after audit kept scope honest without derailing the milestone.
- **Fixture design**: `mock_cache / mock_client / make_mock_message` pattern in conftest scaled through all 5 phases without needing rewrites.

### What Was Inefficient
- **TOOL-06 in Phase 2**: SearchMessages ±3 context was specified in Phase 2 but not fully implemented — SUMMARY said it was, audit caught the gap. The summary should not have claimed completion for unverified behaviour.
- **REQUIREMENTS.md traceability "Partial" rows**: CACH-01/02 and TOOL-03 were marked Partial at milestone time — those partial rows created ambiguity. Future: don't close a phase SUMMARY unless the requirement is verifiably complete (tests pass, not just "code written").
- **STATE.md "percent: 42"**: State percent field was not updated correctly after Phase 5 — minor tracking noise.

### Patterns Established
- **Stub -> implement -> verify in 2-plan pairs**: Wave 0 (stubs) + Wave 1 (implementation) per phase. Reliable, parallelizable.
- **Phase audit before archiving**: Mandatory milestone audit before `/gsd:complete-milestone` to catch SUMMARY/reality gaps.
- **Module-level monkeypatching**: `monkeypatch.setattr('mcp_telegram.cache.time', ...)` — patch the module attribute, not the stdlib function directly.
- **`all_names_with_ttl()` pattern**: Cache returns TTL-filtered names to resolver — resolver stays stateless.

### Key Lessons
1. **Don't close a requirement in SUMMARY unless tests verify it end-to-end.** Phase 2 claimed TOOL-06 done but the test was missing. The milestone audit is the safety net — but the earlier the gap is caught the cheaper it is.
2. **Audit mid-milestone, not only at the end.** Running `/gsd:audit-milestone` after Phase 3 (before Phase 4 planning) gave clean scope for gap-closure phases with zero context loss.
3. **TTL enforcement belongs in cache, not in callers.** All callers simplified to `all_names_with_ttl()`; single TTL logic at the boundary.
4. **WRatio 90/60 thresholds as named constants from day one.** Tuning thresholds without touching logic was trivial — the constant naming made tests self-documenting.

### Cost Observations
- Model mix: ~80% sonnet, ~20% opus (research/planning), 0% haiku
- Sessions: ~5 sessions across 2 days
- Notable: Phase audit + 2 hardening phases added <1 day of work after 3-phase core completed in 1 day

---

## Milestone: v1.2 — MCP Surface Research

**Shipped:** 2026-03-13
**Phases:** 4 | **Plans:** 12 | **Sessions:** ~1 focused day

### What Was Built
- Retained-source evidence hierarchy plus a reflected seven-tool brownfield baseline for the current MCP surface
- Comparative audit of the current tool surface across both per-tool and workflow-level model burden
- Minimal, Medium, and Maximal redesign comparison with an explicit Medium-path recommendation
- Standalone implementation memo with sequencing, runtime freshness gates, and bounded Maximal-path preparation

### What Worked
- **Research stayed bounded**: each phase had a clear handoff, so the milestone moved from evidence to memo without reopening earlier discovery.
- **Brownfield authority won over stale notes**: freezing the reflected seven-tool runtime prevented the audit and redesign work from drifting off the actual surface.
- **Decision-focused artifacts**: each phase produced a direct input to the next one, which made the milestone audit pass without integration gaps.

### What Was Inefficient
- **Archive tooling is only partial**: `gsd-tools milestone complete` created the archive files and milestone entry, but manual cleanup was still required for `ROADMAP.md`, `PROJECT.md`, and `REQUIREMENTS.md`.
- **Validation status lagged delivery**: all four `VALIDATION.md` artifacts remained partial, so the milestone closed with `tech_debt` status even though requirements and integration passed.
- **Historical planning docs are uneven**: earlier milestone documents do not all follow the same archive/retrospective pattern, which weakens cross-milestone comparison.

### Patterns Established
- **Research milestones count as shipped work only when they end in a decision-ready implementation brief.**
- **Freeze the reflected runtime early** and treat it as authoritative over inherited planning notes.
- **Public-schema changes need restarted-runtime verification**, not just local doc or code updates.

### Key Lessons
1. **Primary sources plus live runtime data are enough when the scope is explicit.** The milestone stayed tight because it used MCP/Anthropic docs for normative claims and reflection/code/tests for reality checks.
2. **A recommendation milestone needs one final artifact, not a pile of notes.** The standalone implementation memo was the real shipping unit for `v1.2`.
3. **Archive automation must be verified, not assumed.** The generated archives were a useful starting point, but the milestone still needed human review to meet the planning-document intent.
4. **Validation debt should be made visible even when it does not block shipment.** The audit's `tech_debt` status preserved that signal without falsely failing the milestone.

### Cost Observations
- Model mix: mostly planning/research/documentation work, with no production code changes in scope
- Sessions: ~1 concentrated day on 2026-03-13
- Notable: a 4-phase, 12-plan milestone completed in one day because the scope stayed purely research and decision-oriented

---

## Cross-Milestone Trends

### Process Evolution

| Milestone | Sessions | Phases | Key Change |
|-----------|----------|--------|------------|
| v1.0 | ~5 | 5 | Established TDD stub->implement pattern; audit before archive |
| v1.2 | ~1 | 4 | Established evidence->audit->options->memo pattern for research-only milestones |

### Cumulative Quality

| Milestone | Tests | Zero-Dep Additions |
|-----------|-------|--------------------|
| v1.0 | 57 | format_messages() (no Telethon at import) |
| v1.2 | 169 | decision memo, audit frame, and reflected-runtime acceptance-gate pattern |

### Top Lessons (Verified Across Milestones)

1. Audit before archive — milestone audit catches SUMMARY/reality gaps before they become known gaps
2. Stub -> implement in 2-plan pairs — consistent, parallelizable, zero regressions
3. Freeze live runtime reality early when planning docs and shipped surface might drift
