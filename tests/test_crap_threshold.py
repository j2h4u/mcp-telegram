import json
from pathlib import Path

import pytest
from devtools.crap_threshold import LIMIT, collect, main, violations


def _case(tmp_path: Path, complexity: int) -> tuple[Path, Path]:
    src = tmp_path / "src"
    src.mkdir()
    source = src / "sample.py"
    branches = "".join(f"    if value > {index}: value += 1\n" for index in range(complexity - 1))
    source.write_text(f"def sample(value):\n{branches}    return value\n", encoding="utf-8")
    coverage = tmp_path / "coverage.json"
    coverage.write_text(
        json.dumps(
            {
                "files": {
                    str(source): {
                        "functions": {
                            "sample": {"summary": {"covered_lines": 0, "num_statements": 1}},
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return src, coverage


def test_coverage_report_produces_crap_metric(tmp_path: Path) -> None:
    src, coverage = _case(tmp_path, complexity=5)
    assert collect(coverage, src)["sample.py::sample"] == LIMIT


def test_collect_matches_class_method_and_nested_closure_coverage_keys(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    source = src / "sample.py"
    source.write_text(
        "class Example:\n"
        "    def run(self, value):\n"
        "        def inner():\n"
        "            if value:\n"
        "                return 1\n"
        "            return 0\n"
        "        return inner()\n",
        encoding="utf-8",
    )
    coverage = tmp_path / "coverage.json"
    coverage.write_text(
        json.dumps(
            {
                "files": {
                    str(source): {
                        "functions": {
                            "Example.run": {"summary": {"covered_lines": 1, "num_statements": 3}},
                            "Example.run.inner": {"summary": {"covered_lines": 2, "num_statements": 3}},
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    metrics = collect(coverage, src)

    assert set(metrics) == {"sample.py::Example.run", "sample.py::Example.run.inner"}


def test_duplicate_canonical_identity_uses_worst_crap_metric(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    source = src / "sample.py"
    source.write_text(
        "class Example:\n"
        "    def run(self, value):\n"
        "        if value:\n"
        "            return 1\n"
        "        if not value:\n"
        "            return 0\n"
        "        return 2\n"
        "        return 0\n"
        "\n"
        "    def run(self, value):\n"
        "        if value:\n"
        "            return 1\n"
        "        return 0\n",
        encoding="utf-8",
    )
    coverage = tmp_path / "coverage.json"
    coverage.write_text(
        json.dumps(
            {
                "files": {
                    str(source): {
                        "functions": {
                            "Example.run": {"summary": {"covered_lines": 0, "num_statements": 4}},
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    metrics = collect(coverage, src)

    assert metrics == {"sample.py::Example.run": 12.0}


def test_local_class_method_and_closure_have_coverage_metrics(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    source = src / "sample.py"
    branches = "".join(f"            if value > {index}: value += 1\n" for index in range(10))
    source.write_text(
        "def capture_signals():\n"
        "    class Handler:\n"
        "        def run(self, value):\n"
        "            def inner():\n"
        "                if value: return 1\n"
        "                return 0\n"
        f"{branches}"
        "            return inner() + value\n"
        "    return Handler\n",
        encoding="utf-8",
    )
    coverage = tmp_path / "coverage.json"
    coverage.write_text(
        json.dumps(
            {
                "files": {
                    str(source): {
                        "functions": {
                            "capture_signals": {"summary": {"covered_lines": 1, "num_statements": 1}},
                            "capture_signals.Handler.run": {"summary": {"covered_lines": 0, "num_statements": 1}},
                            "capture_signals.Handler.run.inner": {
                                "summary": {"covered_lines": 0, "num_statements": 1},
                            },
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    metrics = collect(coverage, src)

    assert metrics["sample.py::capture_signals.Handler.run"] == 132.0
    assert "sample.py::capture_signals.Handler.run.inner" in metrics
    assert violations(metrics) == [("sample.py::capture_signals.Handler.run", 132.0)]


def test_limit_value_passes_and_above_limit_fails_without_history() -> None:
    assert violations({"sample.py::sample": LIMIT}) == []
    assert violations({"sample.py::sample": LIMIT + 0.01}) == [("sample.py::sample", LIMIT + 0.01)]


def test_cli_failure_names_function_value_and_limit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src, coverage = _case(tmp_path, complexity=6)
    assert main(["--src", str(src), "--coverage", str(coverage)]) == 1
    output = capsys.readouterr().out
    assert "sample.py::sample CRAP 42 exceeds limit 30" in output


@pytest.mark.parametrize("incomplete_case", ["missing_summary", "empty_function_map", "missing_function_map"])
def test_cli_rejects_incomplete_function_coverage(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    incomplete_case: str,
) -> None:
    src, coverage_path = _case(tmp_path, complexity=5)
    source_path = src / "sample.py"
    if incomplete_case == "missing_summary":
        file_data: object = {"functions": {"sample": {}}}
    elif incomplete_case == "empty_function_map":
        file_data = {"functions": {}}
    else:
        file_data = {}
    coverage_path.write_text(json.dumps({"files": {str(source_path): file_data}}), encoding="utf-8")

    assert main(["--src", str(src), "--coverage", str(coverage_path)]) == 1
    output = capsys.readouterr().out
    assert "CRAP threshold failed:" in output
    assert "sample.py::sample" in output
