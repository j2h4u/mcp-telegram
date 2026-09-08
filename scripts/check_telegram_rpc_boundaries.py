"""Enforce the application-owned Telegram transport boundary.

Application code receives an injected Telegram facade and must use its public
methods. Only the transport owner may reach Telethon's private sender seam or
an unbound superclass implementation. The two explicit exceptions are the
maintenance client factory (used by ``logout``) and the daemon's connection
lifecycle helpers.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src" / "mcp_telegram"
GATE_PATH = SOURCE_ROOT / "telegram_rpc.py"
FACTORY_PATH = SOURCE_ROOT / "telegram.py"
DAEMON_PATH = SOURCE_ROOT / "daemon.py"

# These paths own the concrete Telethon type. The deploy QR helper lives in a
# separate maintenance surface and is intentionally outside SOURCE_ROOT.
_CONCRETE_CLIENT_OWNER_PATHS = frozenset({GATE_PATH, FACTORY_PATH})
_TRANSPORT_OWNER_PATHS = frozenset({GATE_PATH})
_MAINTENANCE_CONSTRUCTOR_FUNCTIONS = frozenset({"create_maintenance_client"})
_LIFECYCLE_ALLOWLIST: dict[Path, dict[str, frozenset[str]]] = {
    FACTORY_PATH: {"logout_from_telegram": frozenset({"connect", "log_out"})},
    DAEMON_PATH: {
        "_connect_telegram": frozenset({"connect"}),
        "_monitor_flood_wait_kill_switch": frozenset({"disconnect"}),
        "_shutdown_sync_main_context": frozenset({"disconnect"}),
    },
}

_LIFECYCLE_METHODS = frozenset(
    {
        "connect",
        "disconnect",
        "start",
        "stop",
        "log_out",
        "sign_in",
        "send_code_request",
        "qr_login",
        "check_password",
        "is_user_authorized",
    }
)
_MIN_CLIENT_CHAIN_PARTS = 2
_VENDOR_WAIT_NAMES = frozenset({"FloodWaitError", "FloodPremiumWaitError", "FloodTestPhoneWaitError"})
_REMOVED_NAMES = frozenset({"TelegramRpcCircuitOpenError", "FloodWaitErrors"})
_GATE_NAME = "TelegramRpcGate"
_LIMITER_NAME = "AsyncLimiter"
_SCHEDULER_NAME = "TelegramRpcAdmissionScheduler"


def _same_path(path: Path, expected: Path) -> bool:
    """Compare paths without requiring either path to exist."""
    return path.resolve() == expected.resolve()


def _owner_path(path: Path, owners: Iterable[Path]) -> bool:
    return any(_same_path(path, owner) for owner in owners)


def _qualified_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _attribute_chain(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        return (*_attribute_chain(node.value), node.attr)
    return ()


def _is_client_receiver(node: ast.AST) -> bool:
    """Recognize conventional injected-client receiver forms."""
    chain = _attribute_chain(node)
    if not chain:
        return False
    if chain[-1] in {"client", "_client", "telegram", "_telegram"}:
        return True
    return (
        chain[-1] in {"deps", "_deps"} and len(chain) >= _MIN_CLIENT_CHAIN_PARTS and chain[-2] in {"client", "_client"}
    )


class _BoundaryVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.violations: list[str] = []
        self._function_stack: list[str] = []
        self._class_stack: list[str] = []
        self._telethon_client_subclasses: list[bool] = []
        self._telegram_client_aliases: set[str] = set()
        self._telethon_module_aliases: set[str] = set()
        self._aiolimiter_module_aliases: set[str] = set()
        self._async_limiter_aliases: set[str] = set()
        self._scheduler_module_aliases: set[str] = set()
        self._scheduler_aliases: set[str] = set()
        self._telegram_rpc_gate_aliases: set[str] = set()
        self._telegram_rpc_module_aliases: set[str] = set()
        self._telegram_rpc_gate_imports: list[tuple[ast.AST, bool, str]] = []
        self._gate_runtime_uses: set[str] = set()
        self._gate_annotation_uses: set[str] = set()
        self._annotation_depth = 0
        self._type_checking_depth = 0

    @property
    def in_transport_owner(self) -> bool:
        return _owner_path(self.path, _TRANSPORT_OWNER_PATHS) and self._class_stack[-1:] == ["TelegramRpcGate"]

    @property
    def in_telethon_client_subclass(self) -> bool:
        return bool(self._telethon_client_subclasses and self._telethon_client_subclasses[-1])

    @property
    def function_name(self) -> str | None:
        return self._function_stack[-1] if self._function_stack else None

    def _add(self, node: ast.AST, message: str) -> None:
        line = getattr(node, "lineno", 1)
        self.violations.append(f"{self.path}:{line}: {message}")

    def _gate_import_owner(self) -> bool:
        return _same_path(self.path, FACTORY_PATH) or _same_path(self.path, GATE_PATH)

    def _transport_module_owner(self) -> bool:
        return _same_path(self.path, GATE_PATH)

    def _is_telegram_rpc_import(self, node: ast.ImportFrom, imported: ast.alias) -> bool:
        return (
            (node.module == "telegram_rpc" and node.level > 0)
            or (node.module == "mcp_telegram.telegram_rpc")
            or (node.module == "mcp_telegram" and imported.name == "telegram_rpc")
            or (node.module is None and node.level > 0 and imported.name == "telegram_rpc")
        )

    def _mark_gate_use(self, node: ast.AST, key: str) -> None:
        del node
        if self._annotation_depth or self._type_checking_depth:
            self._gate_annotation_uses.add(key)
        else:
            self._gate_runtime_uses.add(key)

    def _gate_qualified_target(self, node: ast.AST) -> str | None:
        qualified = _qualified_name(node)
        if qualified in self._telegram_rpc_gate_aliases:
            return qualified
        if not qualified or not qualified.endswith(f".{_GATE_NAME}"):
            return None
        prefix = qualified[: -(len(_GATE_NAME) + 1)]
        if prefix in self._telegram_rpc_module_aliases:
            return prefix
        return None

    def _transport_dependency_target(self, node: ast.AST, name: str, aliases: set[str], modules: set[str]) -> bool:
        qualified = _qualified_name(node)
        if qualified in aliases:
            return True
        if not qualified or not qualified.endswith(f".{name}"):
            return False
        return qualified[: -(len(name) + 1)] in modules

    def _finalize_gate_imports(self) -> None:
        if self._gate_import_owner():
            return
        for node, type_only_import, alias in self._telegram_rpc_gate_imports:
            if type_only_import:
                continue
            if alias in self._gate_runtime_uses:
                self._add(node, "TelegramRpcGate import/use outside create_client")
                continue
            if alias in self._gate_annotation_uses:
                continue
            self._add(node, "unused TelegramRpcGate import outside create_client")

    def _visit_telethon_module_import(self, node: ast.Import, imported: ast.alias) -> None:
        if imported.name == "telethon":
            self._telethon_module_aliases.add(imported.asname or "telethon")
        if imported.name in {"telethon", "telethon.errors", "telethon.errors.rpcerrorlist"} and not _same_path(
            self.path, GATE_PATH
        ):
            self._add(node, f"vendor module import {imported.name}")

    def _visit_aiolimiter_module_import(self, node: ast.Import, imported: ast.alias) -> None:
        if imported.name != "aiolimiter":
            return
        alias = imported.asname or imported.name
        self._aiolimiter_module_aliases.add(alias)
        if not self._transport_module_owner():
            self._add(node, "aiolimiter import outside TelegramRpcGate transport owner")

    def _visit_scheduler_module_import(self, node: ast.Import, imported: ast.alias) -> None:
        if imported.name != "mcp_telegram.telegram_rpc_scheduler":
            return
        alias = imported.asname or imported.name
        self._scheduler_module_aliases.add(alias)
        if not self._transport_module_owner():
            self._add(node, "telegram_rpc_scheduler import outside TelegramRpcGate transport owner")

    def _visit_telegram_rpc_module_import(self, node: ast.Import, imported: ast.alias) -> None:
        if imported.name != "mcp_telegram.telegram_rpc":
            return
        alias = imported.asname or imported.name
        self._telegram_rpc_module_aliases.add(alias)
        self._telegram_rpc_gate_imports.append((node, self._type_checking_depth > 0, alias))

    def visit_Import(self, node: ast.Import) -> None:
        for imported in node.names:
            self._visit_telethon_module_import(node, imported)
            self._visit_aiolimiter_module_import(node, imported)
            self._visit_scheduler_module_import(node, imported)
            self._visit_telegram_rpc_module_import(node, imported)
        self.generic_visit(node)

    def _visit_telethon_symbol_import(self, node: ast.ImportFrom, imported: ast.alias) -> None:
        if imported.name == "TelegramClient":
            alias = imported.asname or imported.name
            self._telegram_client_aliases.add(alias)
            if not _owner_path(self.path, _CONCRETE_CLIENT_OWNER_PATHS):
                self._add(node, "concrete TelegramClient import outside transport/factory owner")
        if (
            node.module in {"telethon.errors", "telethon.errors.rpcerrorlist"}
            and imported.name in _VENDOR_WAIT_NAMES
            and not _same_path(self.path, GATE_PATH)
        ):
            self._add(node, f"vendor wait import {imported.name}")

    def _visit_telethon_import(self, node: ast.ImportFrom) -> None:
        if not node.module or not node.module.startswith("telethon"):
            return
        for imported in node.names:
            self._visit_telethon_symbol_import(node, imported)
        if (
            node.module == "telethon"
            and any(alias.name == "errors" for alias in node.names)
            and not _same_path(self.path, GATE_PATH)
        ):
            self._add(node, "vendor errors module import")

    def _visit_telegram_rpc_import(self, node: ast.ImportFrom) -> None:
        for imported in node.names:
            if not self._is_telegram_rpc_import(node, imported):
                continue
            if imported.name == _GATE_NAME:
                alias = imported.asname or imported.name
                self._telegram_rpc_gate_aliases.add(alias)
                self._telegram_rpc_gate_imports.append((node, self._type_checking_depth > 0, alias))

    def _visit_limiter_import(self, node: ast.ImportFrom, imported: ast.alias) -> None:
        if imported.name != _LIMITER_NAME or node.module != "aiolimiter":
            return
        alias = imported.asname or imported.name
        self._async_limiter_aliases.add(alias)
        if not self._transport_module_owner():
            self._add(node, "AsyncLimiter import outside TelegramRpcGate transport owner")

    def _visit_scheduler_import(self, node: ast.ImportFrom, imported: ast.alias) -> None:
        if imported.name != _SCHEDULER_NAME or node.module == "aiolimiter":
            return
        alias = imported.asname or imported.name
        self._scheduler_aliases.add(alias)
        if not self._transport_module_owner():
            self._add(node, "TelegramRpcAdmissionScheduler import outside TelegramRpcGate transport owner")

    def _visit_transport_dependency_import(self, node: ast.ImportFrom) -> None:
        if node.module not in {"aiolimiter", "telegram_rpc_scheduler", "mcp_telegram.telegram_rpc_scheduler"}:
            return
        for imported in node.names:
            self._visit_limiter_import(node, imported)
            self._visit_scheduler_import(node, imported)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for imported in node.names:
            if imported.name in _REMOVED_NAMES:
                self._add(node, f"removed throttling import {imported.name}")
        self._visit_telethon_import(node)
        self._visit_telegram_rpc_import(node)
        self._visit_transport_dependency_import(node)
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        is_type_checking = isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"
        self.visit(node.test)
        if is_type_checking:
            self._type_checking_depth += 1
        for statement in node.body:
            self.visit(statement)
        if is_type_checking:
            self._type_checking_depth -= 1
        for statement in node.orelse:
            self.visit(statement)

    def _visit_annotation(self, node: ast.AST) -> None:
        self._annotation_depth += 1
        self.visit(node)
        self._annotation_depth -= 1

    def visit_arg(self, node: ast.arg) -> None:
        if node.annotation is not None:
            self._visit_annotation(node.annotation)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._visit_annotation(node.annotation)
        self.visit(node.target)
        if node.value is not None:
            self.visit(node.value)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for base in node.bases:
            gate_target = self._gate_qualified_target(base)
            if gate_target is not None:
                self._mark_gate_use(base, gate_target)
                if not self._gate_import_owner():
                    self._add(base, "TelegramRpcGate subclass outside transport/factory owner")
            if (
                self._transport_dependency_target(
                    base, _SCHEDULER_NAME, self._scheduler_aliases, self._scheduler_module_aliases
                )
                and not self._transport_module_owner()
            ):
                self._add(base, "TelegramRpcAdmissionScheduler subclass outside TelegramRpcGate transport owner")
        is_telethon_client_subclass = any(
            _qualified_name(base) in self._telegram_client_aliases
            or (
                _qualified_name(base) is not None
                and any(_qualified_name(base) == f"{alias}.TelegramClient" for alias in self._telethon_module_aliases)
            )
            for base in node.bases
        )
        self._class_stack.append(node.name)
        self._telethon_client_subclasses.append(is_telethon_client_subclass)
        self.generic_visit(node)
        self._telethon_client_subclasses.pop()
        self._class_stack.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._function_stack.append(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            self.visit(argument)
        if node.args.vararg is not None:
            self.visit(node.args.vararg)
        if node.args.kwarg is not None:
            self.visit(node.args.kwarg)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        if node.returns is not None:
            self._visit_annotation(node.returns)
        for statement in node.body:
            self.visit(statement)
        self._function_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _check_removed_attribute(self, node: ast.Attribute) -> None:
        if node.attr in _REMOVED_NAMES:
            self._add(node, f"removed throttling symbol {node.attr}")

    def _check_vendor_wait_attribute(self, node: ast.Attribute) -> None:
        if node.attr in _VENDOR_WAIT_NAMES and not _same_path(self.path, GATE_PATH):
            self._add(node, f"vendor wait reference {node.attr}")

    def _check_private_telegram_attribute(self, node: ast.Attribute) -> None:
        if node.attr == "_call" and not self.in_transport_owner:
            self._add(node, "private Telegram _call bypasses admission")
        if node.attr == "_sender" and not self.in_transport_owner:
            self._add(node, "private Telegram _sender bypasses admission")

    def _check_private_sender_send(self, node: ast.Attribute) -> None:
        if (
            node.attr == "send"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "_sender"
            and not self.in_transport_owner
        ):
            self._add(node, "private Telegram _sender.send bypasses admission")

    def _mark_gate_attribute(self, node: ast.Attribute) -> None:
        gate_target = self._gate_qualified_target(node)
        if gate_target is not None:
            self._mark_gate_use(node, gate_target)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._check_removed_attribute(node)
        self._check_vendor_wait_attribute(node)
        self._check_private_telegram_attribute(node)
        self._check_private_sender_send(node)
        self._mark_gate_attribute(node)
        self.generic_visit(node)

    def _client_constructor(self, node: ast.Call) -> bool:
        function = node.func
        if isinstance(function, ast.Name):
            return function.id in self._telegram_client_aliases
        if isinstance(function, ast.Attribute) and function.attr == "TelegramClient":
            return (
                bool(self._telethon_module_aliases) and _qualified_name(function.value) in self._telethon_module_aliases
            )
        return False

    def _constructor_allowed(self) -> bool:
        return _same_path(self.path, FACTORY_PATH) and self.function_name in _MAINTENANCE_CONSTRUCTOR_FUNCTIONS

    def _gate_constructor(self, node: ast.Call) -> bool:
        return self._gate_qualified_target(node.func) is not None

    def _transport_dependency_constructor(
        self, node: ast.Call, name: str, aliases: set[str], modules: set[str]
    ) -> bool:
        return self._transport_dependency_target(node.func, name, aliases, modules)

    def _unbound_telegram_call(self, node: ast.Call) -> bool:
        function = node.func
        if isinstance(function, ast.Attribute):
            target = _qualified_name(function.value)
            if target in self._telegram_client_aliases:
                return True
            if target and any(target == f"{alias}.TelegramClient" for alias in self._telethon_module_aliases):
                return True
            if (
                self.in_telethon_client_subclass
                and isinstance(function.value, ast.Call)
                and _qualified_name(function.value.func) == "super"
            ):
                return True
        return False

    def _lifecycle_allowed(self, method: str) -> bool:
        allowed_functions = _LIFECYCLE_ALLOWLIST.get(self.path, {})
        return method in allowed_functions.get(self.function_name or "", frozenset())

    def _check_gate_construction(self, node: ast.Call) -> None:
        if self._gate_constructor(node) and not (
            _same_path(self.path, FACTORY_PATH) and self.function_name == "create_client"
        ):
            self._add(node, "direct TelegramRpcGate construction outside telegram.py:create_client")

    def _check_client_construction(self, node: ast.Call) -> None:
        if self._client_constructor(node) and not self._constructor_allowed():
            self._add(node, "direct TelegramClient construction outside create_maintenance_client")

    def _check_transport_dependency_construction(self, node: ast.Call) -> None:
        if (
            self._transport_dependency_constructor(
                node, _LIMITER_NAME, self._async_limiter_aliases, self._aiolimiter_module_aliases
            )
            and not self._transport_module_owner()
        ):
            self._add(node, "AsyncLimiter construction outside TelegramRpcGate transport owner")
        if (
            self._transport_dependency_constructor(
                node, _SCHEDULER_NAME, self._scheduler_aliases, self._scheduler_module_aliases
            )
            and not self._transport_module_owner()
        ):
            self._add(node, "TelegramRpcAdmissionScheduler construction outside TelegramRpcGate transport owner")

    def _check_unbound_telegram_call(self, node: ast.Call) -> None:
        if self._unbound_telegram_call(node) and not self.in_transport_owner:
            self._add(node, "unbound TelegramClient call bypasses admission")

    def _check_lifecycle_call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in _LIFECYCLE_METHODS
            and _is_client_receiver(node.func.value)
            and not self._lifecycle_allowed(node.func.attr)
        ):
            self._add(node, f"Telegram lifecycle call {node.func.attr} outside approved owner")

    def visit_Call(self, node: ast.Call) -> None:
        self._check_gate_construction(node)
        self._check_client_construction(node)
        self._check_transport_dependency_construction(node)
        self._check_unbound_telegram_call(node)
        self._check_lifecycle_call(node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in _REMOVED_NAMES:
            self._add(node, f"removed throttling symbol {node.id}")
        if node.id in _VENDOR_WAIT_NAMES and not _same_path(self.path, GATE_PATH):
            self._add(node, f"vendor wait reference {node.id}")
        if node.id in self._telegram_rpc_gate_aliases:
            self._mark_gate_use(node, node.id)


def _violations(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    visitor = _BoundaryVisitor(path)
    visitor.visit(tree)
    visitor._finalize_gate_imports()
    return sorted(set(visitor.violations))


def check(root: Path = SOURCE_ROOT) -> list[str]:
    """Return all Telegram RPC boundary violations below *root*."""
    return [violation for path in sorted(root.rglob("*.py")) for violation in _violations(path)]


def main() -> int:
    violations = check()
    if violations:
        print("\n".join(violations), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
