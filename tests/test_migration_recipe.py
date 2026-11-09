import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("failure", ["none", "build", "backup", "promotion", "up", "identity"])
def test_migration_recipe_order(tmp_path: Path, failure: str) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    trace = tmp_path / "trace"
    docker = bindir / "docker"
    docker.write_text("""#!/usr/bin/env bash
echo "docker $*" >> "$TRACE"
case "$*" in
  "inspect --format {{.Id}} mcp-telegram") echo old-container ;;
  "inspect --format {{.Image}} old-container") echo old-image ;;
  "inspect --format {{.Image}} mcp-telegram") if [[ "$FAILURE" == identity ]]; then echo wrong; else echo candidate; fi ;;
  "image inspect "*) echo candidate ;;
  *" config --images "*) echo base-image ;;
  "tag candidate base-image")
    [[ "$FAILURE" != promotion ]] || exit 1
    echo candidate > "$BASE_IMAGE"
    ;;
  *" build "*) [[ "$FAILURE" != build ]] ;;
  *" up "*)
    [[ "$FAILURE" != up ]] || exit 1
    if [[ "$*" != *" -f "*" -f "* ]]; then cat "$BASE_IMAGE" >> "$TRACE"; fi
    ;;
esac
""")
    uv = bindir / "uv"
    uv.write_text("""#!/usr/bin/env bash
echo "backup $*" >> "$TRACE"
[[ "$FAILURE" != backup ]]
""")
    nested_just = bindir / "just"
    nested_just.write_text('#!/usr/bin/env bash\necho "just $*" >> "$TRACE"\n')
    for executable in (docker, uv, nested_just):
        executable.chmod(0o755)
    env = os.environ | {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "TRACE": str(trace),
        "FAILURE": failure,
        "TMPDIR": str(tmp_path),
        "BASE_IMAGE": str(tmp_path / "base-image"),
    }
    (tmp_path / "base-image").write_text("old-image\n")
    result = subprocess.run(
        ["/usr/bin/just", "runtime-migrate", "/state", str(tmp_path / "pair"), "102", "release", "config"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    lines = trace.read_text().splitlines()
    build = next(i for i, line in enumerate(lines) if " build " in line)
    assert any("docker tag old-image " in line for line in lines[:build])
    assert (result.returncode == 0) == (failure == "none")
    if failure == "build":
        assert not any("docker stop " in line for line in lines)
        return
    stop = next(i for i, line in enumerate(lines) if line == "docker stop --time -1 old-container")
    backup = next(i for i, line in enumerate(lines) if line.startswith("backup "))
    assert build < stop < backup
    if failure in {"backup", "promotion"}:
        assert "docker start old-container" in lines
        assert not any(" up " in line for line in lines)
        assert (tmp_path / "base-image").read_text() == "old-image\n"
        assert ("docker tag candidate base-image" in lines) == (failure == "promotion")
    else:
        up = next(i for i, line in enumerate(lines) if " up " in line)
        promotion = lines.index("docker tag candidate base-image")
        assert backup < promotion < up
        assert "--no-build --pull never" in lines[up]
        assert not any(line.startswith("docker start ") for line in lines)
    if failure == "none":
        assert lines[-2:] == ["just runtime-wait", "just runtime-smoke"]
        subprocess.run(["/usr/bin/just", "runtime-start"], env=env, capture_output=True, text=True, check=True)
        assert trace.read_text().splitlines()[-1] == "candidate"
