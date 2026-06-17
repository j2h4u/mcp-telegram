import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from radon.complexity import cc_visit

DEFAULT_THRESHOLD = 30.0
DEFAULT_EPSILON = 0.01
DEFAULT_SOURCE_ROOT = Path("src/mcp_telegram")


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


def _round_metric(value: float) -> float:
    return round(value, 6)


def _qualname_from_block(block: Any, prefix: tuple[str, ...] = ()) -> list[tuple[str, Any]]:
    if hasattr(block, "methods"):
        class_prefix = prefix + (block.name,)
        items: list[tuple[str, Any]] = []
        for inner_class in block.inner_classes:
            items.extend(_qualname_from_block(inner_class, class_prefix))
        for method in block.methods:
            items.extend(_qualname_from_block(method, class_prefix))
        return items

    qualname = ".".join((*prefix, block.name))
    items = [(qualname, block)]
    for closure in block.closures:
        items.extend(_qualname_from_block(closure, prefix + (block.name,)))
    return items


def _load_coverage_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_baseline(path: Path) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("functions", {})
    if not isinstance(entries, dict):
        raise ValueError("baseline file does not contain an object at 'functions'")
    return entries


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
    coverage_report: dict[str, Any],
    source_root: Path,
) -> list[FunctionMetric]:
    source_root = source_root.resolve()
    files = coverage_report.get("files", {})
    if not isinstance(files, dict):
        raise ValueError("coverage report does not contain an object at 'files'")

    metrics: list[FunctionMetric] = []
    for raw_path, file_data in files.items():
        file_path = Path(raw_path).resolve()
        if source_root not in file_path.parents and file_path != source_root:
            continue

        relative_path = file_path.relative_to(source_root).as_posix()
        source_text = file_path.read_text(encoding="utf-8")
        blocks = cc_visit(source_text)
        functions = file_data.get("functions", {})
        if not isinstance(functions, dict):
            raise ValueError(f"coverage report for {raw_path} does not contain function data")

        for qualname, block in _qualname_from_block_list(blocks):
            if qualname not in functions:
                continue
            coverage_entry = functions[qualname]
            if not isinstance(coverage_entry, dict):
                raise ValueError(f"coverage report entry for {raw_path}::{qualname} is invalid")
            summary = coverage_entry.get("summary", {})
            if not isinstance(summary, dict):
                raise ValueError(f"coverage summary for {raw_path}::{qualname} is invalid")
            num_statements = int(summary.get("num_statements", 0))
            covered_lines = int(summary.get("covered_lines", 0))
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


def _qualname_from_block_list(blocks: list[Any]) -> list[tuple[str, Any]]:
    items: list[tuple[str, Any]] = []
    for block in blocks:
        items.extend(_qualname_from_block(block))
    return items


def _compare_metrics(
    current: list[FunctionMetric],
    baseline: dict[str, dict[str, Any]],
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

        baseline_crap = float(baseline_entry.get("crap", 0.0))
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
        return (
            f"{issue.key} CRAP {issue.baseline_crap:.2f} -> {issue.current_crap:.2f} "
            f"(+{issue.delta:.2f})"
        )
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


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

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
