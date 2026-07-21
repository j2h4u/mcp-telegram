import json
from pathlib import Path

from devtools.radon_ratchet import collect, main


def _source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "sample.py").write_text(
        "def offender(value):\n"
        "    if value > 0: value += 1\n"
        "    if value > 1: value += 1\n"
        "    if value > 2: value += 1\n"
        "    if value > 3: value += 1\n"
        "    if value > 4: value += 1\n"
        "    if value > 5: value += 1\n"
        "    if value > 6: value += 1\n"
        "    if value > 7: value += 1\n"
        "    if value > 8: value += 1\n"
        "    return value\n",
        encoding="utf-8",
    )
    return src


def test_collect_includes_full_radon_function_scope(tmp_path: Path) -> None:
    current = collect(_source(tmp_path))
    assert current["sample.py::offender"] == 10


def test_new_complexity_above_ten_fails_without_epsilon(tmp_path: Path) -> None:
    src = _source(tmp_path)
    baseline = tmp_path / "radon.json"
    baseline.write_text(json.dumps({"version": 1, "threshold": 10, "functions": {}}))
    (src / "sample.py").write_text(
        (src / "sample.py")
        .read_text(encoding="utf-8")
        .replace("    return value\n", "    if value > 9: value += 1\n    return value\n"),
        encoding="utf-8",
    )
    assert main(["--src", str(src), "--baseline", str(baseline)]) == 1


def test_tighten_graduates_and_removes_entries(tmp_path: Path) -> None:
    src = _source(tmp_path)
    baseline = tmp_path / "radon.json"
    baseline.write_text(
        json.dumps({"version": 1, "source_root": str(src), "threshold": 10, "functions": {"stale": 12}})
    )
    assert main(["--src", str(src), "--baseline", str(baseline), "--tighten-baseline"]) == 0
    assert json.loads(baseline.read_text())["functions"] == {}
