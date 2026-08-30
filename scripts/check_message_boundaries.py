#!/usr/bin/env python3
"""Enforce message/entity-table and agent-facing Telegram-content ownership.

Tach owns the Python import graph. This focused AST gate owns smaller contracts
that cannot be expressed by an import graph alone:

* SQL that reads ``messages`` belongs to a reviewed repository/query owner.
* Runtime DML for the exact ``entities`` table belongs to ``entity_store``.
* The structured Telegram-content marker is constructed by its canonical
  wrapper, rather than by each tool independently.
* List and Inbox message envelopes are constructed by their canonical delivery
  presenter, including when consumer code delegates to a local helper.

The SQL check deliberately parses only SQL-shaped assignments and arguments to
the sqlite execute family.  It does not grep all source strings, which keeps
ordinary prose, output schemas, and ``messages_fts`` out of the policy.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src" / "mcp_telegram"

# These are the current reviewed repository/query owners.  Adding a new owner
# is intentionally a policy change: update this set and its focused tests.
MESSAGE_SQL_OWNER_PATHS = frozenset(
    {
        "daemon_account_trace.py",
        "daemon_activity_stats.py",
        "daemon_dialog_queries.py",
        "daemon_entity_info.py",
        "reading/sqlite_projection.py",
        "folders/read_repository.py",
        "fts.py",
        "message_fact_refresh.py",
        "messages/sqlite_repository.py",
    }
)

# Existing lifecycle SQL is named separately instead of silently becoming a
# general-purpose owner. These are a ratchet frontier for a later cleanup
# slice; new files may not join this set accidentally.
MESSAGE_SQL_LEGACY_EXCEPTION_PATHS = frozenset(
    {
        "daemon.py",
        "delta_sync.py",
    }
)

# Schema/migration DDL has a distinct owner from runtime message persistence.
MESSAGE_SQL_SCHEMA_OWNER_PATHS = frozenset({"sync_db.py"})

ENTITY_DML_OWNER_PATHS = frozenset({"entity_store.py"})
ENTITY_DML_SCHEMA_OWNER_PATHS = frozenset({"sync_db.py"})

CONTENT_WRAPPER_PATH = "tools/structured.py"
CONTENT_PROJECTOR_PATH = "message_content.py"
RAW_PROJECTOR_PATH = "telegram_message_projection.py"
SCHEDULED_CONTENT_PATH = "reading/scheduled_projection.py"
SCHEDULED_CONTENT_ENTRYPOINT = "scheduled_row_to_wire"
SCHEDULED_PROJECTOR_NAME = "project_read_message_content"
TEXT_PROJECTOR_PATH = "text_projection.py"
TEXT_PROJECTOR_IMPORTERS = frozenset({"daemon_message.py", "message_content.py", RAW_PROJECTOR_PATH})
RAW_PROJECTOR_IMPORTERS = frozenset({"daemon_message.py", "telegram_history.py"})
MESSAGE_BODY_SERIALIZER_PATHS = frozenset(
    {
        "tools/reading.py",
        "tools/unread.py",
        "tools/folders.py",
        "tools/activity.py",
        "tools/account_trace.py",
    }
)
MESSAGE_BODY_ENTRYPOINTS = {
    "tools/reading.py": frozenset({"_list_message_structured_item"}),
    "tools/unread.py": frozenset({"_structured_messages"}),
    "tools/folders.py": frozenset({"list_folder_messages"}),
    "tools/activity.py": frozenset({"_structured_comment"}),
    "tools/account_trace.py": frozenset({"_attach_trace_content_metadata"}),
}
CANONICAL_MESSAGE_VIEW_ENTRYPOINTS = {
    "tools/reading.py": frozenset({"_list_message_structured_item"}),
    "tools/unread.py": frozenset({"_structured_messages"}),
}
CANONICAL_MESSAGE_VIEW_FIELDS = frozenset(
    {
        "dialog_id",
        "msg_id",
        "sent_at",
        "sender",
        "out",
        "is_service",
        "topic",
        "content",
        "media",
        "reply_context_ref",
        "forward",
        "post_author",
        "edit_date",
        "reactions",
        "reaction_events",
        "reaction_events_status",
        "read_at",
        "read_markers",
    }
)
CANONICAL_MESSAGE_VIEW_CONSUMER_PATHS = frozenset(CANONICAL_MESSAGE_VIEW_ENTRYPOINTS)
SEARCH_HIT_CONSUMER_PATH = "tools/reading.py"
SEARCH_HIT_ENTRYPOINT = "_search_result_structured_rows"
LIST_MESSAGE_LIFECYCLE_FIELDS = frozenset(
    {
        "message_state",
        "visibility",
        "unpublished",
        "published",
        "unseen",
        "scheduled_at",
        "published_at",
        "inclusion_basis",
    }
)
MESSAGE_VIEW_BYPASS_NAMES = frozenset(
    {
        "serialize_message_content",
        "project_topic",
        "_compute_inline_markers",
        "_structured_read_marker",
        "_structured_reaction_events",
        "_structured_reactions",
    }
)
TOOL_MESSAGE_PROJECTOR_PATHS = frozenset({"tools/activity.py", "tools/account_trace.py"})
MESSAGE_METADATA_FUNCTIONS = {
    "tools/folders.py": frozenset({"list_folders", "list_folder_messages"}),
    "tools/reading.py": frozenset({"_topic_candidate_payload"}),
    "tools/unread.py": frozenset({"_structured_reactions"}),
}
MESSAGE_BODY_SURFACE_FUNCTION_NAMES = frozenset(
    {"get_inbox", "list_messages", "list_folder_messages", "_structured_messages"}
)

_EXECUTE_METHODS = frozenset({"execute", "executemany", "executescript"})
_SQL_NAME = re.compile(r"(?:^|_)(?:SQL|DDL|QUERY)(?:$|_)", re.IGNORECASE)
_SQL_START = re.compile(r"^(?:SELECT|WITH|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b", re.IGNORECASE)
_SQL_TOKEN = re.compile(
    r"--[^\n]*|/\*.*?\*/|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|[A-Za-z_][A-Za-z0-9_$]*|[().]",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


@dataclass(frozen=True, slots=True)
class _SqlSnippet:
    text: str
    line: int


def _line(node: ast.AST) -> int:
    if isinstance(node, (ast.expr, ast.stmt)):
        return node.lineno
    return 1


def _relative(path: Path, source_root: Path = SOURCE_ROOT) -> str:
    return path.resolve().relative_to(source_root.resolve()).as_posix()


def _literal_string(node: ast.expr | None, constants: Mapping[str, str]) -> str | None:
    """Resolve simple SQL expressions while preserving dynamic f-string gaps."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                parts.append(" __dynamic__ ")
            else:
                return None
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_string(node.left, constants)
        right = _literal_string(node.right, constants)
        return None if left is None or right is None else left + right
    return None


def _assignment_names(node: ast.Assign | ast.AnnAssign) -> Iterator[str]:
    targets: Iterable[ast.expr]
    if isinstance(node, ast.Assign):
        targets = node.targets
    else:
        targets = (node.target,)
    for target in targets:
        if isinstance(target, ast.Name):
            yield target.id


def _static_constants(tree: ast.AST) -> dict[str, str]:
    """Collect simple string aliases, including local SQL builder variables."""
    constants: dict[str, str] = {}
    assignments = [node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))]
    for _ in range(len(assignments) + 1):
        changed = False
        for node in assignments:
            value = _literal_string(node.value, constants)
            if value is None:
                continue
            for name in _assignment_names(node):
                if constants.get(name) != value:
                    constants[name] = value
                    changed = True
        if not changed:
            break
    return constants


def _looks_like_sql(text: str, *, name: str | None = None) -> bool:
    return bool(name and _SQL_NAME.search(name)) or bool(_SQL_START.match(text.strip()))


def _sql_tokens(sql: str) -> list[str]:
    tokens: list[str] = []
    for match in _SQL_TOKEN.finditer(sql):
        token = match.group(0)
        if token.startswith(("--", "/*")):
            continue
        tokens.append(token)
    return tokens


def _identifier(token: str) -> str:
    if token.startswith(('"', "`")) and token.endswith(token[0]):
        return token[1:-1].replace(token[0] * 2, token[0])
    if token.startswith("[") and token.endswith("]"):
        return token[1:-1]
    return token


_MESSAGE_SQL_TABLES = frozenset({"messages", "message_versions"})
_MESSAGE_SQL_TABLE_PRECEDERS = frozenset({"from", "join", "update", "into"})
_SQLITE_UPDATE_CONFLICTS = frozenset({"rollback", "abort", "fail", "ignore", "replace"})


def _has_message_table_sql(sql: str) -> bool:
    """Return whether DML in *sql* addresses a message-owned table.

    The gate intentionally recognizes the table positions for SELECT/JOIN,
    UPDATE, INSERT, and DELETE. ``messages_fts`` is a separate FTS owner and
    is not conflated with the canonical message tables here.
    """
    tokens = _sql_tokens(sql)
    for index, token in enumerate(tokens):
        if token.casefold() not in _MESSAGE_SQL_TABLE_PRECEDERS or index + 1 >= len(tokens):
            continue
        next_index = index + 1
        if token.casefold() == "update" and tokens[next_index].casefold() == "or":
            if next_index + 2 >= len(tokens) or tokens[next_index + 1].casefold() not in _SQLITE_UPDATE_CONFLICTS:
                continue
            next_index += 2
        table = _identifier(tokens[next_index]).casefold()
        if table in _MESSAGE_SQL_TABLES:
            return True
        # Permit a qualified SQLite table name such as main.messages.
        if (
            next_index + 2 < len(tokens)
            and tokens[next_index + 1] == "."
            and _identifier(tokens[next_index + 2]).casefold() in _MESSAGE_SQL_TABLES
        ):
            return True
    return False


def _is_exact_table_at(tokens: list[str], index: int, table_name: str) -> bool:
    if index >= len(tokens):
        return False
    if _identifier(tokens[index]).casefold() == table_name:
        return True
    return (
        index + 2 < len(tokens) and tokens[index + 1] == "." and _identifier(tokens[index + 2]).casefold() == table_name
    )


def _top_level_statement_index(tokens: list[str]) -> int | None:
    """Locate the statement following an optional sequence of CTEs."""
    if not tokens:
        return None
    if tokens[0].casefold() != "with":
        return 0

    depth = 0
    for index, token in enumerate(tokens[1:], start=1):
        if token == "(":
            depth += 1
        elif token == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and token.casefold() in {"delete", "insert", "replace", "select", "update"}:
            return index
    return None


def _has_entity_table_dml(sql: str) -> bool:
    """Return whether DML in *sql* writes the exact ``entities`` table."""
    tokens = _sql_tokens(sql)
    statement_index = _top_level_statement_index(tokens)
    if statement_index is None:
        return False
    statement = tokens[statement_index].casefold()
    if statement in {"insert", "replace"}:
        table_indices = [
            index + 1
            for index, token in enumerate(tokens[statement_index + 1 :], start=statement_index + 1)
            if token.casefold() == "into"
        ]
        return bool(table_indices) and _is_exact_table_at(tokens, table_indices[0], "entities")
    if statement == "update":
        table_index = statement_index + 1
        if table_index < len(tokens) and tokens[table_index].casefold() == "or":
            if table_index + 2 >= len(tokens) or tokens[table_index + 1].casefold() not in _SQLITE_UPDATE_CONFLICTS:
                return False
            table_index += 2
        return _is_exact_table_at(tokens, table_index, "entities")
    if statement == "delete":
        table_index = statement_index + 2
        return (
            statement_index + 1 < len(tokens)
            and tokens[statement_index + 1].casefold() == "from"
            and _is_exact_table_at(tokens, table_index, "entities")
        )
    return False


def _sql_snippets(tree: ast.AST, constants: Mapping[str, str]) -> list[_SqlSnippet]:
    snippets: list[_SqlSnippet] = []
    seen: set[tuple[str, int]] = set()

    def add(node: ast.AST, text: str | None, *, name: str | None = None) -> None:
        if text is None or not _looks_like_sql(text, name=name):
            return
        key = (text, _line(node))
        if key not in seen:
            seen.add(key)
            snippets.append(_SqlSnippet(text=text, line=_line(node)))

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            names = tuple(_assignment_names(node))
            text = _literal_string(node.value, constants)
            for name in names or (None,):
                add(node, text, name=name)
        elif isinstance(node, ast.Return):
            add(node, _literal_string(node.value, constants))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _EXECUTE_METHODS and node.args:
                add(node, _literal_string(node.args[0], constants))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _SQL_START.match(node.value.strip()):
            # Migration lists and other execute-family argument containers may
            # hold SQL strings without assigning each item a name.  Restrict
            # this fallback to strings that begin with a SQL statement keyword.
            add(node, node.value)
    return snippets


def _dict_string_keys(node: ast.Dict) -> dict[str, ast.expr]:
    result: dict[str, ast.expr] = {}
    for key, value in zip(node.keys, node.values, strict=True):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            result[key.value] = value
    return result


def _is_true(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _is_content_wrapper_call(node: ast.Call) -> bool:
    if not (isinstance(node.func, ast.Name) and node.func.id == "dict"):
        return False
    keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg is not None}
    return _is_true(keywords.get("is_telegram_content")) and "content_kind" in keywords


_MANUAL_CONTENT_CONSTRUCTORS = frozenset({"MessageContent", "TelegramContent"})


def _manual_content_bindings(tree: ast.AST) -> tuple[set[str], set[str]]:  # noqa: PLR0912
    """Resolve local aliases for MessageContent/TelegramContent constructors."""
    aliases = set(_MANUAL_CONTENT_CONSTRUCTORS)
    module_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name.endswith(".message_content"):
                    module_aliases.add(imported.name)
                    module_aliases.add(imported.asname or imported.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.ImportFrom):
            module = _import_module(node)
            if module.endswith("message_content"):
                for imported in node.names:
                    if imported.name in _MANUAL_CONTENT_CONSTRUCTORS:
                        aliases.add(imported.asname or imported.name)

    for _ in range(len(aliases) + 1):
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            source_name: str | None = None
            if isinstance(value, ast.Name) and value.id in aliases:
                source_name = value.id
            elif isinstance(value, ast.Attribute) and value.attr in _MANUAL_CONTENT_CONSTRUCTORS:
                dotted = _dotted_name(value.value)
                if dotted in module_aliases:
                    source_name = value.attr
            if source_name is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases.add(target.id)
                    changed = True
        if not changed:
            break
    return aliases, module_aliases


def _is_manual_content_constructor(
    node: ast.Call,
    *,
    aliases: set[str],
    module_aliases: set[str],
) -> bool:
    if isinstance(node.func, ast.Name):
        return node.func.id in aliases
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr in _MANUAL_CONTENT_CONSTRUCTORS
        and _dotted_name(node.func.value) in module_aliases
    )


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix is not None else None
    return None


def _content_violations(path: str, tree: ast.AST) -> list[Finding]:
    findings: list[Finding] = []
    constructor_aliases, constructor_modules = _manual_content_bindings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = _dict_string_keys(node)
            if _is_true(keys.get("is_telegram_content")) and "content_kind" in keys and path != CONTENT_WRAPPER_PATH:
                findings.append(
                    Finding(
                        path, node.lineno, "Telegram content dictionaries must use tools.structured.telegram_content"
                    )
                )
        elif isinstance(node, ast.Call) and _is_content_wrapper_call(node) and path != CONTENT_WRAPPER_PATH:
            findings.append(
                Finding(path, node.lineno, "Telegram content dictionaries must use tools.structured.telegram_content")
            )
        elif (
            isinstance(node, ast.Call)
            and _is_manual_content_constructor(node, aliases=constructor_aliases, module_aliases=constructor_modules)
            and path != CONTENT_PROJECTOR_PATH
        ):
            findings.append(
                Finding(path, node.lineno, "MessageContent must be produced by message_content.project_message_content")
            )
    return findings


def _import_module(node: ast.ImportFrom) -> str:
    prefix = "." * node.level
    return prefix + (node.module or "")


def _projection_import_violations(path: str, tree: ast.AST) -> list[Finding]:
    findings: list[Finding] = []
    for node in ast.walk(tree):
        module_names: list[str] = []
        imported_names: list[str] = []
        if isinstance(node, ast.Import):
            module_names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module_names = [_import_module(node)]
            imported_names = [alias.name for alias in node.names]

        if (
            any(name.endswith(TEXT_PROJECTOR_PATH.replace("/", ".").removesuffix(".py")) for name in module_names)
            and path not in TEXT_PROJECTOR_IMPORTERS
            and "render_text_links" in imported_names
        ):
            findings.append(Finding(path, _line(node), "render_text_links has a single canonical projection seam"))
        if (
            any(name.endswith(RAW_PROJECTOR_PATH.replace("/", ".").removesuffix(".py")) for name in module_names)
            and path not in RAW_PROJECTOR_IMPORTERS
        ):
            findings.append(Finding(path, _line(node), "raw Telegram message projection has a canonical owner"))
        if path in TOOL_MESSAGE_PROJECTOR_PATHS and (
            any(name.endswith("message_content") for name in module_names) or "message_content" in imported_names
        ):
            findings.append(
                Finding(path, _line(node), "tool delivery paths must not import message_content projectors")
            )
        if path in TOOL_MESSAGE_PROJECTOR_PATHS and isinstance(node, ast.Call):
            called_name = _dotted_name(node.func)
            if called_name == "project_message_content" or (
                isinstance(node.func, ast.Attribute) and node.func.attr == "project_message_content"
            ):
                findings.append(
                    Finding(path, _line(node), "tool delivery paths must not call message_content projectors")
                )
    return findings


def _scheduled_import_violations(path: str, tree: ast.AST) -> tuple[list[Finding], bool]:
    findings: list[Finding] = []
    canonical_import = False
    private_symbols = {"project_message_content", "render_text_links", "serialize_message_content", "telegram_content"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        imported_names = {alias.name for alias in node.names}
        if imported_names & private_symbols:
            findings.append(
                Finding(path, node.lineno, "scheduled content must not import private renderer/serializer symbols")
            )
        if SCHEDULED_PROJECTOR_NAME not in imported_names:
            continue
        if _import_module(node) != "..daemon_message":
            findings.append(Finding(path, node.lineno, "scheduled projector must come directly from ..daemon_message"))
        elif any(alias.name == SCHEDULED_PROJECTOR_NAME and alias.asname is None for alias in node.names):
            canonical_import = True
    return findings, canonical_import


def _scheduled_shadow_violations(path: str, tree: ast.AST) -> list[Finding]:
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == SCHEDULED_PROJECTOR_NAME
        ):
            findings.append(Finding(path, node.lineno, "canonical projector binding is shadowed locally"))
            continue
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == SCHEDULED_PROJECTOR_NAME for target in targets):
            findings.append(Finding(path, _line(node), "canonical projector binding is shadowed locally"))
    return findings


def _scheduled_content_violations(path: str, tree: ast.AST) -> list[Finding]:
    """Keep scheduled rows on the canonical projection seam."""
    if path != SCHEDULED_CONTENT_PATH:
        return []

    findings, canonical_import = _scheduled_import_violations(path, tree)
    findings.extend(_scheduled_shadow_violations(path, tree))
    module_body = tree.body if isinstance(tree, ast.Module) else ()
    mapper_nodes = [
        node
        for node in module_body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == SCHEDULED_CONTENT_ENTRYPOINT
    ]

    if not canonical_import:
        findings.append(Finding(path, 1, "scheduled projector must be imported exactly from ..daemon_message"))
    if len(mapper_nodes) != 1:
        findings.append(
            Finding(path, 1, "scheduled content must have exactly one module-level scheduled_row_to_wire entrypoint")
        )
    if mapper_nodes and not any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == SCHEDULED_PROJECTOR_NAME
        for node in ast.walk(mapper_nodes[0])
    ):
        findings.append(
            Finding(
                path,
                mapper_nodes[0].lineno,
                "scheduled message content must call project_read_message_content directly",
            )
        )
    return findings


def _raw_message_content_violations(path: str, tree: ast.AST) -> list[Finding]:
    """Reject message-body wrapper bypasses and require the shared serializer."""
    findings: list[Finding] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.function: str | None = None
            self.tainted: set[str] = set()
            self.serializer_counts: dict[str, int] = {}
            self.message_view_counts: dict[str, int] = {}
            self.content_aliases = {"telegram_content"}

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if _import_module(node).endswith(("tools.structured", ".structured")):
                for alias in node.names:
                    if alias.name == "telegram_content":
                        self.content_aliases.add(alias.asname or alias.name)
            self.generic_visit(node)

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            previous, old_tainted = self.function, self.tainted
            self.function, self.tainted = node.name, set()
            self.generic_visit(node)
            if node.name in MESSAGE_BODY_ENTRYPOINTS.get(path, frozenset()):
                message_view_count = self.message_view_counts.get(node.name, 0)
                if node.name in CANONICAL_MESSAGE_VIEW_ENTRYPOINTS.get(path, frozenset()):
                    if message_view_count == 0:
                        findings.append(
                            Finding(path, node.lineno, "canonical message delivery path must call project_message_view")
                        )
                    elif message_view_count != 1:
                        findings.append(
                            Finding(
                                path,
                                node.lineno,
                                "canonical message delivery path must call project_message_view exactly once",
                            )
                        )
                elif self.serializer_counts.get(node.name, 0) == 0:
                    findings.append(
                        Finding(path, node.lineno, "message delivery path must call serialize_message_content")
                    )
                elif path in TOOL_MESSAGE_PROJECTOR_PATHS and self.serializer_counts.get(node.name, 0) != 1:
                    findings.append(
                        Finding(
                            path,
                            node.lineno,
                            "canonical message delivery path must call serialize_message_content exactly once",
                        )
                    )
            self.function, self.tainted = previous, old_tainted

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_function(node)

        def visit_Assign(self, node: ast.Assign) -> None:
            if isinstance(node.value, ast.Name) and node.value.id in self.content_aliases:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.content_aliases.add(target.id)
            value = node.value
            tainted = self._message_tainted(value)
            if tainted:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.tainted.add(target.id)
            self.generic_visit(node)

        def _message_tainted(self, node: ast.expr) -> bool:  # noqa: PLR0911
            """Conservatively track body values through common row adapters."""
            if isinstance(node, ast.Name):
                return node.id in self.tainted
            if isinstance(node, ast.Attribute):
                return node.attr in {"text", "media_description"} or self._message_tainted(node.value)
            if isinstance(node, ast.Subscript):
                return self._message_tainted(node.value) or self._message_tainted(node.slice)
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in {"str", "cast"}:
                    return any(self._message_tainted(arg) for arg in node.args)
                if isinstance(node.func, ast.Attribute) and node.func.attr in {"get", "__getitem__"}:
                    if (
                        node.func.attr == "get"
                        and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and node.args[0].value in {"text", "media_description"}
                    ):
                        return True
                    return any(self._message_tainted(arg) for arg in node.args) or self._message_tainted(
                        node.func.value
                    )
                return False
            return False

        def visit_Call(self, node: ast.Call) -> None:
            called_name = node.func.id if isinstance(node.func, ast.Name) else None
            if called_name == "serialize_message_content":
                function = self.function or ""
                self.serializer_counts[function] = self.serializer_counts.get(function, 0) + 1
            if called_name == "project_message_view":
                function = self.function or ""
                self.message_view_counts[function] = self.message_view_counts.get(function, 0) + 1
            if (
                self.function in CANONICAL_MESSAGE_VIEW_ENTRYPOINTS.get(path, frozenset())
                and called_name in MESSAGE_VIEW_BYPASS_NAMES
            ):
                findings.append(
                    Finding(path, node.lineno, "canonical message delivery path must use project_message_view")
                )
            is_content_call = isinstance(node.func, ast.Name) and node.func.id in self.content_aliases
            is_content_call = is_content_call or (
                isinstance(node.func, ast.Attribute) and node.func.attr == "telegram_content"
            )
            if is_content_call:
                if (
                    path not in MESSAGE_BODY_SERIALIZER_PATHS
                    and self.function not in MESSAGE_BODY_SURFACE_FUNCTION_NAMES
                ):
                    self.generic_visit(node)
                    return
                allowed = self.function in MESSAGE_METADATA_FUNCTIONS.get(path, frozenset())
                arg = node.args[0] if node.args else None
                message_arg = isinstance(arg, ast.expr) and self._message_tainted(arg)
                # Delivery entrypoints may only use the shared serializer; a
                # no-op serializer call must not excuse a second raw wrapper.
                if self.function in MESSAGE_BODY_ENTRYPOINTS.get(path, frozenset()) and not allowed:
                    message_arg = True
                kind = node.args[1].value if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) else None
                metadata_call = allowed and kind in {"snippet", "reaction", "message_text"}
                if message_arg or (not allowed and not metadata_call):
                    findings.append(Finding(path, node.lineno, "message bodies must use serialize_message_content"))
            self.generic_visit(node)

    Visitor().visit(tree)
    return findings


def _mapping_key_facts(node: ast.expr | None) -> tuple[frozenset[str], bool]:
    """Return visible keys and whether a mapping has opaque key sources."""
    if isinstance(node, ast.Dict):
        keys: set[str] = set()
        has_opaque_sources = False
        for key in node.keys:
            if key is None or not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                has_opaque_sources = True
            else:
                keys.add(key.value)
        return frozenset(keys), has_opaque_sources
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict":
        positional_keys, positional_opaque = (frozenset(), False) if not node.args else _mapping_key_facts(node.args[0])
        keyword_keys = frozenset(keyword.arg for keyword in node.keywords if keyword.arg is not None)
        has_opaque_sources = (
            len(node.args) > 1 or positional_opaque or any(keyword.arg is None for keyword in node.keywords)
        )
        return positional_keys | keyword_keys, has_opaque_sources
    return frozenset(), True


def _literal_subscript_key(node: ast.expr) -> str | None:
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
        return node.slice.value
    return None


def _canonical_message_view_violations(path: str, tree: ast.AST) -> list[Finding]:
    """Reject literal presenter-field construction or mutation in List/Inbox consumers.

    This deliberately does not trace aliases or values. A shallow whole-module
    rule catches local-helper bypasses while keeping the gate predictable.
    """
    if path not in CANONICAL_MESSAGE_VIEW_CONSUMER_PATHS:
        return []

    findings: list[Finding] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.function: str | None = None
            self.canonical_import_count = 0

        def visit_Import(self, node: ast.Import) -> None:
            if any(alias.name.endswith(".tools.message_view") for alias in node.names):
                findings.append(
                    Finding(path, node.lineno, "project_message_view must use its canonical direct import and name")
                )

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if not _import_module(node).endswith(("tools.message_view", ".message_view")):
                return
            for alias in node.names:
                if alias.name != "project_message_view":
                    continue
                if alias.asname is None:
                    self.canonical_import_count += 1
                else:
                    findings.append(
                        Finding(path, node.lineno, "project_message_view must use its canonical direct import and name")
                    )

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            previous = self.function
            self.function = node.name
            self.generic_visit(node)
            self.function = previous

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_function(node)

        def _record_mutation(self, node: ast.AST, key: str | None) -> None:
            if key in CANONICAL_MESSAGE_VIEW_FIELDS:
                findings.append(
                    Finding(path, _line(node), f"canonical field {key!r} must be owned by project_message_view")
                )
            if (
                self.function == "_list_message_structured_item"
                and key is not None
                and key not in LIST_MESSAGE_LIFECYCLE_FIELDS
            ):
                findings.append(
                    Finding(
                        path, _line(node), f"list message extension field {key!r} is not an allowed lifecycle field"
                    )
                )

        def _record_construction(self, node: ast.AST, keys: frozenset[str]) -> None:
            if self.function is None:
                return
            if self.function == "_list_message_structured_item":
                findings.extend(
                    Finding(
                        path, _line(node), f"list message extension field {key!r} is not an allowed lifecycle field"
                    )
                    for key in sorted(keys - LIST_MESSAGE_LIFECYCLE_FIELDS)
                )
            core_fields = keys & CANONICAL_MESSAGE_VIEW_FIELDS
            if core_fields == {"dialog_id"}:
                return
            is_list_filter = (
                core_fields <= {"sender", "topic"}
                and {
                    "exact_dialog_id",
                    "sender_id",
                    "exact_topic_id",
                }
                <= keys
            )
            if is_list_filter:
                return
            for key in sorted(core_fields):
                self._record_mutation(node, key)

        def visit_Dict(self, node: ast.Dict) -> None:
            keys, _has_opaque_sources = _mapping_key_facts(node)
            self._record_construction(node, keys)
            self.generic_visit(node)

        def visit_Assign(self, node: ast.Assign) -> None:
            for target in node.targets:
                self._record_mutation(node, _literal_subscript_key(target))
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            self._record_mutation(node, _literal_subscript_key(node.target))
            self.generic_visit(node)

        def visit_AugAssign(self, node: ast.AugAssign) -> None:
            self._record_mutation(node, _literal_subscript_key(node.target))
            if isinstance(node.op, ast.BitOr):
                keys, has_opaque_sources = _mapping_key_facts(node.value)
                if has_opaque_sources:
                    findings.append(
                        Finding(path, node.lineno, "message view |= keys must be a statically known literal mapping")
                    )
                for key in sorted(keys):
                    self._record_mutation(node, key)
            self.generic_visit(node)

        def visit_Delete(self, node: ast.Delete) -> None:
            for target in node.targets:
                self._record_mutation(node, _literal_subscript_key(target))
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Name) and node.func.id == "dict":
                keys, _has_opaque_sources = _mapping_key_facts(node)
                self._record_construction(node, keys)
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"pop", "setdefault"}:
                key = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else None
                self._record_mutation(node, key if isinstance(key, str) else None)
            if isinstance(node.func, ast.Attribute) and node.func.attr == "update":
                mapping_keys, mapping_opaque = _mapping_key_facts(node.args[0]) if node.args else (frozenset(), False)
                # Deliberately shallow and fail-closed: ordinary presenter
                # extensions use one literal mapping; this is not data-flow analysis.
                has_unknown_keys = (
                    len(node.args) > 1 or mapping_opaque or any(keyword.arg is None for keyword in node.keywords)
                )
                if has_unknown_keys:
                    findings.append(
                        Finding(
                            path, node.lineno, "message view update keys must be a statically known literal mapping"
                        )
                    )
                keyword_keys = frozenset(keyword.arg for keyword in node.keywords if keyword.arg is not None)
                for key in sorted(mapping_keys | keyword_keys):
                    self._record_mutation(node, key)
            self.generic_visit(node)

    visitor = Visitor()
    visitor.visit(tree)
    if visitor.canonical_import_count != 1:
        findings.append(Finding(path, 1, "consumer must import project_message_view directly exactly once"))
    return findings


def _search_schema_uses_canonical_hit(tree: ast.Module) -> bool:
    assignments = [
        node
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and "SEARCH_MESSAGES_OUTPUT_SCHEMA" in _assignment_names(node)
    ]
    if len(assignments) != 1 or not isinstance(assignments[0].value, ast.Dict):
        return False
    schema = _dict_string_keys(assignments[0].value)
    properties = schema.get("properties")
    if not isinstance(properties, ast.Dict):
        return False
    results = _dict_string_keys(properties).get("results")
    if not isinstance(results, ast.Dict):
        return False
    items = _dict_string_keys(results).get("items")
    return isinstance(items, ast.Name) and items.id == "SEARCH_HIT_SCHEMA"


def _search_mapper_uses_canonical_hit(tree: ast.Module) -> bool:
    entrypoints = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == SEARCH_HIT_ENTRYPOINT
    ]
    if len(entrypoints) != 1:
        return False
    returns = [node for node in entrypoints[0].body if isinstance(node, ast.Return)]
    if len(returns) != 1 or not isinstance(returns[0].value, ast.ListComp):
        return False
    item = returns[0].value.elt
    return isinstance(item, ast.Call) and isinstance(item.func, ast.Name) and item.func.id == "project_search_hit"


def _search_hit_violations(path: str, tree: ast.AST) -> list[Finding]:
    """Keep reading's search schema and result mapper on the canonical seam."""
    if path != SEARCH_HIT_CONSUMER_PATH or not isinstance(tree, ast.Module):
        return []

    findings: list[Finding] = []
    canonical_imports = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and _import_module(node).endswith(("tools.search_hit", ".search_hit"))
        for alias in node.names
        if alias.asname is None
    }
    missing_imports = {"SEARCH_HIT_SCHEMA", "project_search_hit"} - canonical_imports
    if missing_imports:
        findings.append(
            Finding(path, 1, "reading must directly import canonical SEARCH_HIT_SCHEMA and project_search_hit")
        )
    if not _search_schema_uses_canonical_hit(tree):
        findings.append(Finding(path, 1, "search results.items must reference SEARCH_HIT_SCHEMA"))
    if not _search_mapper_uses_canonical_hit(tree):
        findings.append(
            Finding(
                path,
                1,
                "search result mapper must directly project every list-comprehension item with project_search_hit",
            )
        )
    return findings


def violations_for(path: Path, source: str) -> list[Finding]:
    relative = _relative(path)
    tree = ast.parse(source, filename=str(path))
    constants = _static_constants(tree)
    findings = _content_violations(relative, tree)
    findings.extend(_projection_import_violations(relative, tree))
    findings.extend(_scheduled_content_violations(relative, tree))
    findings.extend(_raw_message_content_violations(relative, tree))
    findings.extend(_canonical_message_view_violations(relative, tree))
    findings.extend(_search_hit_violations(relative, tree))

    sql_hits = [snippet for snippet in _sql_snippets(tree, constants) if _has_message_table_sql(snippet.text)]
    if (
        sql_hits
        and relative not in MESSAGE_SQL_OWNER_PATHS
        and relative not in MESSAGE_SQL_LEGACY_EXCEPTION_PATHS
        and relative not in MESSAGE_SQL_SCHEMA_OWNER_PATHS
    ):
        findings.extend(
            Finding(
                relative,
                snippet.line,
                "direct FROM/JOIN messages or UPDATE/INSERT/DELETE messages/message_versions SQL is outside a reviewed owner",
            )
            for snippet in sql_hits
        )
    entity_dml_hits = [snippet for snippet in _sql_snippets(tree, constants) if _has_entity_table_dml(snippet.text)]
    if entity_dml_hits and relative not in ENTITY_DML_OWNER_PATHS and relative not in ENTITY_DML_SCHEMA_OWNER_PATHS:
        findings.extend(
            Finding(
                relative,
                snippet.line,
                "direct INSERT/UPDATE/DELETE/REPLACE entities SQL is outside canonical entity_store.py ownership",
            )
            for snippet in entity_dml_hits
        )
    return findings


def boundary_violations(source_root: Path = SOURCE_ROOT) -> list[Finding]:
    findings: list[Finding] = []
    observed_sql_paths: set[str] = set()
    for path in sorted(source_root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        relative = _relative(path, source_root)
        tree = ast.parse(source, filename=str(path))
        constants = _static_constants(tree)
        if any(_has_message_table_sql(snippet.text) for snippet in _sql_snippets(tree, constants)):
            observed_sql_paths.add(relative)
        findings.extend(violations_for(path, source))

    findings.extend(
        Finding(path, 1, "stale message SQL owner/legacy exception entry")
        for path in stale_sql_allowlist_entries(
            observed_sql_paths,
            schema_paths=MESSAGE_SQL_SCHEMA_OWNER_PATHS,
        )
    )
    return findings


def stale_sql_allowlist_entries(
    observed_paths: Iterable[str],
    *,
    owner_paths: Iterable[str] = MESSAGE_SQL_OWNER_PATHS,
    legacy_paths: Iterable[str] = MESSAGE_SQL_LEGACY_EXCEPTION_PATHS,
    schema_paths: Iterable[str] = (),
) -> list[str]:
    """Return reviewed owner/exception paths that no longer contain message SQL."""
    allowed_paths = set(owner_paths) | set(legacy_paths) | set(schema_paths)
    return sorted(allowed_paths - set(observed_paths))


def main() -> int:
    findings = boundary_violations()
    if findings:
        print(
            "Message boundary violations:",
            *[f"- {finding.render()}" for finding in findings],
            sep="\n",
            file=sys.stderr,
        )
        return 1
    print("Message boundary check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
