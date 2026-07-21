from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from typing import Protocol, cast

import pytest


class _Finding(Protocol):
    category: str
    key: str


class _Visitor(Protocol):
    findings: list[_Finding]

    def visit(self, node: ast.AST) -> None: ...


class _PolicyGate(Protocol):
    MANIFEST_PATH: Path

    def _PolicyVisitor(self, relative_path: str) -> _Visitor: ...

    def _load_manifest(self) -> dict[str, set[str]]: ...


def _load_gate() -> _PolicyGate:
    path = Path(__file__).parents[1] / "scripts" / "check_policy_placement.py"
    spec = importlib.util.spec_from_file_location("check_policy_placement", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return cast(_PolicyGate, module)


def _findings(source: str) -> set[tuple[str, str]]:
    gate = _load_gate()
    visitor = gate._PolicyVisitor("src/mcp_telegram/capability.py")
    visitor.visit(ast.parse(source))
    return {(finding.category, finding.key) for finding in visitor.findings}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "def fetch(*, ttl_seconds: int = int(300)) -> None:\n    pass\n",
            ("policy_defaults", "src/mcp_telegram/capability.py:fetch:ttl_seconds"),
        ),
        (
            "DEFAULT_TTL = 300\ndef fetch(*, ttl_seconds: int = DEFAULT_TTL) -> None:\n    pass\n",
            ("policy_defaults", "src/mcp_telegram/capability.py:fetch:ttl_seconds"),
        ),
        (
            "class Cache:\n    def __init__(self) -> None:\n        self.ttl_seconds = 300\n",
            ("policy_assignments", "src/mcp_telegram/capability.py:Cache.__init__:self.ttl_seconds"),
        ),
        (
            "DEFAULT_TTL = 300\nsettings = {'ttl_seconds': DEFAULT_TTL}\n",
            ("policy_dict_values", "src/mcp_telegram/capability.py:<module>:ttl_seconds"),
        ),
    ],
)
def test_policy_placement_gate_rejects_straightforward_literal_evasions(source: str, expected: tuple[str, str]) -> None:
    assert expected in _findings(source)


def test_grouped_allowlist_entries_reject_whitespace_only_rationale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = _load_gate()
    manifest = tmp_path / "policy_placement_allowlist.toml"
    manifest.write_text(
        "[reviewed_protocol_domain]\n"
        "rationale = '   '\n"
        "policy_assignments = []\n"
        "policy_defaults = []\n"
        "policy_call_keywords = []\n"
        "policy_dict_values = []\n"
        "dataclass_field_defaults = []\n"
        "environment_reads = []\n"
        "[reviewed_request_contracts]\nrationale = 'ok'\n"
        "policy_assignments = []\npolicy_defaults = []\npolicy_call_keywords = []\n"
        "policy_dict_values = []\ndataclass_field_defaults = []\nenvironment_reads = []\n"
        "[baseline_non_ttl_debt]\nrationale = 'ok'\n"
        "policy_assignments = []\npolicy_defaults = []\npolicy_call_keywords = []\n"
        "policy_dict_values = []\ndataclass_field_defaults = []\nenvironment_reads = []\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "MANIFEST_PATH", manifest)

    with pytest.raises(ValueError, match="grouped rationale"):
        gate._load_manifest()


def test_individual_allowlist_entries_reject_whitespace_only_rationale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = _load_gate()
    manifest = tmp_path / "policy_placement_allowlist.toml"
    empty_group = (
        "rationale = 'valid'\n"
        "policy_assignments = []\npolicy_defaults = []\npolicy_call_keywords = []\n"
        "policy_dict_values = []\ndataclass_field_defaults = []\nenvironment_reads = []\n"
    )
    manifest.write_text(
        f"[reviewed_protocol_domain]\n{empty_group}"
        f"[reviewed_request_contracts]\n{empty_group}"
        f"[baseline_non_ttl_debt]\n{empty_group}"
        "[[entry]]\n"
        "category = 'policy_defaults'\n"
        "key = 'src/mcp_telegram/capability.py:fetch:ttl_seconds'\n"
        "rationale = '   '\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gate, "MANIFEST_PATH", manifest)

    with pytest.raises(ValueError, match=r"each \[\[entry\]\] needs"):
        gate._load_manifest()
