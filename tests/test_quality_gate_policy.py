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


class _ProjectConfig(TypedDict):
    tool: _ToolConfig


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
    workflow_job_ids = [
        match.group(1) for match in re.finditer(r"(?m)^  ([a-z][a-z0-9_-]*):\s*$", workflow.partition("jobs:\n")[2])
    ]
    assert all("coverage" not in job_id for job_id in workflow_job_ids)
    assert "just coverage-check" not in workflow


def test_crap_remains_the_coverage_informed_gate() -> None:
    justfile = (ROOT / "Justfile").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert re.search(r"(?m)^  crap:\s*$", workflow) is not None
    assert "run: just crap-check" in workflow
    assert workflow.count("needs: changes") == 4
    assert workflow.count("if: needs.changes.outputs.run_heavy == 'true'") == 4
    assert "needs: [changes, quality, unit, crap, docker-build]" in workflow
    assert re.search(r"(?m)^crap-ratchet:\s*$", justfile) is not None
    assert justfile.count("--cov-report=;") == 3
    assert justfile.count('uv run coverage json -o "$coverage_file";') == 3
    assert "--cov-report=json:" not in justfile
    assert "python -m devtools.crap_ratchet" in justfile
    assert "verify: check crap-ratchet runtime-verify" in justfile
