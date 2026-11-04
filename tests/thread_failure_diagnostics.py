"""Bounded, failure-only snapshot of this pytest process's native thread resources."""

from __future__ import annotations

import os
import resource
import threading
from pathlib import Path
from typing import cast

_UNAVAILABLE = "unavailable"
_THREAD_LIMIT = 32
_FIELDS = ("Threads", "VmSize", "VmRSS", "VmPeak", "VmData", "VmStk")
_MEMORY_FIELDS = ("MemAvailable", "CommitLimit", "Committed_AS")


def is_thread_start_failure(error: BaseException) -> bool:
    pending = [error]
    seen: set[int] = set()
    while pending and len(seen) < 64:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if type(current) is RuntimeError and str(current) == "can't start new thread":
            return True
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False


def _read(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8") as stream:
            return stream.read(65536)
    except OSError, UnicodeError:
        return None


def _selected_fields(path: Path, fields: tuple[str, ...]) -> dict[str, str]:
    content = _read(path)
    values = {} if content is None else dict(line.split(":", 1) for line in content.splitlines() if ":" in line)
    return {field: values.get(field, _UNAVAILABLE).strip()[:128] for field in fields}


def _unescape_mount(value: str) -> str:
    for escaped, decoded in ((r"\040", " "), (r"\011", "\t"), (r"\012", "\n"), (r"\134", "\\")):
        value = value.replace(escaped, decoded)
    return value


def _cgroup_directory() -> Path | None:
    membership = _read(Path("/proc/self/cgroup"))
    mounts = _read(Path("/proc/self/mountinfo"))
    if membership is None or mounts is None:
        return None
    groups = [line[3:] for line in membership.splitlines() if line.startswith("0::/")]
    if len(groups) != 1:
        return None
    group = Path(groups[0])
    if ".." in group.parts:
        return None
    candidates = [candidate for line in mounts.splitlines() if (candidate := _cgroup_mount(line, group)) is not None]
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _cgroup_mount(line: str, group: Path) -> tuple[int, Path] | None:
    left, separator, right = line.partition(" - ")
    fields = left.split()
    if not separator or not right.startswith("cgroup2 ") or len(fields) < 5:
        return None
    root = Path(_unescape_mount(fields[3]))
    try:
        relative = group.relative_to(root)
    except ValueError:
        return None
    return len(root.parts), Path(_unescape_mount(fields[4])) / relative


def _cgroup_snapshot() -> dict[str, object]:
    directory = _cgroup_directory()
    result: dict[str, object] = {"path": str(directory) if directory is not None else _UNAVAILABLE}
    for name in ("pids.current", "pids.max", "pids.events", "memory.current", "memory.max", "memory.events"):
        value = _read(directory / name) if directory is not None else None
        result[name] = value.strip()[:1024] if value is not None else _UNAVAILABLE
    return result


def _limits() -> dict[str, object]:
    result: dict[str, object] = {}
    for name in ("RLIMIT_AS", "RLIMIT_NPROC", "RLIMIT_STACK"):
        try:
            result[name] = list(resource.getrlimit(cast(int, getattr(resource, name))))
        except AttributeError, OSError, ValueError:
            result[name] = _UNAVAILABLE
    return result


def collect_thread_resources(node_id: str, phase: str) -> dict[str, object]:
    threads = threading.enumerate()
    return {
        "snapshot": "post_exception_unwind",
        "node": node_id[:512],
        "phase": phase[:32],
        "pid": os.getpid(),
        "limits": _limits(),
        "process": _selected_fields(Path("/proc/self/status"), _FIELDS),
        "python_thread_count": len(threads),
        "threads": [{"name": thread.name[:80], "native_id": thread.native_id} for thread in threads[:_THREAD_LIMIT]],
        "threads_truncated": len(threads) > _THREAD_LIMIT,
        "memory": _selected_fields(Path("/proc/meminfo"), _MEMORY_FIELDS),
        "cgroup_v2": _cgroup_snapshot(),
    }
