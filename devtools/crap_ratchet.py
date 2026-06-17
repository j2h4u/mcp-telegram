import argparse
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypedDict, TypeGuard, cast

from radon.complexity import cc_visit

DEFAULT_THRESHOLD = 30.0
DEFAULT_EPSILON = 0.01
DEFAULT_SOURCE_ROOT = Path("src/mcp_telegram")


class _FunctionSummary(TypedDict):
    covered_lines: int
    num_statements: int


class _CoverageFunctionEntry(TypedDict):
    summary: _FunctionSummary


class _CoverageFileEntry(TypedDict):
    functions: dict[str, _CoverageFunctionEntry]


class _CoverageReport(TypedDict):
    files: dict[str, _CoverageFileEntry]


class _BaselineFunctionEntry(TypedDict, total=False):
    path: str
    qualname: str
    start_line: int
    end_line: int
    complexity: int
    coverage_fraction: float
    crap: float


class _BaselineReport(TypedDict):
    version: int
    source_root: str
    threshold: float
    functions: dict[str, _BaselineFunctionEntry]


class _RadonBlock(Protocol):
    name: str
    lineno: int
    endline: int
    complexity: int


class _RadonFunctionBlock(_RadonBlock, Protocol):
    closures: Sequence[_RadonFunctionBlock]


class _RadonClassBlock(_RadonBlock, Protocol):
    inner_classes: Sequence[_RadonClassBlock]
    methods: Sequence[_RadonFunctionBlock]


class _RatchetArgs(Protocol):
    coverage: Path
    baseline: Path
    src: Path
    threshold: float
    epsilon: float
    write_baseline: bool


@dataclass(frozen=True, slots=True)
class FunctionMetric:
    key: str
    path: str
    qualname: str
    start_line: int
    end_line: int
    complexity: int
    coverage_fraction: float
    crap: float


@dataclass(frozen=True, slots=True)
class RatchetIssue:
    key: str
    kind: str
    current_crap: float
    baseline_crap: float | None
    delta: float | None


RadonVisit = Callable[[str], Sequence[_RadonBlock]]
_CC_VISIT: RadonVisit = cast(RadonVisit, cc_visit)


def _round_metric(value: float) -> float:
    return round(value, 6)


def _expect_dict(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(context)
    return cast(dict[str, object], value)


def _expect_str(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise ValueError(context)
    return value


def _expect_int(value: object, context: str) -> int:
    if not isinstance(value, int):
        raise ValueError(context)
    return value


def _expect_float(value: object, context: str) -> float:
    if not isinstance(value, (int, float)):
        raise ValueError(context)
    return float(value)


def _is_class_block(block: _RadonBlock) -> TypeGuard[_RadonClassBlock]:
    return hasattr(block, "methods") and hasattr(block, "inner_classes")


def _qualname_from_block(
    block: _RadonBlock,
    prefix: tuple[str, ...] = (),
) -> list[tuple[str, _RadonBlock]]:
    if _is_class_block(block):
        class_prefix = (*prefix, block.name)
        items: list[tuple[str, _RadonBlock]] = []
        for inner_class in block.inner_classes:
            items.extend(_qualname_from_block(inner_class, class_prefix))
        for method in block.methods:
            items.extend(_qualname_from_block(method, class_prefix))
        return items

    function_block = cast(_RadonFunctionBlock, block)
    qualname = ".".join((*prefix, function_block.name))
    items = [(qualname, function_block)]
    for closure in function_block.closures:
        items.extend(_qualname_from_block(closure, (*prefix, function_block.name)))
    return items


def _load_coverage_report(path: Path) -> _CoverageReport:
    raw_report = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    files = _expect_dict(raw_report.get("files"), "coverage report does not contain an object at 'files'")
    return cast(_CoverageReport, {"files": files})


def _load_baseline(path: Path) -> dict[str, _BaselineFunctionEntry]:
    raw_report = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    entries = raw_report.get("functions", {})
    if not isinstance(entries, dict):
        raise ValueError("baseline file does not contain an object at 'functions'")
    return cast(dict[str, _BaselineFunctionEntry], entries)


def _save_baseline(path: Path, source_root: Path, threshold: float, metrics: list[FunctionMetric]) -> None:
    payload = {
        "version": 1,
        "source_root": source_root.as_posix(),
        "threshold": threshold,
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
            for metric in sorted(metrics, key=lambda item: item.key)
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _function_metrics_from_report(
    coverage_report: _CoverageReport,
    source_root: Path,
) -> list[FunctionMetric]:
    source_root = source_root.resolve()
    files = coverage_report["files"]

    metrics: list[FunctionMetric] = []
    for raw_path, file_data in files.items():
        raw_path = _expect_str(raw_path, "coverage report file path is invalid")
        file_path = Path(raw_path).resolve()
        if source_root not in file_path.parents and file_path != source_root:
            continue

        relative_path = file_path.relative_to(source_root).as_posix()
        source_text = file_path.read_text(encoding="utf-8")
        blocks = _CC_VISIT(source_text)
        functions = file_data["functions"]

        for qualname, block in _qualname_from_block_list(blocks):
            if qualname not in functions:
                continue
            coverage_entry = functions[qualname]
            summary = coverage_entry["summary"]
            num_statements = _expect_int(
                summary["num_statements"],
                f"coverage summary for {raw_path}::{qualname} has invalid num_statements",
            )
            covered_lines = _expect_int(
                summary["covered_lines"],
                f"coverage summary for {raw_path}::{qualname} has invalid covered_lines",
            )
            coverage_fraction = 1.0 if num_statements <= 0 else covered_lines / num_statements
            crap = (block.complexity**2) * ((1 - coverage_fraction) ** 3) + block.complexity
            metrics.append(
                FunctionMetric(
                    key=f"{relative_path}::{qualname}",
                    path=relative_path,
                    qualname=qualname,
                    start_line=block.lineno,
                    end_line=block.endline,
                    complexity=block.complexity,
                    coverage_fraction=_round_metric(coverage_fraction),
                    crap=_round_metric(crap),
                )
            )
    return metrics


def _qualname_from_block_list(blocks: Sequence[_RadonBlock]) -> list[tuple[str, _RadonBlock]]:
    items: list[tuple[str, _RadonBlock]] = []
    for block in blocks:
        items.extend(_qualname_from_block(block))
    return items


def _compare_metrics(
    current: list[FunctionMetric],
    baseline: dict[str, _BaselineFunctionEntry],
    threshold: float,
    epsilon: float,
) -> list[RatchetIssue]:
    issues: list[RatchetIssue] = []
    for metric in current:
        baseline_entry = baseline.get(metric.key)
        if baseline_entry is None:
            if metric.crap > threshold + epsilon:
                issues.append(
                    RatchetIssue(
                        key=metric.key,
                        kind="new-offender",
                        current_crap=metric.crap,
                        baseline_crap=None,
                        delta=None,
                    ),
                )
            continue

        baseline_crap = _expect_float(
            baseline_entry.get("crap", 0.0),
            f"baseline entry for {metric.key} has invalid crap",
        )
        delta = metric.crap - baseline_crap
        if delta > epsilon:
            issues.append(
                RatchetIssue(
                    key=metric.key,
                    kind="regression",
                    current_crap=metric.crap,
                    baseline_crap=baseline_crap,
                    delta=delta,
                ),
            )

    issues.sort(
        key=lambda issue: (
            0 if issue.kind == "regression" else 1,
            -(issue.delta or 0.0),
            -issue.current_crap,
            issue.key,
        ),
    )
    return issues


def _format_issue(issue: RatchetIssue) -> str:
    if issue.kind == "regression":
        return f"{issue.key} CRAP {issue.baseline_crap:.2f} -> {issue.current_crap:.2f} (+{issue.delta:.2f})"
    return f"{issue.key} CRAP {issue.current_crap:.2f} exceeds threshold"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate or enforce a CRAP ratchet.")
    parser.add_argument("--coverage", type=Path, required=True, help="pytest-cov JSON report")
    parser.add_argument(
        "--baseline",
        type=Path,
        required=True,
        help="tracked baseline JSON file",
    )
    parser.add_argument(
        "--src",
        type=Path,
        default=DEFAULT_SOURCE_ROOT,
        help="source root to scan",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help="CRAP threshold for new functions",
    )
    parser.add_argument(
        "--epsilon",
        type=float,
        default=DEFAULT_EPSILON,
        help="allowed drift above baseline",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="regenerate the baseline from the current report",
    )
    return parser


def _parse_args(argv: list[str] | None) -> _RatchetArgs:
    parser = _build_parser()
    return cast(_RatchetArgs, parser.parse_args(argv))


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    coverage_report = _load_coverage_report(args.coverage)
    source_root = args.src.resolve()
    metrics = _function_metrics_from_report(coverage_report, source_root)

    if args.write_baseline:
        _save_baseline(args.baseline, source_root, args.threshold, metrics)
        print(f"Wrote {len(metrics)} CRAP baseline entries to {args.baseline}")
        return 0

    baseline = _load_baseline(args.baseline)
    issues = _compare_metrics(metrics, baseline, args.threshold, args.epsilon)
    if issues:
        print(f"CRAP ratchet failed: {len(issues)} issue(s)")
        for issue in issues[:20]:
            print(f"  {_format_issue(issue)}")
        return 1

    print(f"CRAP ratchet passed: {len(metrics)} function(s) within baseline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
