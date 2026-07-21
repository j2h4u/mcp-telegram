import json
from pathlib import Path

import pytest
from devtools.crap_ratchet import main


def _report(tmp_path: Path, *, covered: int = 0) -> tuple[Path, Path, Path]:
    src = tmp_path / "src" / "mcp_telegram"
    src.mkdir(parents=True)
    source = src / "sample.py"
    source.write_text("def f(value):\n    if value: return 1\n    return 2\n", encoding="utf-8")
    coverage = tmp_path / "coverage.json"
    coverage.write_text(
        json.dumps(
            {"files": {str(source): {"functions": {"f": {"summary": {"covered_lines": covered, "num_statements": 2}}}}}}
        )
    )
    return src, coverage, tmp_path / "baseline.json"


def test_write_baseline_is_disabled(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src, coverage, baseline = _report(tmp_path)
    assert main(["--coverage", str(coverage), "--baseline", str(baseline), "--src", str(src), "--write-baseline"]) == 2
    assert not baseline.exists()
    assert "disabled" in capsys.readouterr().out


def test_v1_check_is_non_mutating_and_filters_healthy_entries(tmp_path: Path) -> None:
    src, coverage, baseline = _report(tmp_path, covered=2)
    before = {"version": 1, "source_root": str(src), "functions": {"sample.py::f": {"crap": 1.0}}}
    baseline.write_text(json.dumps(before))
    assert main(["--coverage", str(coverage), "--baseline", str(baseline), "--src", str(src)]) == 0
    assert json.loads(baseline.read_text()) == before
