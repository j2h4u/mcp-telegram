from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Protocol, cast

import pytest


class _BoundaryChecker(Protocol):
    GATE_PATH: Path
    SOURCE_ROOT: Path
    _PRIVATE_SENDER_SEAMS: dict[Path, frozenset[tuple[str, str, str]]]
    _LIFECYCLE_ALLOWLIST: dict[Path, dict[str, frozenset[str]]]

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
        "from mcp_telegram.telegram_rpc import reset_account_cooldown as reset\nreset()\n",
        "import mcp_telegram.telegram_rpc as rpc\nrpc.reset_account_cooldown()\n",
        "reset_account_cooldown()\n",
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
        "client._main_sender.send(request)\n",
        "sender = client._main_sender\nsender.send(request)\n",
        "send = client._main_sender.send\nawait send(request)\n",
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


@pytest.mark.parametrize(
    "source",
    [
        "await client.download_media(message)\n",
        "await client.download_file(location, bytes)\n",
        "await client.get_stats(channel)\n",
        "await client.edit_message(inline_message_id, 'updated')\n",
        "borrow = client._borrow_exported_sender\n",
        "create = self._client._create_exported_sender\n",
    ],
)
def test_boundary_rejects_inherited_borrowed_sender_methods(tmp_path: Path, source: str) -> None:
    path = tmp_path / "consumer.py"
    path.write_text(source, encoding="utf-8")
    gate = _load_gate()

    violations = gate._violations(path)

    assert any("bypasses admission through inherited transport" in violation for violation in violations)


def test_boundary_allows_admitted_public_client_methods(tmp_path: Path) -> None:
    path = tmp_path / "adapter.py"
    path.write_text(
        "await client.get_messages(peer, ids=[1])\nawait client.get_entity(peer)\nawait client(request)\n",
        encoding="utf-8",
    )
    gate = _load_gate()

    assert gate._violations(path) == []


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
        "        return await self._sender.send(request)\n"
        "    async def send_main(self, request):\n"
        "        return await self._main_sender.send(request)\n"
        "class _MainSenderAdapter:\n"
        "    def send(self, request):\n"
        "        return self._gate._main_sender.send(request)\n",
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


def test_photo_iterator_allows_only_type_checking_client_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = _load_gate()
    path = tmp_path / "telegram_image_download.py"
    monkeypatch.setattr(gate, "PHOTO_ITERATOR_PATH", path)
    import_text = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from telethon.client.telegramclient import TelegramClient\n"
    path.write_text(
        import_text + "def identity(client: TelegramClient) -> TelegramClient:\n    return client\n", encoding="utf-8"
    )
    assert gate._violations(path) == []
    for forbidden in (
        "from telethon.client.telegramclient import TelegramClient\n",
        import_text + "TelegramClient()\n",
        import_text + "TelegramClient.connect(client)\n",
    ):
        path.write_text(forbidden, encoding="utf-8")
        assert gate._violations(path)
    neighbor = tmp_path / "neighbor.py"
    neighbor.write_text(import_text, encoding="utf-8")
    assert gate._violations(neighbor)


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


@pytest.mark.parametrize(
    "case",
    [
        ("telegram_rpc.py", "_AdmissionAwareSender", "_selected_sender", "self._gate._main_sender.send_owned(request)"),
        ("telegram_image_download.py", "_PhotoIteratorFacade", "__init__", "self._sender = object()"),
        ("telegram_image_download.py", "_PhotoIteratorFacade", "_call", "sender = self._sender"),
        ("telegram_image_download.py", "_PhotoIteratorFacade", "_borrow_exported_sender", "sender = self._sender"),
        ("telegram_image_download.py", "_PhotoIteratorFacade", "_return_exported_sender", "sender = self._sender"),
        ("telegram_transport.py", "_OwnedPacker", "__init__", "self._sender = sender"),
        ("telegram_transport.py", "_OwnedPacker", "append", "self._sender.require_owned(state)"),
        ("telegram_transport.py", "_AuthConnectionProxy", "__init__", "self._sender = sender"),
        ("telegram_transport.py", "_AuthConnectionProxy", "send", "self._sender._prepare_state(state)"),
        ("telegram_transport.py", "_AuthConnectionProxy", "recv", "connection = self._sender._connection"),
        ("telegram_transport.py", "GuardedMTProtoPlainSender", "__init__", "self._sender = sender"),
        ("telegram_transport.py", "GuardedMTProtoPlainSender", "send", "self._sender._remember(state, owner)"),
    ],
)
def test_owned_sender_registration_is_method_specific(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, str, str, str],
) -> None:
    filename, owner, method, expression = case
    gate = _load_gate()
    path = tmp_path / filename
    seams = gate._PRIVATE_SENDER_SEAMS[gate.SOURCE_ROOT / filename]
    monkeypatch.setattr(gate, "_PRIVATE_SENDER_SEAMS", {path: seams})
    source = f"class {owner}:\n    def {method}(self):\n        {expression}\n"
    path.write_text(source, encoding="utf-8")
    assert gate._violations(path) == []

    for forbidden in (
        source.replace(f"def {method}(", "def adjacent("),
        source.replace(f"class {owner}:", "class OtherOwner:"),
        source.replace("self._", "other._"),
        source + "    def adjacent(self):\n        self._sender.send(request)\n",
    ):
        path.write_text(forbidden, encoding="utf-8")
        assert gate._violations(path)

    neighbor = tmp_path / "neighbor.py"
    neighbor.write_text(source, encoding="utf-8")
    assert gate._violations(neighbor)


@pytest.mark.parametrize(
    "expression",
    ["self._sender.send(request)", "self._call(request)", "self._main_sender.send(request)"],
)
def test_owned_sender_method_does_not_permit_raw_send_or_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expression: str
) -> None:
    gate = _load_gate()
    path = tmp_path / "telegram_transport.py"
    seams = gate._PRIVATE_SENDER_SEAMS[gate.SOURCE_ROOT / path.name]
    monkeypatch.setattr(gate, "_PRIVATE_SENDER_SEAMS", {path: seams})
    path.write_text(f"class _AuthConnectionProxy:\n    def send(self):\n        {expression}\n", encoding="utf-8")

    assert gate._violations(path)


@pytest.mark.parametrize(
    ("filename", "function", "method"),
    [("telegram.py", "maintenance_client", "connect"), ("telegram_auth.py", "authenticate", "is_user_authorized")],
)
def test_auth_lifecycle_registration_is_function_specific(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str, function: str, method: str
) -> None:
    gate = _load_gate()
    path = tmp_path / filename
    allowed = gate._LIFECYCLE_ALLOWLIST[gate.SOURCE_ROOT / filename]
    monkeypatch.setattr(gate, "_LIFECYCLE_ALLOWLIST", {path: allowed})
    source = f"async def {function}(client):\n    await client.{method}()\n"
    path.write_text(source, encoding="utf-8")
    assert gate._violations(path) == []

    for forbidden in (
        source.replace(function, "adjacent"),
        source.replace(f"client.{method}()", "client.start()"),
        source.replace(f"client.{method}()", "client._sender.send(request)"),
        "class OtherOwner:\n" + "\n".join(f"    {line}" for line in source.splitlines()),
    ):
        path.write_text(forbidden, encoding="utf-8")
        assert gate._violations(path)

    neighbor = tmp_path / "neighbor.py"
    neighbor.write_text(source, encoding="utf-8")
    assert gate._violations(neighbor)
