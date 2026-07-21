#!/usr/bin/env python3
"""Placement/shape ratchet for operator-controlled policy values.

The manifest captures reviewed protocol/domain/request defaults and inherited
non-TTL debt.  New values need an explicit decision: put them in config, add a
runtime sink, or document why they are not operator policy.

This intentionally verifies local placement and injected policy-object shape;
it does not prove whole-runtime value provenance.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src" / "mcp_telegram"
MANIFEST_PATH = ROOT / "scripts" / "policy_placement_allowlist.toml"
CONFIG_MODULE = "src/mcp_telegram/config.py"
POLICY_NAME = re.compile(
    r"(?:ttl|retention|freshness|timeout|deadline|interval|limit|budget|concurrency|threshold|"
    r"page_size|batch_size|retry|backoff|cooldown|window)",
    re.IGNORECASE,
)
PLACEMENT_POLICY_NAME = re.compile(
    r"(?:ttl|retention|freshness|timeout|deadline|interval|concurrency|threshold|"
    r"page_size|batch_size|backoff|cooldown|window)",
    re.IGNORECASE,
)
REQUIRED_POLICY_FIELDS = {
    "read_at_ttl_seconds",
    "entity_detail_ttl_seconds",
    "user_directory_ttl_seconds",
    "group_directory_ttl_seconds",
    "resolver_enrichment_ttl_seconds",
    "telemetry_retention_ttl_seconds",
}
REQUIRED_POLICY_SINKS = {
    "daemon_api.py": ("DaemonApiPolicy", REQUIRED_POLICY_FIELDS),
    "resolver.py": ("ResolverEnrichmentPolicy", {"entity_cache", "ttl_seconds"}),
}


@dataclass(frozen=True, slots=True)
class Finding:
    category: str
    key: str
    line: int


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _qualified_name(stack: list[str]) -> str:
    return ".".join(stack) if stack else "<module>"


def _is_literal_policy_value(value: ast.expr | None) -> bool:
    if value is None:
        return False
    if isinstance(value, ast.Constant):
        return value.value is not None
    if isinstance(value, ast.Call):
        return (
            isinstance(value.func, ast.Name)
            and value.func.id in {"int", "float", "str"}
            and len(value.args) == 1
            and not value.keywords
            and _is_literal_policy_value(value.args[0])
        )
    return isinstance(value, (ast.BinOp, ast.UnaryOp, ast.Tuple, ast.List, ast.Set, ast.Dict))


def _called_name(node: ast.Call) -> str | None:
    if (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "os"
    ):
        return f"os.{node.func.value.attr}.{node.func.attr}"
    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "os":
        return f"os.{node.func.attr}"
    return None


class _PolicyVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.stack: list[str] = []
        self.findings: list[Finding] = []
        self.os_aliases = {"os"}
        self.getenv_aliases: set[str] = set()
        self.environ_aliases: set[str] = set()
        self.field_aliases: set[str] = set()
        self.dataclasses_aliases = {"dataclasses"}
        self.literal_alias_scopes: list[dict[str, bool]] = [{}]

    def _key(self, name: str) -> str:
        return f"{self.relative_path}:{_qualified_name(self.stack)}:{name}"

    def _is_policy_value(self, value: ast.expr | None) -> bool:
        if _is_literal_policy_value(value):
            return True
        if not isinstance(value, ast.Name):
            return False
        return any(scope.get(value.id, False) for scope in reversed(self.literal_alias_scopes))

    def _record_literal_aliases(self, node: ast.Assign | ast.AnnAssign) -> None:
        if not self._is_policy_value(node.value):
            return
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                self.literal_alias_scopes[-1][target.id] = True

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.stack.append(node.name)
        positional = [*node.args.posonlyargs, *node.args.args]
        defaults = [None] * (len(positional) - len(node.args.defaults)) + list(node.args.defaults)
        for arg, default in zip(positional, defaults, strict=True):
            if POLICY_NAME.search(arg.arg) and self._is_policy_value(default):
                self.findings.append(Finding("policy_defaults", self._key(arg.arg), node.lineno))
        for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True):
            if POLICY_NAME.search(arg.arg) and self._is_policy_value(default):
                self.findings.append(Finding("policy_defaults", self._key(arg.arg), node.lineno))
        self.literal_alias_scopes.append({})
        self.generic_visit(node)
        self.literal_alias_scopes.pop()
        self.stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.literal_alias_scopes.append({})
        self.generic_visit(node)
        self.literal_alias_scopes.pop()
        self.stack.pop()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "os":
                self.os_aliases.add(alias.asname or alias.name)
            if alias.name == "dataclasses":
                self.dataclasses_aliases.add(alias.asname or alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "os":
            for alias in node.names:
                if alias.name == "getenv":
                    self.getenv_aliases.add(alias.asname or alias.name)
                if alias.name == "environ":
                    self.environ_aliases.add(alias.asname or alias.name)
        if node.module == "dataclasses":
            for alias in node.names:
                if alias.name == "field":
                    self.field_aliases.add(alias.asname or alias.name)

    def _target_policy_name(self, node: ast.Assign | ast.AnnAssign) -> str | None:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and PLACEMENT_POLICY_NAME.search(target.id):
                return target.id
        return None

    @staticmethod
    def _target_name(target: ast.expr) -> str | None:
        if isinstance(target, ast.Name):
            return target.id
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
            return f"{target.value.id}.{target.attr}"
        return None

    def _is_field_call(self, node: ast.Call) -> bool:
        if isinstance(node.func, ast.Name):
            return node.func.id in self.field_aliases
        return (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in self.dataclasses_aliases
            and node.func.attr == "field"
        )

    def _record_field_default(self, node: ast.Assign | ast.AnnAssign) -> None:
        name = self._target_policy_name(node)
        value = node.value
        if name is None or not isinstance(value, ast.Call) or not self._is_field_call(value):
            return
        if any(keyword.arg in {"default", "default_factory"} for keyword in value.keywords):
            self.findings.append(Finding("dataclass_field_defaults", self._key(name), node.lineno))

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._is_policy_value(node.value):
            for target in node.targets:
                name = self._target_name(target)
                if name and POLICY_NAME.search(name):
                    self.findings.append(Finding("policy_assignments", self._key(name), node.lineno))
        self._record_literal_aliases(node)
        self._record_field_default(node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if (
            (name := self._target_name(node.target)) is not None
            and POLICY_NAME.search(name)
            and self._is_policy_value(node.value)
        ):
            self.findings.append(Finding("policy_assignments", self._key(name), node.lineno))
        self._record_literal_aliases(node)
        self._record_field_default(node)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and PLACEMENT_POLICY_NAME.search(key.value)
                and self._is_policy_value(value)
            ):
                self.findings.append(Finding("policy_dict_values", self._key(key.value), node.lineno))
        self.generic_visit(node)

    def _is_environ_value(self, node: ast.expr) -> bool:
        return (isinstance(node, ast.Name) and node.id in self.environ_aliases) or (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id in self.os_aliases
        )

    def visit_Call(self, node: ast.Call) -> None:
        called = _called_name(node)
        environment_key: str | None = called if called in {"os.environ.get", "os.getenv"} else None
        if isinstance(node.func, ast.Name) and node.func.id in self.getenv_aliases:
            environment_key = "getenv"
        if isinstance(node.func, ast.Attribute) and self._is_environ_value(node.func.value) and node.func.attr == "get":
            environment_key = environment_key or "environ.get"
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in self.os_aliases
            and node.func.attr == "getenv"
        ):
            environment_key = "getenv"
        if environment_key:
            self.findings.append(Finding("environment_reads", self._key(environment_key), node.lineno))
        for keyword in node.keywords:
            if keyword.arg and PLACEMENT_POLICY_NAME.search(keyword.arg) and self._is_policy_value(keyword.value):
                self.findings.append(Finding("policy_call_keywords", self._key(keyword.arg), node.lineno))
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if self._is_environ_value(node.value):
            self.findings.append(Finding("environment_reads", self._key("environ[]"), node.lineno))
        self.generic_visit(node)


def _load_manifest() -> dict[str, set[str]]:
    with MANIFEST_PATH.open("rb") as handle:
        data = cast(dict[str, object], tomllib.load(handle))
    categories = (
        "dataclass_field_defaults",
        "environment_reads",
        "policy_assignments",
        "policy_call_keywords",
        "policy_defaults",
        "policy_dict_values",
    )
    approved = {category: set[str]() for category in categories}
    for section in ("reviewed_protocol_domain", "reviewed_request_contracts", "baseline_non_ttl_debt"):
        raw_entries = data.get(section, {})
        if not isinstance(raw_entries, dict):
            raise ValueError(f"[{section}] must be a TOML table")
        entries = cast(dict[str, object], raw_entries)
        rationale = entries.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError(f"[{section}] needs a non-empty grouped rationale")
        for category in categories:
            values = entries.get(category, [])
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise ValueError(f"[{section}].{category} must be a string list")
            approved[category].update(values)
    individual_entries = data.get("entry", [])
    if not isinstance(individual_entries, list):
        raise ValueError("[[entry]] values must be TOML tables")
    for entry in individual_entries:
        if not isinstance(entry, dict):
            raise ValueError("[[entry]] values must be TOML tables")
        category = entry.get("category")
        key = entry.get("key")
        rationale = entry.get("rationale")
        if (
            category not in approved
            or not isinstance(key, str)
            or not isinstance(rationale, str)
            or not rationale.strip()
        ):
            raise ValueError("each [[entry]] needs a known category, key, and rationale")
        approved[category].add(key)
    return approved


def _is_frozen_slots_dataclass(node: ast.ClassDef) -> bool:
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        is_dataclass = (isinstance(decorator.func, ast.Name) and decorator.func.id == "dataclass") or (
            isinstance(decorator.func, ast.Attribute)
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "dataclasses"
            and decorator.func.attr == "dataclass"
        )
        if is_dataclass:
            keywords = {keyword.arg: keyword.value for keyword in decorator.keywords}
            for name in ("frozen", "slots"):
                value = keywords.get(name)
                if not isinstance(value, ast.Constant) or value.value is not True:
                    return False
            return True
    return False


def _verify_policy_sink() -> list[str]:
    violations: list[str] = []
    for filename, (class_name, required_fields) in REQUIRED_POLICY_SINKS.items():
        path = SOURCE_ROOT / filename
        tree = ast.parse(path.read_text(), filename=str(path))
        policy_class = next(
            (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name),
            None,
        )
        if policy_class is None:
            violations.append(f"{class_name} policy sink is missing")
            continue
        if not _is_frozen_slots_dataclass(policy_class):
            violations.append(f"{class_name} must be a frozen slots dataclass")
        fields = {
            statement.target.id
            for statement in policy_class.body
            if isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.value is None
        }
        missing = required_fields - fields
        if missing:
            violations.append(f"{class_name} is missing default-free injected fields: {', '.join(sorted(missing))}")
    return violations


def main() -> int:
    manifest = _load_manifest()
    violations = _verify_policy_sink()
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative_path = _relative(path)
        if relative_path == CONFIG_MODULE:
            continue
        visitor = _PolicyVisitor(relative_path)
        visitor.visit(ast.parse(path.read_text(), filename=str(path)))
        for finding in visitor.findings:
            if finding.key not in manifest[finding.category]:
                violations.append(f"{finding.category}: {finding.key} (line {finding.line})")
    if violations:
        print("Policy-placement violations:", *[f"- {item}" for item in violations], sep="\n", file=sys.stderr)
        return 1
    print("Policy placement check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
