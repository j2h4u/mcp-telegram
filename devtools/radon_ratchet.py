"""Monotonic Radon cyclomatic-complexity gate (policy cutoff B=10)."""

import argparse
import json
from pathlib import Path
from typing import Any

from radon.complexity import cc_visit

THRESHOLD = 10
BASELINE_VERSION = 1


def _blocks(blocks: list[Any], prefix: tuple[str, ...] = ()) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for block in blocks:
        name = ".".join((*prefix, block.name))
        if hasattr(block, "methods"):
            out.extend(_blocks(list(getattr(block, "inner_classes", ())), (*prefix, block.name)))
            out.extend(_blocks(list(block.methods), (*prefix, block.name)))
        else:
            out.append((name, block))
            out.extend(_blocks(list(getattr(block, "closures", ())), (*prefix, block.name)))
    return out


def collect(src: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src).as_posix()
        for name, block in _blocks(list(cc_visit(path.read_text(encoding="utf-8")))):
            result[f"{rel}::{name}"] = int(block.complexity)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", type=Path, default=Path("src/mcp_telegram"))
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--threshold", type=int, default=THRESHOLD)
    parser.add_argument("--tighten-baseline", action="store_true")
    args = parser.parse_args(argv)
    current = collect(args.src)
    baseline: dict[str, int] = {}
    if args.baseline.exists():
        raw = json.loads(args.baseline.read_text(encoding="utf-8"))
        if raw.get("version", BASELINE_VERSION) != BASELINE_VERSION:
            raise ValueError("unsupported Radon baseline version")
        source_root = raw.get("source_root")
        if source_root is not None and Path(source_root).as_posix() != args.src.as_posix():
            print("Radon complexity ratchet failed: baseline source_root does not match --src")
            return 1
        baseline_threshold = raw.get("threshold")
        if baseline_threshold is not None and int(baseline_threshold) != args.threshold:
            print("Radon complexity ratchet failed: baseline threshold does not match --threshold")
            return 1
        baseline = {k: int(v) for k, v in raw.get("functions", {}).items()}
    issues: list[str] = []
    for key, complexity in current.items():
        cap = baseline.get(key)
        if cap is None and complexity > args.threshold:
            issues.append(f"{key} complexity {complexity} exceeds {args.threshold}")
        elif cap is not None and complexity > args.threshold and complexity > cap:
            issues.append(f"{key} complexity {complexity} exceeds baseline {cap}")
    if issues:
        print(f"Radon complexity ratchet failed: {len(issues)} issue(s)")
        print("\n".join(f"  {issue}" for issue in issues[:20]))
        return 1
    if args.tighten_baseline:
        entries = {k: c for k, c in current.items() if c > args.threshold}
        entries = {k: min(c, baseline[k]) if k in baseline else c for k, c in entries.items()}
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": BASELINE_VERSION,
            "source_root": args.src.as_posix(),
            "threshold": args.threshold,
            "functions": dict(sorted(entries.items())),
        }
        args.baseline.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Radon complexity ratchet passed: {len(current)} function(s) checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
