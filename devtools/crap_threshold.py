"""Enforce the repository-wide CRAP limit using pytest coverage data."""

import argparse
import json
from pathlib import Path
from typing import Protocol, cast

from .function_complexity import collect_function_complexities

LIMIT = 30.0
_MAX_DUPLICATE_COMPLEXITY = 5


class _Args(Protocol):
    coverage: Path
    src: Path


def _expect_dict(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(context)
    return cast(dict[str, object], value)


def _expect_int(value: object, context: str) -> int:
    if type(value) is not int:
        raise ValueError(context)
    return value


def _metrics_for_file(path: Path, relative_path: str, file_data: object | None) -> dict[str, float]:
    blocks = collect_function_complexities(path.read_text(encoding="utf-8"))
    if file_data is None:
        raise ValueError(f"coverage report is missing source file {relative_path}")

    file_object = _expect_dict(file_data, f"coverage report entry for {relative_path} must be an object")
    first_qualname = blocks[0].name if blocks else "<no-functions>"
    functions = _expect_dict(
        file_object.get("functions"),
        f"coverage report is missing function map for {relative_path}::{first_qualname}",
    )
    metrics: dict[str, float] = {}
    complexities: dict[str, int] = {}
    for block in blocks:
        context = f"{relative_path}::{block.name}"
        coverage = _expect_dict(functions.get(block.name), f"coverage report is missing function {context}")
        summary = _expect_dict(coverage.get("summary"), f"coverage report is missing summary for {context}")
        num_statements = _expect_int(summary.get("num_statements"), f"invalid coverage summary for {context}")
        covered_lines = _expect_int(summary.get("covered_lines"), f"invalid coverage summary for {context}")
        if num_statements < 0 or covered_lines < 0 or covered_lines > num_statements:
            raise ValueError(f"invalid coverage counts for {context}")

        coverage_fraction = 1.0 if num_statements == 0 else covered_lines / num_statements
        crap = block.complexity**2 * (1 - coverage_fraction) ** 3 + block.complexity
        key = context
        prior_complexity = complexities.get(key)
        if prior_complexity is not None and max(prior_complexity, block.complexity) > _MAX_DUPLICATE_COMPLEXITY:
            raise ValueError(f"duplicate function identity has complexity > {_MAX_DUPLICATE_COMPLEXITY} for {key}")
        metrics[key] = max(metrics.get(key, 0.0), round(crap, 6))
        complexities[key] = max(prior_complexity or 0, block.complexity)
    return metrics


def collect(coverage_path: Path, src: Path) -> dict[str, float]:
    report = cast(object, json.loads(coverage_path.read_text(encoding="utf-8")))
    report_object = _expect_dict(report, "coverage report must be a JSON object")
    files = _expect_dict(report_object.get("files"), "coverage report does not contain an object at 'files'")

    source_root = src.resolve()
    coverage_by_path: dict[Path, object] = {}
    for raw_path, file_data in files.items():
        if not isinstance(raw_path, str):
            raise ValueError("coverage report contains an invalid file path")
        file_path = Path(raw_path).resolve()
        if source_root in file_path.parents:
            coverage_by_path[file_path] = file_data

    metrics: dict[str, float] = {}
    for file_path in sorted(source_root.rglob("*.py")):
        relative_path = file_path.relative_to(source_root).as_posix()
        if file_path not in coverage_by_path:
            raise ValueError(f"coverage report is missing source file {relative_path}")
        file_metrics = _metrics_for_file(file_path, relative_path, coverage_by_path[file_path])
        metrics.update(file_metrics)

    if not metrics:
        raise ValueError(f"coverage report contains no functions under {source_root}")
    return metrics


def violations(metrics: dict[str, float]) -> list[tuple[str, float]]:
    return [(name, value) for name, value in sorted(metrics.items()) if value > LIMIT]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Enforce CRAP <= {LIMIT:g} using pytest coverage.")
    parser.add_argument("--coverage", type=Path, required=True, help="pytest coverage JSON report")
    parser.add_argument("--src", type=Path, default=Path("src/mcp_telegram"))
    args = cast(_Args, parser.parse_args(argv))

    try:
        current = collect(args.coverage, args.src)
    except (OSError, ValueError) as error:
        print(f"CRAP threshold failed: {error}")
        return 1

    issues = violations(current)
    if issues:
        print(f"CRAP threshold failed: {len(issues)} function(s) exceed limit {LIMIT:g}")
        for name, value in issues[:20]:
            print(f"  {name} CRAP {value:g} exceeds limit {LIMIT:g}")
        return 1

    print(f"CRAP threshold passed: {len(current)} function(s) at or below limit {LIMIT:g}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
