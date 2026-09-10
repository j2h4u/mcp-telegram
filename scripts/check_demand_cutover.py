#!/usr/bin/env python3
"""Enforce the final Telegram demand cutover shape.

This is a release gate for PR2.  It deliberately uses Python's AST instead of
text matching: comments, documentation, and ordinary task names must not make
the gate noisy, while aliases and nested call expressions must still be
checked.  The gate is intentionally strict about the daemon composition root;
domain modules remain free to expose their own ``run_slice`` implementations
and producers may continue to offer demand to the coordinator.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src" / "mcp_telegram"

# These modules and symbols only exist to bridge PR1's shadow execution into
# legacy workers.  PR2 removes the bridge and gives the coordinator ownership
# of durable execution.
SHADOW_MODULES = frozenset(
    {
        "demand_shadow_wiring",
    }
)
SHADOW_IDENTIFIER_FRAGMENT = "demandshadow"
SHADOW_SYMBOLS = frozenset(
    {
        "DemandCycleRunner",
        "DemandShadow",
        "TelegramDemandShadow",
        "run_legacy_demand_cycle",
    }
)
# Durable polling launchers that must disappear from daemon composition.  The
# source functions may remain in a transition commit, but the final daemon may
# not call them or schedule them.
RETIRED_DURABLE_LOOP_CALLS = frozenset(
    {
        "_run_dialog_reconciliation_loop",
        "_run_message_fact_refresh_with_dedicated_connection",
        "_run_read_position_reconciliation_loop",
        "_run_scheduled_reconciliation_loop",
        "_run_self_profile_refresh_loop",
        "_run_sync_loop",
        "run_access_probe_loop",
        "run_activity_sync_loop",
        "run_cold_backfill_loop",
        "run_delta_catch_up_loop",
        "run_hot_sweep_loop",
    }
)

# Task labels are useful evidence even when a launcher is hidden behind a
# temporary variable or a task-spec list.
RETIRED_DURABLE_TASK_LABELS = frozenset(
    {
        "access_probe_loop",
        "activity_cold_backfill",
        "activity_hot_sweep",
        "activity_sync_loop",
        "backfill_total_messages",
        "delta_catch_up_loop",
        "folder_projection_worker",
        "initialize_read_positions",
        "message_fact_hydration_worker",
        "message_fact_refresh_loop",
        "reconciliation_loop",
        "scheduled_message_reconciliation",
        "self_profile_refresh_loop",
    }
)

# Direct calls on these worker-like objects are the other common way for the
# daemon to bypass coordinator selection.  ``coordinator.run`` is explicitly
# excluded below because it is the one allowed global lifecycle task.
DIRECT_DURABLE_METHODS = frozenset(
    {
        "bootstrap_dms",
        "process_one_batch",
        "run",
        "run_cycle",
        "run_demand_slice",
        "run_full_pass",
        "run_light_pass",
    }
)
DIRECT_DURABLE_RECEIVER_MARKERS = frozenset(
    {
        "backfill",
        "bootstrap",
        "hydration",
        "projection",
        "reconciler",
        "reconciliation",
        "sync",
        "worker",
    }
)

COORDINATOR_TASK_NAME = "telegram_demand_coordinator"
TASK_CREATORS = frozenset({"create_task", "ensure_future", "_create_tracked_task"})


@dataclass(frozen=True, slots=True)
class Finding:
    """One release-gate finding with stable machine-readable rule text."""

    path: str
    line: int
    rule: str
    detail: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.detail}"


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _line(node: ast.AST) -> int:
    return getattr(node, "lineno", 1)


def _name_parts(node: ast.expr) -> tuple[str, ...]:
    """Return dotted names without attempting to resolve runtime values."""
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        return (*_name_parts(node.value), node.attr)
    return ()


def _is_shadow_identifier(value: str) -> bool:
    """Recognize snake_case and CamelCase names from the retired bridge."""
    return SHADOW_IDENTIFIER_FRAGMENT in value.casefold().replace("_", "")


def _is_shadow_module(value: str) -> bool:
    module_name = value.rsplit(".", 1)[-1]
    return module_name in SHADOW_MODULES or _is_shadow_identifier(module_name)


def _literal_strings(node: ast.AST) -> tuple[str, ...]:
    return tuple(
        item.value for item in ast.walk(node) if isinstance(item, ast.Constant) and isinstance(item.value, str)
    )


class _ShadowVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.path = relative_path
        self.findings: list[Finding] = []
        self.shadow_aliases: set[str] = set()

    def _add(self, node: ast.AST, rule: str, detail: str) -> None:
        self.findings.append(Finding(self.path, _line(node), rule, detail))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if _is_shadow_module(alias.name):
                self._add(node, "shadow-module", f"PR1 shadow module {alias.name!r} is retired")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if _is_shadow_module(module):
            self._add(node, "shadow-module", f"PR1 shadow module {module!r} is retired")
        for alias in node.names:
            if alias.name in SHADOW_SYMBOLS:
                self._add(node, "shadow-symbol", f"PR1 shadow symbol {alias.name!r} is retired")
                self.shadow_aliases.add(alias.asname or alias.name)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in SHADOW_SYMBOLS or node.id in self.shadow_aliases:
            self._add(node, "shadow-symbol", f"PR1 shadow symbol {node.id!r} is retired")
        elif _is_shadow_identifier(node.id):
            self._add(node, "shadow-field", f"PR1 shadow field {node.id!r} is retired")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in SHADOW_SYMBOLS:
            self._add(node, "shadow-symbol", f"PR1 shadow attribute {node.attr!r} is retired")
        elif _is_shadow_identifier(node.attr):
            self._add(node, "shadow-field", f"PR1 shadow field {node.attr!r} is retired")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_declared_name(node, node.name)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_declared_name(node, node.name)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._check_declared_name(node, node.name)
        self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> None:
        self._check_declared_name(node, node.arg)
        self.generic_visit(node)

    def _check_declared_name(self, node: ast.AST, name: str) -> None:
        if name in SHADOW_SYMBOLS:
            self._add(node, "shadow-symbol", f"PR1 shadow symbol {name!r} is retired")
        elif _is_shadow_identifier(name):
            self._add(node, "shadow-field", f"PR1 shadow field {name!r} is retired")


class _DaemonVisitor(ast.NodeVisitor):
    """Check only daemon composition, where durable ownership is decided."""

    def __init__(self, relative_path: str) -> None:
        self.path = relative_path
        self.findings: list[Finding] = []
        self.coordinator_tasks = 0
        self.retired_loop_aliases: set[str] = set()
        self.direct_worker_aliases: set[str] = set()
        self.coordinator_run_aliases: set[str] = set()
        self.string_aliases: dict[str, set[str]] = {}

    def _add(self, node: ast.AST, rule: str, detail: str) -> None:
        self.findings.append(Finding(self.path, _line(node), rule, detail))

    @staticmethod
    def _call_name(node: ast.Call) -> str | None:
        parts = _name_parts(node.func) if isinstance(node.func, ast.expr) else ()
        return parts[-1] if parts else None

    @staticmethod
    def _receiver_is_coordinator(node: ast.Call) -> bool:
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "run":
            return False
        return _DaemonVisitor._looks_like_coordinator_receiver(node.func.value)

    @staticmethod
    def _looks_like_coordinator_receiver(node: ast.expr) -> bool:
        return any("coordinator" in part.lower() for part in _name_parts(node))

    @staticmethod
    def _is_task_creator(node: ast.Call) -> bool:
        name = _DaemonVisitor._call_name(node)
        return name in TASK_CREATORS

    @staticmethod
    def _target_names(node: ast.AST) -> tuple[str, ...]:
        if isinstance(node, ast.Name):
            return (node.id,)
        if isinstance(node, (ast.List, ast.Tuple)):
            return tuple(name for element in node.elts for name in _DaemonVisitor._target_names(element))
        return ()

    @classmethod
    def _assignment_targets(cls, node: ast.Assign | ast.AnnAssign) -> tuple[str, ...]:
        targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
        return tuple(name for target in targets for name in cls._target_names(target))

    @staticmethod
    def _callable_name(node: ast.expr | None) -> str | None:
        if node is None:
            return None
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None

    def _assigned_strings(self, node: ast.expr | None) -> set[str]:
        if node is None:
            return set()
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.Name):
            return set(self.string_aliases.get(node.id, ()))
        return set()

    @staticmethod
    def _remember_alias(aliases: set[str], name: str | None) -> bool:
        if name is None or name in aliases:
            return False
        aliases.add(name)
        return True

    def _collect_import_aliases(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in RETIRED_DURABLE_LOOP_CALLS:
                        self._remember_alias(self.retired_loop_aliases, alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported_name = alias.name.rsplit(".", 1)[-1]
                    if imported_name in RETIRED_DURABLE_LOOP_CALLS:
                        self._remember_alias(self.retired_loop_aliases, alias.asname)

    def _collect_assignment_aliases(self, assignments: tuple[ast.Assign | ast.AnnAssign, ...]) -> bool:
        changed = False
        for node in assignments:
            value = node.value
            value_name = self._callable_name(value)
            value_strings = self._assigned_strings(value)
            for target in self._assignment_targets(node):
                changed |= self._remember_alias(
                    self.retired_loop_aliases,
                    target
                    if value_name in RETIRED_DURABLE_LOOP_CALLS or value_name in self.retired_loop_aliases
                    else None,
                )
                changed |= self._remember_alias(
                    self.direct_worker_aliases,
                    target if value_name in self.direct_worker_aliases else None,
                )
                changed |= self._remember_alias(
                    self.coordinator_run_aliases,
                    target if value_name in self.coordinator_run_aliases else None,
                )
                if isinstance(value, ast.Attribute):
                    changed |= self._remember_alias(
                        self.retired_loop_aliases,
                        target if value.attr in RETIRED_DURABLE_LOOP_CALLS else None,
                    )
                    changed |= self._remember_alias(
                        self.direct_worker_aliases,
                        target
                        if value.attr in DIRECT_DURABLE_METHODS and self._looks_like_durable_receiver(value.value)
                        else None,
                    )
                    changed |= self._remember_alias(
                        self.coordinator_run_aliases,
                        target if value.attr == "run" and self._looks_like_coordinator_receiver(value.value) else None,
                    )
                if value_strings:
                    known_strings = self.string_aliases.setdefault(target, set())
                    if known_strings.isdisjoint(value_strings):
                        known_strings.update(value_strings)
                        changed = True
        return changed

    def collect_bindings(self, tree: ast.AST) -> None:
        """Collect simple aliases before checking calls, regardless of source order."""
        assignments = tuple(node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign)))
        self._collect_import_aliases(tree)
        while self._collect_assignment_aliases(assignments):
            pass

    def _resolved_strings(self, node: ast.AST) -> tuple[str, ...]:
        values = set(_literal_strings(node))
        for item in ast.walk(node):
            if isinstance(item, ast.Name):
                values.update(self.string_aliases.get(item.id, ()))
        return tuple(sorted(values))

    def _check_coordinator_task(self, node: ast.Call) -> None:
        if not self._is_task_creator(node):
            return
        strings = self._resolved_strings(node)
        is_named = COORDINATOR_TASK_NAME in strings or any("coordinator" in value.lower() for value in strings)
        has_coordinator_run = any(
            isinstance(item, ast.Call)
            and (self._receiver_is_coordinator(item) or self._call_name(item) in self.coordinator_run_aliases)
            for item in ast.walk(node)
        )
        if has_coordinator_run or is_named:
            self.coordinator_tasks += 1

    def _check_retired_loop(self, node: ast.Call) -> None:
        name = self._call_name(node)
        if name in RETIRED_DURABLE_LOOP_CALLS or name in self.retired_loop_aliases:
            self._add(node, "retired-durable-launch", f"daemon launches retired durable loop {name!r}")

    def _check_task_label(self, node: ast.Call) -> None:
        if not self._is_task_creator(node):
            return
        for value in self._resolved_strings(node):
            if value in RETIRED_DURABLE_TASK_LABELS:
                self._add(node, "retired-durable-task", f"daemon creates retired durable task {value!r}")
            elif _is_shadow_identifier(value):
                self._add(node, "shadow-task", f"PR1 shadow task label {value!r} is retired")

    @staticmethod
    def _looks_like_durable_receiver(node: ast.expr) -> bool:
        parts = _name_parts(node)
        return bool(parts) and any(
            marker in part.lower() for part in parts for marker in DIRECT_DURABLE_RECEIVER_MARKERS
        )

    def _check_direct_worker_operation(self, node: ast.Call) -> None:
        name = self._call_name(node)
        if name in self.direct_worker_aliases:
            self._add(
                node,
                "direct-durable-operation",
                f"daemon directly invokes durable worker operation {name!r}",
            )
            return
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in DIRECT_DURABLE_METHODS:
            return
        receiver = node.func.value
        if not self._looks_like_durable_receiver(receiver):
            return
        if any("coordinator" in part.lower() for part in _name_parts(receiver)):
            return
        self._add(
            node,
            "direct-durable-operation",
            f"daemon directly invokes durable worker operation {node.func.attr!r}",
        )

    def visit_Call(self, node: ast.Call) -> None:
        self._check_coordinator_task(node)
        self._check_retired_loop(node)
        self._check_task_label(node)
        self._check_direct_worker_operation(node)
        self.generic_visit(node)


def _python_files(source_root: Path) -> tuple[Path, ...]:
    return tuple(sorted(source_root.rglob("*.py")))


def find_violations(root: Path = ROOT) -> tuple[Finding, ...]:
    """Return deterministic cutover findings for a repository checkout."""
    source_root = root / "src" / "mcp_telegram"
    findings: list[Finding] = []
    daemon_found = False
    daemon_visitor: _DaemonVisitor | None = None
    if not source_root.is_dir():
        return (Finding("src/mcp_telegram", 1, "source-root", "source package directory is missing"),)
    for path in _python_files(source_root):
        relative_path = _relative(path, root)
        if _is_shadow_module(path.stem):
            findings.append(
                Finding(relative_path, 1, "shadow-module", f"PR1 shadow module {path.stem!r} must be removed")
            )
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            findings.append(Finding(relative_path, 1, "parse", f"cannot inspect source: {type(exc).__name__}"))
            continue
        shadow_visitor = _ShadowVisitor(relative_path)
        shadow_visitor.visit(tree)
        findings.extend(shadow_visitor.findings)
        if path.name == "daemon.py":
            daemon_found = True
            daemon_visitor = _DaemonVisitor(relative_path)
            daemon_visitor.collect_bindings(tree)
            daemon_visitor.visit(tree)
            findings.extend(daemon_visitor.findings)

    if daemon_found and daemon_visitor is not None:
        if daemon_visitor.coordinator_tasks == 0:
            findings.append(
                Finding("src/mcp_telegram/daemon.py", 1, "coordinator-task", "missing coordinator task launch")
            )
        elif daemon_visitor.coordinator_tasks > 1:
            findings.append(
                Finding(
                    "src/mcp_telegram/daemon.py",
                    1,
                    "coordinator-task",
                    f"expected one coordinator task launch, found {daemon_visitor.coordinator_tasks}",
                )
            )
    else:
        findings.append(Finding("src/mcp_telegram/daemon.py", 1, "daemon", "daemon composition module is missing"))
    return tuple(sorted(findings, key=lambda finding: (finding.path, finding.line, finding.rule, finding.detail)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (default: checkout root)")
    args = parser.parse_args(argv)
    root = cast(Path, args.root)
    findings = find_violations(root.resolve())
    if findings:
        print("Demand cutover gate failed:", file=sys.stderr)
        print("\n".join(f"- {finding.render()}" for finding in findings), file=sys.stderr)
        return 1
    print("Demand cutover gate passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
