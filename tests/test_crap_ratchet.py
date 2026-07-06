import json
import textwrap
from pathlib import Path
from typing import TypedDict, cast

from devtools.crap_ratchet import main
from pytest import CaptureFixture


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


def _write_extended_report_for_new_offender(path: Path, source_path: Path) -> None:
    report = {
        "files": {
            str(source_path): {
                "functions": {
                    "simple": {
                        "summary": {"covered_lines": 2, "num_statements": 4},
                    },
                    "complex": {
                        "summary": {"covered_lines": 0, "num_statements": 1},
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


def test_tighten_baseline_only_decreases_existing_crap(tmp_path: Path) -> None:
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
    baseline["functions"][simple_key]["crap"] += 10.0
    baseline_path.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    exit_code = main(
        [
            "--coverage",
            str(coverage_path),
            "--baseline",
            str(baseline_path),
            "--src",
            str(source_root),
            "--tighten-baseline",
            "--threshold",
            "30",
        ],
    )

    tightened = cast(BaselineReport, json.loads(baseline_path.read_text(encoding="utf-8")))
    assert exit_code == 0
    assert tightened["functions"][simple_key]["crap"] < baseline["functions"][simple_key]["crap"]


def test_tighten_baseline_leaves_unchanged_functions_when_not_worse(tmp_path: Path) -> None:
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
            ]
        )
        == 0
    )

    baseline = cast(BaselineReport, json.loads(baseline_path.read_text(encoding="utf-8")))
    simple_key = "sample.py::simple"
    simple_crap_before = baseline["functions"][simple_key]["crap"]

    assert (
        main(
            [
                "--coverage",
                str(coverage_path),
                "--baseline",
                str(baseline_path),
                "--src",
                str(source_root),
                "--tighten-baseline",
            ]
        )
        == 0
    )

    tightened = cast(BaselineReport, json.loads(baseline_path.read_text(encoding="utf-8")))
    assert tightened["functions"][simple_key]["crap"] == simple_crap_before


def test_tighten_baseline_rejects_regressions(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
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
            ]
        )
        == 0
    )

    baseline = cast(BaselineReport, json.loads(baseline_path.read_text(encoding="utf-8")))
    simple_key = "sample.py::simple"
    baseline["functions"][simple_key]["crap"] = 0.1
    baseline_path.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    exit_code = main(
        [
            "--coverage",
            str(coverage_path),
            "--baseline",
            str(baseline_path),
            "--src",
            str(source_root),
            "--tighten-baseline",
            "--epsilon",
            "0.0",
        ],
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "CRAP ratchet failed: 1 issue(s)" in captured.out
    assert f"{simple_key} CRAP 0.10 -> 2.50 (+2.40)" in captured.out


def test_ratchet_defaults_to_0_5_epsilon(tmp_path: Path) -> None:
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
            ]
        )
        == 0
    )

    baseline = cast(BaselineReport, json.loads(baseline_path.read_text(encoding="utf-8")))
    simple_key = "sample.py::simple"
    baseline["functions"][simple_key]["crap"] -= 0.4
    baseline_path.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    exit_code = main(
        [
            "--coverage",
            str(coverage_path),
            "--baseline",
            str(baseline_path),
            "--src",
            str(source_root),
        ]
    )
    assert exit_code == 0


def test_ratchet_default_epsilon_catches_large_drift(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
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
            ]
        )
        == 0
    )

    baseline = cast(BaselineReport, json.loads(baseline_path.read_text(encoding="utf-8")))
    simple_key = "sample.py::simple"
    baseline["functions"][simple_key]["crap"] -= 0.6
    baseline_path.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    exit_code = main(
        [
            "--coverage",
            str(coverage_path),
            "--baseline",
            str(baseline_path),
            "--src",
            str(source_root),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "CRAP ratchet failed: 1 issue(s)" in captured.out
    assert simple_key in captured.out


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
    baseline["functions"][simple_key]["crap"] -= 0.6
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


def test_tighten_baseline_rejects_new_offender(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    source_root, coverage_path, baseline_path = _write_source_tree(tmp_path)
    source_path = source_root / "sample.py"
    _write_extended_report_for_new_offender(coverage_path, source_path)

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
    baseline["functions"].clear()
    baseline_path.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    exit_code = main(
        [
            "--coverage",
            str(coverage_path),
            "--baseline",
            str(baseline_path),
            "--src",
            str(source_root),
            "--tighten-baseline",
            "--threshold",
            "3",
        ],
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "CRAP ratchet failed: 1 issue(s)" in captured.out
    assert "exceeds threshold" in captured.out
