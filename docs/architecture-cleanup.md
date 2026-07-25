# Architecture cleanup: adopting `tach`

`tach.toml` declares the target module graph for the clean-architecture migration.
It is intentionally stricter than the current code. `just module-boundaries`
is advisory until this document is empty; `just check` and import-linter remain
the enforced gates during the transition.

When `just module-boundaries` turns green:

1. add `module-boundaries` to `just check`;
2. keep `tach` as the primary architecture gate;
3. delete redundant import-linter contracts from `pyproject.toml`.

Do not fix a `tach` failure by approving the edge in `tach.toml` unless the
edge is part of the target architecture. The default remediation is moving the
abstraction, exposing a public interface, or inverting the dependency.

## Why `tach`, not only import-linter

Import-linter is useful as a ratchet, but it is mostly a list of forbidden
imports. It does not naturally express the intended module graph, public
interfaces, ownership seams, or cleanup order.

`tach` gives us the architecture as data:

- declared dependencies instead of accidental imports;
- public interfaces for module seams;
- layered architecture;
- cycle checks;
- dependency maps for cleanup planning;
- incremental adoption without making the main gate red.

## Adoption mode

The official Tach happy path is:

1. define modules with `tach init` or `tach mod`;
2. run `tach sync` so `tach.toml` matches current imports;
3. enforce `tach check` in CI/pre-commit;
4. tighten dependencies, public interfaces, layers, and deprecations over time.

That is the right mode when the goal is an immediately green ratchet.

This repository is using a different mode on purpose: `tach.toml` describes the
target architecture first, and the codebase is pulled toward it in later PRs.
Because that makes `tach check` red by design, `module-boundaries` is not part
of `just check`, CI, or pre-commit yet.

Do not mix the two modes in one step:

- for an enforced baseline, allow current edges through `tach sync`, `unchecked`,
  or `deprecated`, then ratchet downward;
- for target-architecture design, keep bad edges undeclared and document the red
  baseline here.

## Target layers

Highest first:

| Layer | Modules | Rule |
|---|---|---|
| `delivery` | `server`, `tools`, CLI entrypoint | Transports and presentation only. No Telegram or SQLite. |
| `composition` | `daemon` | The only layer that wires concrete adapters together. |
| `application` | daemon API, read/query services, sync orchestration | Use cases and projections. Depends on ports/contracts, not delivery. |
| `capability` | `messages`, `folders`, `reactions`, `topics` | Vertical slices with contracts/ports/use cases/adapters. |
| `telegram_gateway` | Telethon client and Telegram fact adapters | Anti-corruption over Telegram/Telethon. |
| `persistence` | `sync_db`, `feedback_db`, `fts`, `read_state` | Local state ownership. |
| `foundation` | config, models, contracts, errors, temporal, pagination, formatting | Pure shared primitives. |

Same-layer edges must be explicit. Higher layers may depend downward only when
the edge is declared. Upward edges are always wrong unless the target graph
itself is wrong.

## Product architecture invariants

- The daemon owns Telegram access, TelegramClient lifecycle, and DB writes.
- MCP tools remain thin inbound adapters.
- Delivery must not import `telegram`, `sync_db`, capability adapters, or daemon internals.
- Capability `contracts.py` and `ports.py` are transport- and storage-neutral.
- Concrete adapters are wired in composition or daemon-owned application services.
- SQLite schema names can remain legacy while agent-facing contracts become cleaner.

## Adoption rules

- No `deprecated = true` dependency waivers in `tach.toml`.
- No inline `tach: ignore` comments without adding a named cleanup item here.
- `ignore_type_checking_imports = false`: type-only imports still count as coupling.
- `require_ignore_directive_reasons = "error"`: any future inline ignore must
  carry a reason, matching Tach's own strict configuration style.
- `exact = false` during adoption: the current brownfield graph still uses grouped
  modules, so exact unused-edge checks produce noise before the graph is split.
  Turn `exact` on only after the advisory gate is close to green or after moving
  capability-owned details into domain configs.
- Use `just module-map` when planning a cleanup slice; commit the map only if it is
  intentionally needed for review.
- `tach.domain.toml` is the target ownership mechanism for mature vertical
  packages. Keep the root `tach.toml` authoritative until a package has a stable
  internal contract/ports/adapters split.
- Do not install the pre-commit hook while the advisory gate is intentionally red.
  Add it only when `module-boundaries` is promoted into `just check`.
- Tach ships a pytest plugin. Keep `-p no:tach` in pytest addopts until this
  project intentionally adopts Tach impact-analysis for test selection.

## Useful Tach commands

```bash
just module-boundaries
uv run tach check --output json
just module-map
uv run tach map --direction dependents
uv run tach map --closure src/mcp_telegram/daemon_reading.py
uv run tach report src/mcp_telegram/daemon_reading.py --dependencies --usages --raw
uv run tach show --mermaid -o tach-module-graph.mmd
```

`tach-module-map.json` and generated graph files are working artifacts. Commit
them only when a PR review needs the exact snapshot.

## Baseline on adoption

Run:

```bash
just module-boundaries
```

Initial result on `chore/tach-architecture-boundaries`:

- 105 total Tach failures;
- 95 undeclared dependencies;
- 6 layer violations;
- 4 private interface violations.

The first visible cleanup categories are:

1. `daemon_reading` is a god-facade over query helpers; it imports private SQL
   constants and helper functions from sibling daemon modules. This should become
   explicit application/query contracts.
2. Activity and scheduled-message workers share private helpers across modules
   (`activity_peer_sweep`, `activity_sync`, `activity_peer_resolve`,
   `scheduled_messages`, `own_only`). Extract neutral ports/contracts or combine
   cohesive units.
3. The CLI package root is now modelled as delivery, but it still exposes/imports
   daemon and Telegram wiring directly. Move CLI wiring out of `mcp_telegram`
   when the entrypoint becomes too crowded.
4. `daemon_api` reaches directly into persistence and capability implementation
   details (`folders.sqlite_repository`, `feedback_db`, refreshers). The target
   is an application service boundary.
5. Telegram gateway modules import application/persistence helpers
   (`dialog_sync`, `daemon_message`, `messages.sqlite_repository`,
   `telegram_gateway` helper module). Move shared Telegram error/read-model
   contracts downward or split adapters.
6. Capability internals still need public interface refinement. Current public
   interfaces expose contracts/ports/refresh seams, but callers still use private
   details such as `daemon_client.daemon_connection` and `_DaemonClientLike`.

Each cleanup PR should remove one category of failures and tighten `tach.toml`
only when it reflects the intended graph more accurately.
