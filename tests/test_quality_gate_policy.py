import json
import os
import re
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path
from typing import TypedDict, cast

import pytest

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


def test_fixed_metric_thresholds_and_coverage_informed_crap_gate() -> None:
    pyproject_text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    justfile = (ROOT / "Justfile").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert re.search(r"(?m)^  crap-threshold:\s*$", workflow) is not None
    assert "run: python3 scripts/ci_pytest_runner.py run -- just crap-threshold" in workflow
    assert workflow.count("needs: changes") == 3
    assert workflow.count("if: needs.changes.outputs.run_heavy == 'true'") == 3
    assert "needs: [changes, quality, crap-threshold, docker-build]" in workflow
    assert re.search(r"(?m)^  unit:\s*$", workflow) is None
    assert re.search(r"(?m)^radon-threshold:\s*$", justfile) is not None
    assert re.search(r"(?m)^crap-threshold:\s*$", justfile) is not None
    assert "pytest-crap" not in pyproject_text
    assert "pytest-crap" not in justfile
    assert "--crap" not in justfile
    assert re.search(r"(?m)^coverage-data:\s*$", justfile) is not None
    assert "--cov-append --cov-report=" in justfile
    assert justfile.count("just coverage-data;") == 1
    assert justfile.count('uv run coverage json -o "$coverage_file";') == 1
    assert "--cov-report=json:" not in justfile
    assert "python -m devtools.crap_threshold" in justfile
    assert "python -m devtools.radon_threshold" in justfile
    assert "verify: check crap-threshold runtime-verify" in justfile
    assert "--baseline" not in justfile
    assert "tighten-baseline" not in justfile


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


def test_full_ci_is_premerge_or_explicit_not_repeated_on_main_or_nightly() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    triggers = workflow.partition("\non:\n")[2].partition("\npermissions:")[0]
    assert re.search(r"(?m)^  pull_request:", triggers)
    assert re.search(r"(?m)^  workflow_dispatch:", triggers)
    assert not re.search(r"(?m)^  (push|schedule):", triggers)
    assert "run: just package-smoke" not in workflow
    assert workflow.count("-- just crap-threshold") == 1

    codeql = (ROOT / ".github" / "workflows" / "codeql.yml").read_text(encoding="utf-8")
    codeql_triggers = codeql.partition("\non:\n")[2].partition("\njobs:")[0]
    assert not re.search(r"(?m)^  push:", codeql_triggers)
    assert re.search(r"(?m)^  schedule:", codeql_triggers)


def test_full_suite_has_one_recipe_without_compatibility_aliases() -> None:
    result = subprocess.run(
        ["just", "--dry-run", "crap-threshold"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert (result.stdout + result.stderr).count("just coverage-data;") == 1
    justfile = (ROOT / "Justfile").read_text(encoding="utf-8")
    assert not re.search(r"(?m)^(unit|typecheck-tests):", justfile)


@pytest.mark.parametrize("recipe", ["test", "test-parallel"])
def test_development_recipes_require_an_explicit_slice(recipe: str) -> None:
    result = subprocess.run(["just", "--dry-run", recipe], cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "uv run pytest" not in result.stdout + result.stderr


@pytest.mark.parametrize("file_count", [4, 5])
def test_instrumented_batches_execute_each_file_once_through_shared_recipe(tmp_path: Path, file_count: int) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    expected_files = [f"tests/test_{index}.py" for index in range(file_count)]
    for relative_path in expected_files:
        (tmp_path / relative_path).touch()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    invocation_log = tmp_path / "invocations.jsonl"
    uv = bin_dir / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['INVOCATION_LOG'], 'a') as log:\n"
        "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "INVOCATION_LOG": str(invocation_log)}
    # Nested just calls must keep the real recipe file and isolated working directory.
    env.update({"JUST_JUSTFILE": str(ROOT / "Justfile"), "JUST_WORKING_DIRECTORY": str(tmp_path)})
    subprocess.run(["just", "coverage-data"], cwd=tmp_path, env=env, capture_output=True, text=True, check=True)
    invocations = [cast(list[str], json.loads(line)) for line in invocation_log.read_text().splitlines()]
    assert len(invocations) == 2
    assert all(command[:2] == ["run", "pytest"] for command in invocations)
    executed_files = [argument for command in invocations for argument in command if argument.startswith("tests/")]
    assert executed_files == expected_files
    assert "--cov-append" not in invocations[0]
    assert "--cov-append" in invocations[1]


def test_changed_file_lookup_failure_cannot_skip_heavy_ci(tmp_path: Path) -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    filter_step = workflow.partition("id: filter")[2].partition("run: |")[2]
    script_lines = []
    for line in filter_step.splitlines():
        if line and not line.startswith("          "):
            break
        script_lines.append(line[10:] if line else "")
    script = textwrap.dedent("\n".join(script_lines)).replace("${{ github.repository }}", "example/repo")
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'case "$GH_MODE" in\n'
        "  failure) exit 22 ;;\n"
        "  docs) printf '%s\\n' README.md docs/guide.md ;;\n"
        "  code) printf '%s\\n' README.md src/module.py ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)

    def run_filter(mode: str) -> subprocess.CompletedProcess[str]:
        output = tmp_path / f"{mode}.output"
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{tmp_path}:{env['PATH']}",
                "GITHUB_OUTPUT": str(output),
                "EVENT_NAME": "pull_request",
                "PR_NUMBER": "123",
                "GH_MODE": mode,
            }
        )
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=False)

    failed = run_filter("failure")
    assert failed.returncode != 0
    failure_output = tmp_path / "failure.output"
    assert not failure_output.exists() or "run_heavy=false" not in failure_output.read_text(encoding="utf-8")

    docs = run_filter("docs")
    assert docs.returncode == 0
    assert (tmp_path / "docs.output").read_text(encoding="utf-8") == "run_heavy=false\n"

    code = run_filter("code")
    assert code.returncode == 0
    assert (tmp_path / "code.output").read_text(encoding="utf-8") == "run_heavy=true\n"
