"""Enforce the repository-wide Radon cyclomatic complexity limit."""

import argparse
from pathlib import Path
from typing import Protocol, cast

from .function_complexity import collect_function_complexities

LIMIT = 10


class _Args(Protocol):
    src: Path


def collect(src: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for path in sorted(src.rglob("*.py")):
        relative_path = path.relative_to(src).as_posix()
        for block in collect_function_complexities(path.read_text(encoding="utf-8")):
            key = f"{relative_path}::{block.name}"
            result[key] = max(result.get(key, 0), block.complexity)
    return result


def violations(metrics: dict[str, int]) -> list[tuple[str, int]]:
    return [(name, value) for name, value in sorted(metrics.items()) if value > LIMIT]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Enforce Radon CC <= {LIMIT}.")
    parser.add_argument("--src", type=Path, default=Path("src/mcp_telegram"))
    args = cast(_Args, parser.parse_args(argv))

    current = collect(args.src)
    issues = violations(current)
    if issues:
        print(f"Radon CC threshold failed: {len(issues)} function(s) exceed limit {LIMIT}")
        for name, value in issues[:20]:
            print(f"  {name} CC {value} exceeds limit {LIMIT}")
        return 1

    print(f"Radon CC threshold passed: {len(current)} function(s) at or below limit {LIMIT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
