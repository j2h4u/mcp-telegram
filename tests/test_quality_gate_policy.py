import re
import tomllib
from pathlib import Path
from typing import TypedDict, cast

ROOT = Path(__file__).resolve().parents[1]


class _CoverageReportConfig(TypedDict, total=False):
    fail_under: int


class _CoverageConfig(TypedDict):
    report: _CoverageReportConfig


class _ToolConfig(TypedDict):
    coverage: _CoverageConfig
    importlinter: dict[str, list[dict[str, str]]]


_ProjectConfig = TypedDict(
    "_ProjectConfig",
    {"tool": _ToolConfig, "dependency-groups": dict[str, list[str]]},
)


def test_aggregate_coverage_is_informational_only() -> None:
    pyproject_text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    pyproject = cast(_ProjectConfig, tomllib.loads(pyproject_text))
    coverage_report = pyproject["tool"]["coverage"]["report"]
    justfile = (ROOT / "Justfile").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "fail_under" not in coverage_report
    assert "fail_under" not in pyproject_text
    assert all("fail-under" not in text for text in (pyproject_text, justfile, workflow))
    assert "coverage-check:" not in justfile
    assert re.search(r"(?m)^coverage:\s*$", justfile) is None
    assert "just coverage" not in workflow
    workflow_job_ids = [
        match.group(1) for match in re.finditer(r"(?m)^  ([a-z][a-z0-9_-]*):\s*$", workflow.partition("jobs:\n")[2])
    ]
    assert all("coverage" not in job_id for job_id in workflow_job_ids)
    assert "just coverage-check" not in workflow


def test_crap_remains_the_coverage_informed_gate() -> None:
    pyproject_text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    justfile = (ROOT / "Justfile").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert re.search(r"(?m)^  crap:\s*$", workflow) is not None
    assert "run: python3 scripts/ci_pytest_runner.py run -- just crap-check" in workflow
    assert workflow.count("needs: changes") == 4
    assert workflow.count("if: needs.changes.outputs.run_heavy == 'true'") == 4
    assert "needs: [changes, quality, unit, crap, docker-build]" in workflow
    assert re.search(r"(?m)^crap-ratchet:\s*$", justfile) is not None
    assert re.search(r"(?m)^crap:\s*$", justfile) is None
    assert "pytest-crap" not in pyproject_text
    assert "pytest-crap" not in justfile
    assert "--crap" not in justfile
    assert re.search(r"(?m)^coverage-data:\s*$", justfile) is not None
    assert "--cov-append --cov-report=" in justfile
    assert justfile.count("just coverage-data;") == 3
    assert justfile.count('uv run coverage json -o "$coverage_file";') == 3
    assert "--cov-report=json:" not in justfile
    assert "python -m devtools.crap_ratchet" in justfile
    assert "verify: check crap-ratchet runtime-verify" in justfile


def test_import_linter_and_tach_are_both_required_static_gates() -> None:
    pyproject_text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    pyproject = cast(_ProjectConfig, tomllib.loads(pyproject_text))
    justfile = (ROOT / "Justfile").read_text(encoding="utf-8")

    dev_dependencies = pyproject["dependency-groups"]["dev"]
    contracts = pyproject["tool"]["importlinter"]["contracts"]
    assert any(str(dependency).startswith("import-linter>=") for dependency in dev_dependencies)
    assert {contract["id"] for contract in contracts} == {
        "telegram-transport-layers",
        "scheduler-independent-from-runtime",
        "runtime-observations-independent",
        "admission-observer-independent-from-composition",
    }
    check_recipe = justfile.partition("check:")[2].partition("\n")[0]
    assert "import-contracts" in check_recipe
    assert "module-boundaries" in check_recipe
    assert "uv run lint-imports" in justfile
    assert "uv run tach check --dependencies --interfaces --exact" in justfile
