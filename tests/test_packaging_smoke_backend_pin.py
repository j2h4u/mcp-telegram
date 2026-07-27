import tomllib
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]


def test_packaging_smoke_runs_from_frozen_project_environment() -> None:
    justfile = (ROOT / "Justfile").read_text(encoding="utf-8")

    assert "uv run --frozen python scripts/check_packaging_smoke.py" in justfile


def test_build_backend_is_locked_for_packaging_smoke() -> None:
    pyproject = cast(dict[str, object], tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8")))
    lockfile = cast(dict[str, object], tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8")))
    script = (ROOT / "scripts" / "check_packaging_smoke.py").read_text(encoding="utf-8")
    dependency_groups = cast(dict[str, list[str]], pyproject["dependency-groups"])
    locked_packages = cast(list[dict[str, object]], lockfile["package"])

    assert "setuptools==83.0.0" in dependency_groups["dev"]

    locked_setuptools = [package for package in locked_packages if package["name"] == "setuptools"]
    assert locked_setuptools == [
        {
            "name": "setuptools",
            "version": "83.0.0",
            "source": {"registry": "https://pypi.org/simple"},
            "sdist": {
                "url": "https://files.pythonhosted.org/packages/34/26/f5d29e25ffdb535afef2d35cdb55b325298f96debd670da4c325e08d70f4/setuptools-83.0.0.tar.gz",
                "hash": "sha256:025bccbbf0fa05b6192bc64ae1e7b16e001fd6d6d4d5de03c97b1c1ade523bef",
                "size": 1154254,
                "upload-time": "2026-07-04T15:31:22.699Z",
            },
            "wheels": [
                {
                    "url": "https://files.pythonhosted.org/packages/5d/40/e1e72872c6354b306daef1703549e8e83b4d43cfea356311bf722a043752/setuptools-83.0.0-py3-none-any.whl",
                    "hash": "sha256:29b23c360f22f414dc7336bb39178cc7bcbf6021ed2733cde173f09dba19abb3",
                    "size": 1008090,
                    "upload-time": "2026-07-04T15:31:20.885Z",
                }
            ],
        }
    ]
    assert '"--no-build-isolation"' in script
    assert '"--python"' in script
    assert "sys.executable" in script
