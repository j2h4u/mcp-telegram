from __future__ import annotations

import tomllib
from pathlib import Path
from typing import cast

from scripts import check_packaging_smoke


def test_packaging_smoke_build_uses_locked_backend_environment() -> None:
    command = check_packaging_smoke._build_wheel_command(Path("/repo"), Path("/repo/dist"))

    assert "--no-build-isolation" in command


def test_packaging_smoke_backend_is_in_locked_dev_group() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    dependency_groups = cast(dict[str, object], pyproject["dependency-groups"])
    dev_dependencies = cast(list[str], dependency_groups["dev"])

    assert any(dependency.startswith("setuptools>=") for dependency in dev_dependencies)
