import json
import textwrap
from pathlib import Path
from typing import TypedDict, cast

from pytest import CaptureFixture

from devtools.crap_ratchet import main


class BaselineFunction(TypedDict):
    crap: float
    coverage_fraction: float


class BaselineReport(TypedDict):
    source_root: str
    functions: dict[str, BaselineFunction]


def _write_source_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    source_root = tmp_path / "src" / "mcp_telegram"
    source_root.mkdir(parents=True)
    source_path = source_root / "sample.py"
    source_path.write_text(
        textwrap.dedent(
            """
            def simple(flag):
                if flag:
                    return 1
                return 2


            def complex(value):
                if value > 0:
                    value += 1
                if value > 1:
                    value += 1
                if value > 2:
                    value += 1
                if value > 3:
                    value += 1
                if value > 4:
                    value += 1
                if value > 5:
                    value += 1
                if value > 6:
                    value += 1
                return value


            class Sample:
                def method(self, flag):
                    if flag:
                        return 1

                    def inner(value):
                        if value:
                            return 2
                        return 3

                    return inner(flag)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    coverage_path = tmp_path / "coverage.json"
    baseline_path = tmp_path / "reports" / "crap-baseline.json"
    return source_root, coverage_path, baseline_path


def _write_report(path: Path, source_path: Path) -> None:
    report = {
        "files": {
            str(source_path): {
                "functions": {
                    "simple": {
                        "summary": {"covered_lines": 2, "num_statements": 4},
                    },
                    "complex": {
                        "summary": {"covered_lines": 0, "num_statements": 9},
                    },
                    "Sample.method": {
                        "summary": {"covered_lines": 2, "num_statements": 7},
                    },
                    "Sample.method.inner": {
                        "summary": {"covered_lines": 0, "num_statements": 3},
                    },
                },
            },
        },
    }
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def test_write_baseline_serializes_function_identity(tmp_path: Path) -> None:
    source_root, coverage_path, baseline_path = _write_source_tree(tmp_path)
    source_path = source_root / "sample.py"
    _write_report(coverage_path, source_path)

    exit_code = main(
        [
            "--coverage",
            str(coverage_path),
            "--baseline",
            str(baseline_path),
            "--src",
            str(source_root),
            "--write-baseline",
        ],
    )

    assert exit_code == 0
    baseline = cast(BaselineReport, json.loads(baseline_path.read_text(encoding="utf-8")))
    assert baseline["source_root"] == source_root.as_posix()
    assert baseline["functions"]["sample.py::simple"]["coverage_fraction"] == 0.5
    assert "sample.py::Sample.method" in baseline["functions"]
    assert "sample.py::Sample.method.inner" in baseline["functions"]


def test_ratchet_flags_regression_and_new_offender(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    source_root, coverage_path, baseline_path = _write_source_tree(tmp_path)
    source_path = source_root / "sample.py"
    _write_report(coverage_path, source_path)

    assert (
        main(
            [
                "--coverage",
                str(coverage_path),
                "--baseline",
                str(baseline_path),
                "--src",
                str(source_root),
                "--write-baseline",
            ],
        )
        == 0
    )

    baseline = cast(BaselineReport, json.loads(baseline_path.read_text(encoding="utf-8")))
    simple_key = "sample.py::simple"
    complex_key = "sample.py::complex"
    baseline["functions"][simple_key]["crap"] -= 0.5
    del baseline["functions"][complex_key]
    baseline_path.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    exit_code = main(
        [
            "--coverage",
            str(coverage_path),
            "--baseline",
            str(baseline_path),
            "--src",
            str(source_root),
            "--threshold",
            "30",
        ],
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "CRAP ratchet failed: 2 issue(s)" in captured.out
    assert simple_key in captured.out
    assert complex_key in captured.out
    assert captured.out.index(simple_key) < captured.out.index(complex_key)
