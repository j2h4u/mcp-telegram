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


def _findings_at(relative_path: str, source: str) -> set[tuple[str, str]]:
    gate = _load_gate()
    visitor = gate._PolicyVisitor(relative_path)
    visitor.visit(ast.parse(source))
    return {(finding.category, finding.key) for finding in visitor.findings}


def _findings(source: str) -> set[tuple[str, str]]:
    return _findings_at("src/mcp_telegram/capability.py", source)


NON_OPERATOR_POLICY_CASES = (
    ("src/mcp_telegram/delta_sync.py", "_DELTA_SLICE_MESSAGE_LIMIT = 100\n"),
    (
        "src/mcp_telegram/dialog_sync.py",
        (
            "class DialogReconciliationWorker:\n"
            "    async def run_full_pass(self) -> None:\n"
            "        await work(wait_on_throttle=True)\n"
        ),
    ),
    (
        "src/mcp_telegram/dialog_sync.py",
        (
            "class DialogFullReconciliationDemandAdapter:\n"
            "    async def run_slice(self) -> None:\n"
            "        await work(wait_on_throttle=False)\n"
        ),
    ),
    (
        "src/mcp_telegram/reactions/refresh.py",
        "_PERSISTENCE_RETRY_DELAYS_SECONDS = (0.25, 1.0, 2.0)\n",
    ),
    (
        "src/mcp_telegram/scheduled_messages.py",
        "class ScheduledReconciliationPolicy:\n    failure_retry_seconds: int = 300\n",
    ),
    (
        "src/mcp_telegram/scheduled_messages.py",
        (
            "class ScheduledMessageReconciler:\n"
            "    async def _process_due_dialog(self) -> None:\n"
            "        retry_at = int(time.time()) + exc.retry_after_seconds\n"
        ),
    ),
    (
        "src/mcp_telegram/sync_db.py",
        '_ACCOUNT_COOLDOWN_UNTIL_UTC_KEY = "telegram_account_cooldown_until_utc"\n',
    ),
    (
        "src/mcp_telegram/telegram_demand.py",
        (
            "def resolve_admission_deadline(now, contract):\n"
            "    policy_deadline = now + contract.admission_timeout_seconds\n"
        ),
    ),
    (
        "src/mcp_telegram/telegram_rpc.py",
        (
            "class TelegramRpcGate:\n"
            "    def _persist_account_cooldown(self) -> None:\n"
            "        deadline_utc = time.time() + max(0.0, remaining)\n"
        ),
    ),
    (
        "src/mcp_telegram/telegram_rpc_consumers.py",
        "_ADMISSION_TIMEOUT_SECONDS = {service_class: 15.0}\n",
    ),
    (
        "src/mcp_telegram/telegram_rpc_consumers.py",
        "_SOURCE_OUTSTANDING_LIMIT = {service_class: 8}\n",
    ),
    (
        "src/mcp_telegram/telegram_rpc_consumers.py",
        "def validate_demand_contracts():\n    source_limits = {}\n",
    ),
)


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
            "def fetch() -> None:\n    cache_ttl_seconds: int = 300\n",
            ("policy_assignments", "src/mcp_telegram/capability.py:fetch:cache_ttl_seconds"),
        ),
        (
            "DEFAULT_TTL = 300\nsettings = {'ttl_seconds': DEFAULT_TTL}\n",
            ("policy_dict_values", "src/mcp_telegram/capability.py:<module>:ttl_seconds"),
        ),
    ],
)
def test_policy_placement_gate_rejects_straightforward_literal_evasions(source: str, expected: tuple[str, str]) -> None:
    assert expected in _findings(source)


@pytest.mark.parametrize(("relative_path", "source"), NON_OPERATOR_POLICY_CASES)
def test_policy_placement_gate_ignores_exact_non_operator_policy_findings(relative_path: str, source: str) -> None:
    assert _findings_at(relative_path, source) == set()


@pytest.mark.parametrize(("_relative_path", "source"), NON_OPERATOR_POLICY_CASES)
def test_non_operator_policy_exclusions_do_not_apply_in_neighbor_modules(_relative_path: str, source: str) -> None:
    assert _findings_at("src/mcp_telegram/neighbor.py", source)


def test_demand_cadence_and_injected_timeout_remain_policy_findings() -> None:
    composition_findings = _findings_at(
        "src/mcp_telegram/demand_composition.py",
        "ACTIVITY_ARCHIVE_INTERVAL_SECONDS = 3_600.0\n"
        "DIALOG_FULL_RECONCILIATION_INTERVAL_SECONDS = 86_400.0\n"
        "def build_durable_adapter_map():\n"
        "    return Adapter(interval_seconds=DIALOG_FULL_RECONCILIATION_INTERVAL_SECONDS)\n",
    )
    assert composition_findings == {
        (
            "policy_assignments",
            "src/mcp_telegram/demand_composition.py:<module>:ACTIVITY_ARCHIVE_INTERVAL_SECONDS",
        ),
        (
            "policy_assignments",
            "src/mcp_telegram/demand_composition.py:<module>:DIALOG_FULL_RECONCILIATION_INTERVAL_SECONDS",
        ),
        (
            "policy_call_keywords",
            "src/mcp_telegram/demand_composition.py:build_durable_adapter_map:interval_seconds",
        ),
    }
    assert _findings_at(
        "src/mcp_telegram/dialog_sync.py",
        "class DialogFullReconciliationDemandAdapter:\n"
        "    def __init__(self, *, interval_seconds: float = 86_400.0) -> None:\n"
        "        pass\n",
    ) == {
        (
            "policy_defaults",
            "src/mcp_telegram/dialog_sync.py:DialogFullReconciliationDemandAdapter.__init__:interval_seconds",
        )
    }
    assert _findings_at(
        "src/mcp_telegram/scheduled_messages.py",
        "class ScheduledMessageReconciler:\n"
        "    def __init__(self) -> None:\n"
        "        self.policy = Policy(activity_rpc_timeout_seconds=120.0)\n",
    ) == {
        (
            "policy_call_keywords",
            "src/mcp_telegram/scheduled_messages.py:ScheduledMessageReconciler.__init__:activity_rpc_timeout_seconds",
        )
    }


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
