from pathlib import Path

import pytest
from devtools.radon_threshold import LIMIT, collect, main, violations


def _source(tmp_path: Path, branches: int) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    statements = "".join(f"    if value > {i}: value += 1\n" for i in range(branches - 1))
    (src / "sample.py").write_text(
        f"def sample(value):\n{statements}    return value\n",
        encoding="utf-8",
    )
    return src


def test_collect_reports_function_complexity(tmp_path: Path) -> None:
    src = _source(tmp_path, LIMIT)
    assert collect(src)["sample.py::sample"] == LIMIT


def test_collect_includes_method_of_function_local_class(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    branches = "".join(f"            if value > {i}: value += 1\n" for i in range(LIMIT))
    (src / "sample.py").write_text(
        "def capture_signals():\n"
        "    class Handler:\n"
        "        def run(self, value):\n"
        f"{branches}"
        "            return value\n"
        "    return Handler\n",
        encoding="utf-8",
    )

    metrics = collect(src)

    assert metrics["sample.py::capture_signals.Handler.run"] == LIMIT + 1
    assert violations(metrics) == [("sample.py::capture_signals.Handler.run", LIMIT + 1)]


def test_limit_value_passes_and_above_limit_fails_without_history() -> None:
    assert violations({"sample.py::sample": LIMIT}) == []
    assert violations({"sample.py::sample": LIMIT + 1}) == [("sample.py::sample", LIMIT + 1)]


def test_cli_failure_names_function_value_and_limit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = _source(tmp_path, LIMIT + 1)
    assert main(["--src", str(src)]) == 1
    output = capsys.readouterr().out
    assert "sample.py::sample CC 11 exceeds limit 10" in output
