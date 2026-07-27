import os
import subprocess
import sys
import tempfile
from importlib import metadata
from pathlib import Path
from venv import EnvBuilder

BUILD_BACKEND_DISTRIBUTION = "setuptools"


def _run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _require_locked_build_backend() -> None:
    try:
        metadata.version(BUILD_BACKEND_DISTRIBUTION)
    except metadata.PackageNotFoundError as exc:
        raise RuntimeError(
            f"{BUILD_BACKEND_DISTRIBUTION} must be installed by the locked project environment "
            "before running packaging smoke"
        ) from exc


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    _require_locked_build_backend()

    with tempfile.TemporaryDirectory(prefix="mcp-telegram-packaging-") as tmp:
        workdir = Path(tmp)
        dist_dir = workdir / "dist"
        venv_dir = workdir / "venv"

        _run(
            [
                "uv",
                "build",
                "--wheel",
                "--out-dir",
                str(dist_dir),
                "--no-build-logs",
                "--no-build-isolation",
                "--python",
                sys.executable,
                str(repo_root),
            ]
        )

        wheel_files = sorted(dist_dir.glob("*.whl"))
        if len(wheel_files) != 1:
            raise RuntimeError(f"expected exactly one wheel, found {len(wheel_files)} in {dist_dir}")

        EnvBuilder(with_pip=False, clear=True).create(venv_dir)
        venv_python = venv_dir / "bin" / "python"
        executable = venv_dir / "bin" / "mcp-telegram"
        if not venv_python.is_file():
            raise RuntimeError(f"venv python not found: {venv_python}")

        install_env = {**os.environ, "UV_LINK_MODE": "copy"}
        _run(["uv", "pip", "install", "--python", str(venv_python), str(wheel_files[0])], env=install_env)
        _run([str(executable), "--help"])
        _run([str(executable), "feedback", "--help"])

    print("packaging smoke passed: wheel built, installed, CLI help paths ran")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
