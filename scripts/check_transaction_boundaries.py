"""AST gate for sync.db transaction ownership and connection setup."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "mcp_telegram"

# Separate stores have their own transaction contracts and are outside the
# sync.db ownership gate.
EXCLUDED_MODULES = {Path("feedback_db.py"), Path("chat_export_checkpoint.py"), Path("chat_export_identity.py")}

# These functions are schema/bootstrap transactions, recovery/checkpoint
# boundaries, or a read-only snapshot transaction. Exceptions stay narrow and
# function-scoped so new runtime code cannot inherit them by module location.
TRANSACTION_EXCEPTIONS: dict[Path, frozenset[str]] = {
    Path("sync_db.py"): frozenset(
        {
            "_apply_migration",
            "_repair_v54_schema_ledger",
            "_ensure_scheduled_messages_fts",
            "_ensure_hydration_jobs",
            "ensure_own_only_schema",
            "repair_reserved_dialog_types",
            "_migrate_from_legacy_db",
        }
    ),
    Path("sqlite_checkpoint.py"): frozenset({"checkpoint_sqlite_connection"}),
    Path("reading/service.py"): frozenset({"_get_unread_summary_sync"}),
}

_SYNC_DB_BOOTSTRAP_TRANSACTIONS = frozenset(
    {
        "_apply_migration",
        "_apply_migration_51",
        "_apply_migration_52",
        "_apply_migration_53",
        "_apply_migration_54",
        "_apply_migration_55",
        "_apply_migration_56",
        "_apply_migration_59",
        "_apply_migration_60",
        "_apply_migration_61",
        "_apply_migration_62",
        "_apply_migration_63",
        "_apply_migration_64",
        "_apply_migration_65",
        "_apply_migration_66",
        "_apply_migration_67",
        "_apply_migration_68",
        "_apply_migration_69",
        "_apply_migration_70",
        "_apply_migration_71",
        "_apply_migration_74",
        "_apply_migration_78",
        "_apply_migration_79",
    }
    | TRANSACTION_EXCEPTIONS[Path("sync_db.py")]
)

CONNECT_EXCEPTIONS: dict[Path, frozenset[str]] = {
    Path("sync_db.py"): frozenset({"_open_sync_db"}),
    Path("runtime_observations.py"): frozenset({"_open_writer_connection"}),
    Path("event_recovery.py"): frozenset({"recover_events"}),
    Path("__init__.py"): frozenset({"feedback_list"}),
}

OPEN_SYNC_DB_CALLERS: dict[Path, frozenset[str]] = {
    Path("sync_db.py"): frozenset({"open_sync_db_reader", "open_runtime_sync_db", "ensure_sync_schema"}),
    Path("daemon.py"): frozenset({"_build_sync_main_context"}),
}

RUNTIME_FACTORY_FUNCTIONS: dict[Path, frozenset[str]] = {
    Path("sync_db.py"): frozenset({"open_runtime_sync_db"}),
    Path("daemon.py"): frozenset({"_build_sync_main_context"}),
    Path("runtime_observations.py"): frozenset({"_open_writer_connection"}),
}

_TRANSACTION_SQL = re.compile(r"^(?:BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b", re.IGNORECASE)
_PRAGMA_TOGGLE = re.compile(r"^PRAGMA\s+query_only\b", re.IGNORECASE)
_SQL_SPACE = re.compile(r"\s+")
_CONNECTION_NAMES = {"conn", "_conn", "connection", "_connection", "db", "_db"}
_HELPERS = {"write_transaction", "write_savepoint"}


def _qualified(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _qualified(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _string(node: ast.AST, values: dict[str, str], seen: frozenset[str] = frozenset()) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in seen:
            return None
        return values.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _string(node.left, values, seen)
        right = _string(node.right, values, seen)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        return "".join(
            item.value for item in node.values if isinstance(item, ast.Constant) and isinstance(item.value, str)
        )
    return None


def _function_nodes(body: list[ast.stmt], prefix: tuple[str, ...] = ()) -> Iterator[tuple[str, ast.AST]]:
    for statement in body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = ".".join((*prefix, statement.name))
            yield name, statement
            yield from _function_nodes(statement.body, (*prefix, statement.name))
        elif isinstance(statement, ast.ClassDef):
            yield from _function_nodes(statement.body, (*prefix, statement.name))


def _lexical_nodes(root: ast.AST) -> Iterator[ast.AST]:
    """Walk one lexical function, leaving nested function/class bodies deferred."""
    if isinstance(root, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
        stack: list[ast.AST] = list(reversed(root.body))
    else:
        return
    while stack:
        node = stack.pop()
        if node is not root and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield node
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _is_transaction_sql(text: str) -> bool:
    normalized = _SQL_SPACE.sub(" ", text).strip()
    normalized = re.sub(r"^(?:--[^\n]*(?:\n|$)|/\*.*?\*/\s*)+", "", normalized, flags=re.DOTALL)
    return bool(_TRANSACTION_SQL.match(normalized)) or bool(_PRAGMA_TOGGLE.match(normalized))


def _target_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return set().union(*(_target_names(item) for item in target.elts))
    return set()


def _helper_alias(value: ast.AST, aliases: dict[str, str]) -> str | None:
    qualified = _qualified(value)
    if qualified in _HELPERS or (qualified and qualified.rsplit(".", 1)[-1] in _HELPERS):
        return qualified.rsplit(".", 1)[-1]
    return aliases.get(qualified or "") if isinstance(value, ast.Name) else None


def _import_aliases(node: ast.Import | ast.ImportFrom) -> dict[str, str]:
    aliases: dict[str, str] = {}
    if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("sync_transactions"):
        aliases.update({alias.asname or alias.name: alias.name for alias in node.names if alias.name in _HELPERS})
    if isinstance(node, ast.Import):
        for item in node.names:
            if item.name.endswith("sync_transactions"):
                module = item.asname or item.name.rsplit(".", 1)[-1]
                aliases[module] = "sync_transactions"
                aliases.update({f"{module}.{name}": name for name in _HELPERS})
    return aliases


def _assignment_aliases(node: ast.Assign | ast.AnnAssign, aliases: dict[str, str]) -> dict[str, str]:
    value = node.value
    if value is None:
        return {}
    name = _helper_alias(value, aliases)
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return {} if name is None else dict.fromkeys(set().union(*map(_target_names, targets)), name)


def _aliases(root: ast.AST, inherited: dict[str, str] | None = None) -> dict[str, str]:
    aliases = dict(inherited or {})
    if isinstance(root, (ast.FunctionDef, ast.AsyncFunctionDef)) and root.name in _HELPERS:
        aliases[root.name] = root.name
    if isinstance(root, (ast.FunctionDef, ast.AsyncFunctionDef)) and root.name == "write_savepoint":
        aliases["write_transaction"] = "write_transaction"
    for node in _lexical_nodes(root):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            aliases.update(_import_aliases(node))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            aliases.update(_assignment_aliases(node, aliases))
    return aliases


def _connections(root: ast.AST) -> set[str]:
    names = set(_CONNECTION_NAMES)
    if isinstance(root, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = root.args
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            annotation = ast.unparse(arg.annotation) if arg.annotation is not None else ""
            if arg.arg in _CONNECTION_NAMES or "Connection" in annotation:
                names.add(arg.arg)
    for node in _lexical_nodes(root):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if value is None:
                continue
            source = _qualified(value)
            source_is_connection = (isinstance(value, ast.Name) and value.id in names) or (
                source is not None and source.endswith((".conn", "._conn"))
            )
            if isinstance(value, ast.Call):
                called = _qualified(value.func) or ""
                source_is_connection = source_is_connection or called.endswith(
                    ("connect", "_open_sync_db", "open_runtime_sync_db")
                )
            if source_is_connection:
                for target in targets:
                    names.update(_target_names(target))
    return names


def _is_connection_expr(node: ast.AST, names: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Attribute):
        return node.attr in _CONNECTION_NAMES or node.attr.endswith("_conn")
    if isinstance(node, ast.Call):
        return (_qualified(node.func) or "").rsplit(".", 1)[-1] in {"connect", "_open_sync_db", "open_runtime_sync_db"}
    return False


def _allowed_transaction(rel: Path, function: str) -> bool:
    if rel == Path("sync_transactions.py"):
        return True
    if rel == Path("sync_db.py"):
        return function in _SYNC_DB_BOOTSTRAP_TRANSACTIONS
    return function in TRANSACTION_EXCEPTIONS.get(rel, frozenset())


def _sqlite_connect_call(node: ast.Call, module_aliases: set[str], function_aliases: set[str]) -> bool:
    if isinstance(node.func, ast.Name):
        return node.func.id in function_aliases
    if isinstance(node.func, ast.Attribute) and node.func.attr == "connect":
        return (_qualified(node.func.value) or "") in module_aliases
    return False


def _call_parent_map(root: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(root) for child in ast.iter_child_nodes(parent)}


@dataclass(frozen=True, slots=True)
class _ScanContext:
    rel: Path
    parents: dict[ast.AST, ast.AST]
    values: dict[str, str]
    sqlite_modules: set[str]
    sqlite_connects: set[str]


@dataclass(frozen=True, slots=True)
class _ScopeContext:
    function: str
    allowed_tx: bool
    aliases: dict[str, str]
    conn_names: set[str]


def _context_helper_call(node: ast.AST, parent: dict[ast.AST, ast.AST], aliases: dict[str, str]) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = _qualified(node.func)
    helper = aliases.get(func or "")
    if helper is None and func:
        helper = aliases.get(func.rsplit(".", 1)[-1])
    item = parent.get(node)
    return (
        helper is not None
        and isinstance(item, ast.withitem)
        and item.context_expr is node
        and isinstance(parent.get(item), ast.With)
    )


def _owner_body_has_suspension(body: list[ast.stmt]) -> int | None:
    stack: list[ast.AST] = list(reversed(body))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, (ast.Await, ast.Yield, ast.YieldFrom, ast.AsyncWith, ast.AsyncFor)):
            return int(getattr(node, "lineno", 0))
        stack.extend(reversed(list(ast.iter_child_nodes(node))))
    return None


def _check_call(
    scan: _ScanContext,
    scope: _ScopeContext,
    node: ast.Call,
) -> list[str]:
    function, allowed_tx, aliases, conn_names = (scope.function, scope.allowed_tx, scope.aliases, scope.conn_names)
    rel = scan.rel
    line = int(getattr(node, "lineno", 0))
    called = _qualified(node.func) or ""
    tail = called.rsplit(".", 1)[-1]
    findings: list[str] = []
    helper = aliases.get(called) or aliases.get(tail)
    if tail in {"commit", "rollback", "executescript"} and not allowed_tx:
        findings.append(f"{rel}:{line}: raw SQLite {tail} in {function}")
    if helper in _HELPERS and not _context_helper_call(node, scan.parents, aliases):
        findings.append(f"{rel}:{line}: {helper} must be a direct with-context in {function}")
    findings.extend(_check_factory_call(scan, scope, node, tail, line))
    if tail in {"execute", "executemany", "executescript"} and node.args and not allowed_tx:
        sql = _string(node.args[0], scan.values)
        if sql is not None and _is_transaction_sql(sql):
            findings.append(f"{rel}:{line}: raw transaction SQL in {function}")
    if tail == "enter_context" and node.args and not allowed_tx and _is_connection_expr(node.args[0], conn_names):
        findings.append(f"{rel}:{line}: connection context managed through ExitStack in {function}")
    if tail in {"__enter__", "__exit__", "__aenter__", "__aexit__"} and not allowed_tx:
        findings.append(f"{rel}:{line}: manual context entry/exit in {function}")
    return findings


def _check_factory_call(scan: _ScanContext, scope: _ScopeContext, node: ast.Call, tail: str, line: int) -> list[str]:
    rel, function = scan.rel, scope.function
    short_name = function.rsplit(".", 1)[-1]
    findings: list[str] = []
    if (
        tail == "_open_sync_db"
        and short_name not in OPEN_SYNC_DB_CALLERS.get(rel, frozenset())
        and not (
            rel == Path("sync_db.py") and short_name in {"_open_sync_db", "open_sync_db_reader", "open_runtime_sync_db"}
        )
    ):
        findings.append(f"{rel}:{line}: legacy _open_sync_db call in {function}")
    allowed_factory = short_name in RUNTIME_FACTORY_FUNCTIONS.get(rel, frozenset())
    if (
        tail == "enable_runtime_writes"
        and not allowed_factory
        and not (rel == Path("sync_transactions.py") and function == "enable_runtime_writes")
    ):
        findings.append(f"{rel}:{line}: runtime write guard outside connection factory in {function}")
    if _sqlite_connect_call(node, scan.sqlite_modules, scan.sqlite_connects):
        if short_name not in CONNECT_EXCEPTIONS.get(rel, frozenset()):
            findings.append(f"{rel}:{line}: SQLite connection outside approved factory in {function}")
        elif rel == Path("__init__.py"):
            text = _string(node.args[0], scan.values) if node.args else None
            if not text or "mode=ro" not in text:
                findings.append(f"{rel}:{line}: feedback-list connection must be mode=ro")
    return findings


def _check_context(
    scan: _ScanContext,
    scope: _ScopeContext,
    node: ast.With | ast.AsyncWith,
) -> list[str]:
    function, allowed_tx, aliases, conn_names = (scope.function, scope.allowed_tx, scope.aliases, scope.conn_names)
    rel = scan.rel
    findings: list[str] = []
    line = int(getattr(node, "lineno", 0))
    for item in node.items:
        context = item.context_expr
        if _context_helper_call(context, scan.parents, aliases):
            if isinstance(node, ast.AsyncWith):
                findings.append(f"{rel}:{line}: async context around synchronous write helper in {function}")
            elif not allowed_tx:
                suspension = _owner_body_has_suspension(node.body)
                if suspension is not None:
                    findings.append(f"{rel}:{suspension}: suspension inside synchronous write context in {function}")
        elif isinstance(context, ast.Call) and (_qualified(context.func) or "").endswith("closing"):
            continue
        elif not allowed_tx and _is_connection_expr(context, conn_names):
            findings.append(f"{rel}:{line}: raw SQLite connection context in {function}")
    return findings


def _check_helper_reference(
    scan: _ScanContext,
    function: str,
    node: ast.Name | ast.Attribute,
    aliases: dict[str, str],
) -> list[str]:
    if isinstance(node, ast.Name):
        helper = aliases.get(node.id)
        if not isinstance(node.ctx, ast.Load) or helper not in _HELPERS:
            return []
    else:
        if node.attr not in _HELPERS or aliases.get(_qualified(node.value) or "") != "sync_transactions":
            return []
    caller = scan.parents.get(node)
    if isinstance(caller, ast.Call) and caller.func is node and _context_helper_call(caller, scan.parents, aliases):
        return []
    line = int(getattr(node, "lineno", 0))
    return [f"{scan.rel}:{line}: write helper reference escapes direct with-context in {function}"]


def _check_scope(
    scan: _ScanContext,
    function: str,
    root: ast.AST,
    *,
    aliases: dict[str, str],
) -> list[str]:
    rel = scan.rel
    conn_names = _connections(root)
    allowed_tx = _allowed_transaction(rel, function.rsplit(".", 1)[-1])
    scope = _ScopeContext(function, allowed_tx, aliases, conn_names)
    findings: list[str] = []
    for node in _lexical_nodes(root):
        if isinstance(node, ast.Call):
            findings.extend(
                _check_call(
                    scan,
                    scope,
                    node,
                )
            )
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            findings.extend(
                _check_context(
                    scan,
                    scope,
                    node,
                )
            )
        elif isinstance(node, (ast.Name, ast.Attribute)):
            findings.extend(_check_helper_reference(scan, function, node, aliases))
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and not allowed_tx:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Attribute) and target.attr == "isolation_level" for target in targets):
                findings.append(f"{rel}:{node.lineno}: runtime isolation_level toggle in {function}")
        if (
            isinstance(node, ast.AugAssign)
            and isinstance(node.target, ast.Attribute)
            and node.target.attr == "isolation_level"
            and not allowed_tx
        ):
            findings.append(f"{rel}:{node.lineno}: runtime isolation_level toggle in {function}")
    return findings


def _file_violations(path: Path, source_root: Path) -> list[str]:
    rel = path.relative_to(source_root)
    if rel in EXCLUDED_MODULES:
        return []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parents = _call_parent_map(tree)
    sqlite_modules = {"sqlite3"}
    sqlite_connects: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            sqlite_modules.update(alias.asname or "sqlite3" for alias in node.names if alias.name == "sqlite3")
        elif isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            sqlite_connects.update(alias.asname or alias.name for alias in node.names if alias.name == "connect")
    values: dict[str, str] = {}
    for _ in range(3):
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                text = _string(node.value, values)
                if text is not None:
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        values.update(dict.fromkeys(_target_names(target), text))
    module_aliases = _aliases(tree)
    scan = _ScanContext(rel, parents, values, sqlite_modules, sqlite_connects)
    scopes = [(name, node) for name, node in _function_nodes(tree.body)]
    scopes.append(("<module>", tree))
    findings: list[str] = []
    for function, root in scopes:
        aliases = _aliases(root, module_aliases)
        findings.extend(_check_scope(scan, function, root, aliases=aliases))
    if rel == Path("event_recovery.py"):
        findings.extend(_recovery_source_violations(tree, rel, values, sqlite_modules, sqlite_connects))
    return findings


def _recovery_source_violations(
    tree: ast.Module,
    rel: Path,
    values: dict[str, str],
    sqlite_modules: set[str],
    sqlite_connects: set[str],
) -> list[str]:
    findings: list[str] = []
    for function, root in _function_nodes(tree.body):
        if function.rsplit(".", 1)[-1] != "recover_events":
            continue
        for node in _lexical_nodes(root):
            if not isinstance(node, ast.Call) or not _sqlite_connect_call(node, sqlite_modules, sqlite_connects):
                continue
            text = _string(node.args[0], values) if node.args else None
            target = bool(node.args and isinstance(node.args[0], ast.Name) and node.args[0].id == "target_path")
            if not target and (text is None or "mode=ro" not in text):
                findings.append(f"{rel}:{node.lineno}: recovery source connection must be mode=ro")
    return findings


def boundary_violations(source_root: Path = SOURCE_ROOT) -> list[str]:
    findings = [
        finding for path in sorted(source_root.rglob("*.py")) for finding in _file_violations(path, source_root)
    ]
    return sorted(set(findings))


if __name__ == "__main__":
    violations = boundary_violations()
    if violations:
        raise SystemExit("Transaction boundary violations:\n" + "\n".join(violations))
    print("Transaction boundary check passed.")
