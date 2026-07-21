from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Protocol, cast

import pytest


class _ConfigImportGate(Protocol):
    def violations_for(self, path: Path, source: str) -> list[str]: ...


def _load_gate() -> _ConfigImportGate:
    path = Path(__file__).parents[1] / "scripts" / "check_config_imports.py"
    spec = importlib.util.spec_from_file_location("check_config_imports", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return cast(_ConfigImportGate, module)


@pytest.mark.parametrize(
    "source",
    [
        "from .config import load_config\n",
        "from .config import load_config as configured\n",
        "from . import config\n",
        "from . import config as configured\n",
        "from .. import config\n",
        "from .. import config as configured\n",
        "from mcp_telegram.config import FreshnessConfig\n",
        "from mcp_telegram.config import FreshnessConfig as Policy\n",
        "import mcp_telegram.config\n",
        "import mcp_telegram.config as configured\n",
        "from mcp_telegram import config\n",
        "from mcp_telegram import config as configured\n",
    ],
)
def test_state_config_import_spellings_are_rejected(source: str) -> None:
    gate = _load_gate()
    path = Path(__file__).parents[1] / "src/mcp_telegram/state.py"

    violations = gate.violations_for(path, source)

    assert violations == ["src/mcp_telegram/state.py:1: direct config import is allowed only in composition roots"]


@pytest.mark.parametrize(
    "path_name, source",
    [
        ("daemon.py", "from .config import FreshnessConfig\n"),
        ("daemon.py", "from . import config as configured\n"),
        ("daemon_client.py", "from mcp_telegram.config import FreshnessConfig\n"),
        ("telegram.py", "import mcp_telegram.config as configured\n"),
        ("__init__.py", "from mcp_telegram import config as configured\n"),
        ("config.py", "from .config import FreshnessConfig\n"),
    ],
)
def test_composition_root_config_import_spellings_are_allowed(path_name: str, source: str) -> None:
    gate = _load_gate()
    path = Path(__file__).parents[1] / "src/mcp_telegram" / path_name

    assert gate.violations_for(path, source) == []


def test_capability_direct_config_import_is_rejected() -> None:
    gate = _load_gate()
    path = Path(__file__).parents[1] / "src/mcp_telegram/daemon_reading.py"

    assert gate.violations_for(path, "from mcp_telegram import config\n") == [
        "src/mcp_telegram/daemon_reading.py:1: direct config import is allowed only in composition roots"
    ]
