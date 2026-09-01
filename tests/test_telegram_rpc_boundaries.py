from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Protocol, cast

import pytest


class _BoundaryChecker(Protocol):
    GATE_PATH: Path

    def _violations(self, path: Path) -> list[str]: ...
    def check(self) -> list[str]: ...


def _load_gate() -> _BoundaryChecker:
    path = Path(__file__).parents[1] / "scripts" / "check_telegram_rpc_boundaries.py"
    spec = importlib.util.spec_from_file_location("check_telegram_rpc_boundaries", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return cast(_BoundaryChecker, module)


@pytest.mark.parametrize(
    "source",
    [
        "from telethon.errors import FloodWaitError\n",
        "from telethon.errors.rpcerrorlist import FloodPremiumWaitError as Wait\n",
        "from telethon.errors import FloodTestPhoneWaitError\ndef f():\n    raise FloodTestPhoneWaitError\n",
        "from telethon import errors as e\ne.FloodWaitError\n",
        "import telethon as t\nt.errors.FloodPremiumWaitError\n",
        "import telethon.errors as e\ne.FloodTestPhoneWaitError\n",
        "def f():\n    from telethon.errors import FloodTestPhoneWaitError as Wait\n",
        "from mcp_telegram.telegram_rpc import TelegramRpcCircuitOpenError\n",
        "def f(exc):\n    return isinstance(exc, FloodWaitErrors)\n",
    ],
)
def test_boundary_rejects_vendor_waits_and_removed_consumer_symbols(tmp_path: Path, source: str) -> None:
    path = tmp_path / "consumer.py"
    path.write_text(source, encoding="utf-8")
    gate = _load_gate()

    violations = gate._violations(path)

    assert violations


def test_boundary_allows_vendor_wait_imports_inside_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "telegram_rpc.py"
    path.write_text("from telethon.errors import FloodWaitError\n", encoding="utf-8")
    gate = _load_gate()
    monkeypatch.setattr(gate, "GATE_PATH", path)

    assert gate._violations(path) == []


def test_repository_has_no_boundary_violations() -> None:
    gate = _load_gate()

    assert gate.check() == []
