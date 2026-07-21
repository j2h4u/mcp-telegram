import json
import shutil
import textwrap
from pathlib import Path
from typing import TypedDict, cast

import pytest
from devtools.crap_ratchet import (
    _function_metrics_from_report,
    _load_coverage_report,
    _normalized_source_root,
    main,
)
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


def _seed_baseline(coverage_path: Path, source_root: Path, baseline_path: Path) -> None:
    """Seed a synthetic v2 debt-only baseline for ratchet scenarios."""
    metrics = _function_metrics_from_report(_load_coverage_report(coverage_path), source_root)
    payload = {
        "version": 2,
        "source_root": _normalized_source_root(source_root, baseline_path),
        "threshold": 30.0,
        "epsilon": 0.5,
        "functions": {
            metric.key: {
                "path": metric.path,
                "qualname": metric.qualname,
                "start_line": metric.start_line,
                "end_line": metric.end_line,
                "complexity": metric.complexity,
                "coverage_fraction": metric.coverage_fraction,
                "crap": metric.crap,
            }
            for metric in metrics
            if metric.crap > 30.0
        },
    }
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_write_baseline_is_disabled(tmp_path: Path) -> None:
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

    assert exit_code == 2
    assert not baseline_path.exists()


def test_tighten_baseline_only_decreases_existing_crap(tmp_path: Path) -> None:
    source_root, coverage_path, baseline_path = _write_source_tree(tmp_path)
    source_path = source_root / "sample.py"
    _write_report(coverage_path, source_path)

    _seed_baseline(coverage_path, source_root, baseline_path)

    baseline = cast(BaselineReport, json.loads(baseline_path.read_text(encoding="utf-8")))
    simple_key = "sample.py::complex"
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

    _seed_baseline(coverage_path, source_root, baseline_path)

    baseline = cast(BaselineReport, json.loads(baseline_path.read_text(encoding="utf-8")))
    simple_key = "sample.py::complex"
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

    _seed_baseline(coverage_path, source_root, baseline_path)

    baseline = cast(BaselineReport, json.loads(baseline_path.read_text(encoding="utf-8")))
    simple_key = "sample.py::complex"
    baseline["functions"][simple_key]["crap"] = 31.0
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
        ],
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "CRAP ratchet failed: 1 issue(s)" in captured.out
    assert f"{simple_key} CRAP 31.00 -> 72.00 (+41.00)" in captured.out


def test_ratchet_defaults_to_0_5_epsilon(tmp_path: Path) -> None:
    source_root, coverage_path, baseline_path = _write_source_tree(tmp_path)
    source_path = source_root / "sample.py"
    _write_report(coverage_path, source_path)

    _seed_baseline(coverage_path, source_root, baseline_path)

    baseline = cast(BaselineReport, json.loads(baseline_path.read_text(encoding="utf-8")))
    simple_key = "sample.py::complex"
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

    _seed_baseline(coverage_path, source_root, baseline_path)

    baseline = cast(BaselineReport, json.loads(baseline_path.read_text(encoding="utf-8")))
    simple_key = "sample.py::complex"
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


def test_ratchet_flags_new_offender(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    source_root, coverage_path, baseline_path = _write_source_tree(tmp_path)
    source_path = source_root / "sample.py"
    _write_report(coverage_path, source_path)

    _seed_baseline(coverage_path, source_root, baseline_path)

    baseline = cast(BaselineReport, json.loads(baseline_path.read_text(encoding="utf-8")))
    complex_key = "sample.py::complex"
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
    assert "CRAP ratchet failed: 1 issue(s)" in captured.out
    assert complex_key in captured.out


def test_v2_baseline_survives_relocated_checkout(tmp_path: Path) -> None:
    original = tmp_path / "original"
    source_root, coverage_path, baseline_path = _write_source_tree(original)
    _write_report(coverage_path, source_root / "sample.py")
    _seed_baseline(coverage_path, source_root, baseline_path)

    relocated = tmp_path / "relocated"
    shutil.copytree(original, relocated)
    relocated_source = relocated / "src" / "mcp_telegram"
    relocated_coverage = relocated / "coverage.json"
    relocated_baseline = relocated / "reports" / "crap-baseline.json"
    _write_report(relocated_coverage, relocated_source / "sample.py")

    assert (
        main(
            [
                "--coverage",
                str(relocated_coverage),
                "--baseline",
                str(relocated_baseline),
                "--src",
                str(relocated_source),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--coverage",
                str(relocated_coverage),
                "--baseline",
                str(relocated_baseline),
                "--src",
                str(relocated_source),
                "--tighten-baseline",
            ]
        )
        == 0
    )
    persisted = cast(BaselineReport, json.loads(relocated_baseline.read_text(encoding="utf-8")))
    assert persisted["source_root"] == "src/mcp_telegram"


def test_v2_baseline_rejects_non_legacy_entry(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    source_root, coverage_path, baseline_path = _write_source_tree(tmp_path)
    _write_report(coverage_path, source_root / "sample.py")
    _seed_baseline(coverage_path, source_root, baseline_path)
    baseline = cast(dict[str, object], json.loads(baseline_path.read_text(encoding="utf-8")))
    functions = cast(dict[str, object], baseline["functions"])
    functions["sample.py::simple"] = {"crap": 30.0}
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")

    assert main(["--coverage", str(coverage_path), "--baseline", str(baseline_path), "--src", str(source_root)]) == 1
    assert "must have crap > threshold (30)" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("source_root", "elsewhere", "baseline source_root must match --src (src/mcp_telegram)"),
        ("threshold", 29.0, "baseline threshold must match --threshold (30)"),
        ("epsilon", 0.25, "baseline epsilon must match --epsilon (0.5)"),
    ],
)
def test_v2_baseline_rejects_policy_metadata_mismatch(
    tmp_path: Path,
    capsys: CaptureFixture[str],
    field: str,
    value: object,
    expected: str,
) -> None:
    source_root, coverage_path, baseline_path = _write_source_tree(tmp_path)
    _write_report(coverage_path, source_root / "sample.py")
    _seed_baseline(coverage_path, source_root, baseline_path)
    baseline = cast(dict[str, object], json.loads(baseline_path.read_text(encoding="utf-8")))
    baseline[field] = value
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")

    assert main(["--coverage", str(coverage_path), "--baseline", str(baseline_path), "--src", str(source_root)]) == 1
    assert expected in capsys.readouterr().out


def test_tighten_baseline_rejects_new_offender(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    source_root, coverage_path, baseline_path = _write_source_tree(tmp_path)
    source_path = source_root / "sample.py"
    _write_extended_report_for_new_offender(coverage_path, source_path)

    _seed_baseline(coverage_path, source_root, baseline_path)

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
            "30",
        ],
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "CRAP ratchet failed: 1 issue(s)" in captured.out
    assert "exceeds threshold" in captured.out
