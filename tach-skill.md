# Tach skill notes

Working notes for a future reusable skill for introducing Tach into Python repos.
Keep this file concise: add only stable lessons from real use, not a command log.

## Core workflow decision

Start every Tach adoption by choosing one of two modes.

1. Enforced current-state baseline:
   - use `tach init` / `tach mod`;
   - use `tach sync` to model existing imports;
   - make `tach check` green;
   - add CI/pre-commit;
   - tighten dependencies, interfaces, layers, and deprecated edges over time.

2. Target-architecture advisory baseline:
   - hand-write the desired module graph;
   - leave bad edges undeclared;
   - keep `tach check` red and out of CI/pre-commit;
   - document the baseline and cleanup slices;
   - promote to enforced only when the advisory gate is close to green.

Do not mix the two modes accidentally. `deprecated`, `unchecked`, and broad
`tach sync` output are useful for a green ratchet, but can normalize debt when
the goal is to describe the ideal architecture.

## Tach config defaults that worked here

- `source_roots = ["src"]`
- `root_module = "forbid"` after explicitly modelling the package root when it
  has real code.
- `forbid_circular_dependencies = true` in the target config.
- `ignore_type_checking_imports = false` so type-only coupling remains visible.
- `require_ignore_directive_reasons = "error"` to prevent unexplained inline
  ignores.
- Keep `exact = false` during early brownfield target-mode adoption; turn it on
  later, when grouped modules have been split enough that unused declared edges
  are actionable rather than noise.

## Practical command notes

- `tach report <file-or-dir> --dependencies --usages --raw` is good for local
  blast-radius analysis.
- `tach map --closure <file>` returns a JSON object keyed by the requested file
  path in Tach 0.35, not a bare list.
- `tach map --direction dependents --closure <file>` is useful for checking who
  sits above a candidate cleanup slice.
- `tach show --mermaid -o <file>` can produce very small graphs for a selected
  module, but for single files it may be too sparse to be useful.

## Pitfalls found

- Tach installs a pytest plugin. If the repo is not intentionally adopting Tach
  test-impact analysis, add `-p no:tach` to pytest addopts.
- Be careful with `uv run --project <other-repo> tach sync`: Tach still acts on
  the current working directory unless the command also runs from the intended
  repo. Use `cd <repo> && uv run tach ...` for sync experiments.
- When `forbid_circular_dependencies = true`, a newly exposed cycle can hide the
  detailed undeclared-dependency/interface report. For inventory only, run a
  temporary copy of the config with cycle checks disabled; keep the real config
  strict.

## First successful cleanup pattern

Tach reported private interface failures around `daemon_client` and
`daemon_api`.

Resolution pattern:

- If a symbol is already intended public API, add it to the module interface.
- If external code imports a leading-underscore type/protocol, rename or move it
  into a public contract instead of adding an ignore.

In `mcp-telegram`, this removed all initial private-interface failures:

- `daemon_client.daemon_connection` was already in `__all__`, so it became part
  of the Tach interface.
- `_DaemonClientLike` was used by daemon construction/tests, so it was renamed
  to public `DaemonClientLike`.

## Layer cleanup pattern

When Tach reports an upward edge, first ask whether the imported concept belongs
to the lower layer or to a neutral seam. Do not immediately whitelist the edge.

Patterns that worked:

- Shared Telethon exception classification belongs beside Telegram gateway code,
  not in an application worker that happened to need it first.
- Message projection from Telethon-shaped objects belongs in a Telegram-facing
  projection module, not in a daemon query module.
- If one grouped Tach module contains files that need directed internal edges,
  split those files into explicit modules. Grouping is useful early, but it can
  create artificial same-layer cycles or hide the real ownership seam.
