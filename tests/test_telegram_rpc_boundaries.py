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


@pytest.mark.parametrize(
    "source",
    [
        "from telethon import TelegramClient\nclient = TelegramClient('session', 1, 'hash')\n",
        "import telethon as tl\nclient = tl.TelegramClient('session', 1, 'hash')\n",
        "client._call(request)\n",
        "client._sender.send(request)\n",
        "sender = client._sender\nsender.send(request)\n",
        "from telethon import TelegramClient\nTelegramClient.__call__(client, request)\n",
        "from telethon import TelegramClient\nTelegramClient.get_messages(client)\n",
        "from telethon import TelegramClient\nclass OtherGate(TelegramClient):\n    async def call(self, request):\n        return await super().__call__(request)\n",
        "from telethon import TelegramClient\nclass OtherGate(TelegramClient):\n    async def update(self):\n        return await super()._update_loop()\n",
        "client.connect()\n",
    ],
)
def test_boundary_rejects_transport_bypasses(tmp_path: Path, source: str) -> None:
    path = tmp_path / "consumer.py"
    path.write_text(source, encoding="utf-8")
    gate = _load_gate()

    violations = gate._violations(path)

    assert violations


def test_boundary_allows_only_named_factory_and_transport_seams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = tmp_path / "telegram.py"
    factory.write_text(
        "from telethon import TelegramClient\n"
        "def create_maintenance_client():\n"
        "    return TelegramClient('session', 1, 'hash')\n",
        encoding="utf-8",
    )
    transport = tmp_path / "telegram_rpc.py"
    transport.write_text(
        "from telethon import TelegramClient\n"
        "class TelegramRpcGate(TelegramClient):\n"
        "    async def call(self, request):\n"
        "        return await super().__call__(request)\n"
        "    async def update(self):\n"
        "        return await super()._update_loop()\n"
        "    async def send(self, request):\n"
        "        return await self._sender.send(request)\n",
        encoding="utf-8",
    )
    gate = _load_gate()
    monkeypatch.setattr(gate, "FACTORY_PATH", factory)
    monkeypatch.setattr(gate, "GATE_PATH", transport)
    monkeypatch.setattr(gate, "_CONCRETE_CLIENT_OWNER_PATHS", frozenset({factory, transport}))
    monkeypatch.setattr(gate, "_TRANSPORT_OWNER_PATHS", frozenset({transport}))

    assert gate._violations(factory) == []
    assert gate._violations(transport) == []


def test_boundary_allows_documented_daemon_lifecycle_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    daemon = tmp_path / "daemon.py"
    daemon.write_text(
        "async def _connect_telegram(ctx):\n"
        "    await ctx.client.connect()\n"
        "async def _shutdown_sync_main_context(ctx):\n"
        "    await ctx.client.disconnect()\n",
        encoding="utf-8",
    )
    gate = _load_gate()
    monkeypatch.setattr(gate, "DAEMON_PATH", daemon)
    monkeypatch.setattr(
        gate,
        "_LIFECYCLE_ALLOWLIST",
        {
            daemon: {
                "_connect_telegram": frozenset({"connect"}),
                "_shutdown_sync_main_context": frozenset({"disconnect"}),
            }
        },
    )

    assert gate._violations(daemon) == []


@pytest.mark.parametrize(
    "source",
    [
        "from mcp_telegram.telegram_rpc import TelegramRpcGate\nclient = TelegramRpcGate()\n",
        "from mcp_telegram.telegram_rpc import TelegramRpcGate as Gate\nclient = Gate()\n",
        "import mcp_telegram.telegram_rpc as rpc\nclient = rpc.TelegramRpcGate()\n",
        "from .telegram_rpc import TelegramRpcGate\nTelegramRpcGate()\n",
    ],
)
def test_boundary_rejects_gate_construction_outside_factory(tmp_path: Path, source: str) -> None:
    path = tmp_path / "consumer.py"
    path.write_text(source, encoding="utf-8")
    gate = _load_gate()

    violations = gate._violations(path)

    assert any("TelegramRpcGate" in violation for violation in violations)


def test_boundary_allows_gate_type_only_imports(tmp_path: Path) -> None:
    path = tmp_path / "consumer.py"
    path.write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from mcp_telegram.telegram_rpc import TelegramRpcGate as Gate\n"
        "def use(client: Gate) -> Gate:\n"
        "    return client\n",
        encoding="utf-8",
    )
    gate = _load_gate()

    assert gate._violations(path) == []


def test_boundary_allows_gate_construction_only_in_create_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = tmp_path / "telegram.py"
    factory.write_text(
        "from mcp_telegram.telegram_rpc import TelegramRpcGate as Gate\ndef create_client():\n    return Gate()\n",
        encoding="utf-8",
    )
    gate = _load_gate()
    monkeypatch.setattr(gate, "FACTORY_PATH", factory)
    monkeypatch.setattr(gate, "_CONCRETE_CLIENT_OWNER_PATHS", frozenset({factory, gate.GATE_PATH}))

    assert gate._violations(factory) == []


@pytest.mark.parametrize(
    "source",
    [
        "from aiolimiter import AsyncLimiter\nindependent_gate = AsyncLimiter(1, 1)\n",
        "from aiolimiter import AsyncLimiter as Limiter\nindependent_gate = Limiter(1, 1)\n",
        "import aiolimiter as limiter\nindependent_gate = limiter.AsyncLimiter(1, 1)\n",
        (
            "from .telegram_rpc_scheduler import TelegramRpcAdmissionScheduler\n"
            "independent_gate = TelegramRpcAdmissionScheduler(policy=policy, limiter=None)\n"
        ),
        (
            "from .telegram_rpc_scheduler import TelegramRpcAdmissionScheduler as Scheduler\n"
            "independent_gate = Scheduler(policy=policy, limiter=None)\n"
        ),
        (
            "import mcp_telegram.telegram_rpc_scheduler as scheduler\n"
            "independent_gate = scheduler.TelegramRpcAdmissionScheduler(policy=policy, limiter=None)\n"
        ),
        (
            "from .telegram_rpc_scheduler import TelegramRpcAdmissionScheduler\n"
            "class IndependentScheduler(TelegramRpcAdmissionScheduler):\n    pass\n"
        ),
    ],
)
def test_boundary_rejects_account_wide_transport_dependencies(tmp_path: Path, source: str) -> None:
    path = tmp_path / "consumer.py"
    path.write_text(source, encoding="utf-8")
    gate = _load_gate()

    violations = gate._violations(path)

    assert violations


def test_boundary_allows_account_wide_transport_dependencies_in_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "telegram_rpc.py"
    path.write_text(
        "from aiolimiter import AsyncLimiter\n"
        "from .telegram_rpc_scheduler import TelegramRpcAdmissionScheduler\n"
        "import mcp_telegram.telegram_rpc_scheduler as scheduler\n"
        "class TelegramRpcGate:\n"
        "    def __init__(self, policy):\n"
        "        self.limiter = AsyncLimiter(1, 1)\n"
        "        self.scheduler = TelegramRpcAdmissionScheduler(policy=policy, limiter=self.limiter)\n"
        "        self.other_scheduler = scheduler.TelegramRpcAdmissionScheduler(policy=policy, limiter=self.limiter)\n",
        encoding="utf-8",
    )
    gate = _load_gate()
    monkeypatch.setattr(gate, "GATE_PATH", path)

    assert gate._violations(path) == []
