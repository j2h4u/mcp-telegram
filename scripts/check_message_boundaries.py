#!/usr/bin/env python3
"""Enforce message-table and agent-facing Telegram-content ownership.

Tach owns the Python import graph.  This focused AST gate owns two smaller
contracts that cannot be expressed by an import graph alone:

* SQL that reads ``messages`` belongs to a reviewed repository/query owner.
* The structured Telegram-content marker is constructed by its canonical
  wrapper, rather than by each tool independently.

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
        "daemon_message_queries.py",
        "daemon_read_state_queries.py",
        "daemon_source_export.py",
        "folders/sqlite_repository.py",
        "fts.py",
        "message_fact_refresh.py",
        "messages/sqlite_repository.py",
    }
)

# Existing lifecycle/migration SQL is named separately instead of silently
# becoming a general-purpose owner.  These are a ratchet frontier for a later
# cleanup slice; new files may not join this set accidentally.
MESSAGE_SQL_LEGACY_EXCEPTION_PATHS = frozenset(
    {
        "daemon.py",
        "delta_sync.py",
        "event_handlers.py",
        "sync_db.py",
    }
)

CONTENT_WRAPPER_PATH = "tools/structured.py"
CONTENT_PROJECTOR_PATH = "message_content.py"
RAW_PROJECTOR_PATH = "telegram_message_projection.py"
TEXT_PROJECTOR_PATH = "text_projection.py"
TEXT_PROJECTOR_IMPORTERS = frozenset({"daemon_message.py", "message_content.py", RAW_PROJECTOR_PATH})
RAW_PROJECTOR_IMPORTERS = frozenset({"daemon_message.py", "telegram_history.py"})
MESSAGE_BODY_SERIALIZER_PATHS = frozenset(
    {"tools/reading.py", "tools/unread.py", "tools/folders.py"}
)

_EXECUTE_METHODS = frozenset({"execute", "executemany", "executescript"})
_SQL_NAME = re.compile(r"(?:^|_)(?:SQL|DDL|QUERY)(?:$|_)", re.IGNORECASE)
_SQL_START = re.compile(r"^(?:SELECT|WITH|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b", re.IGNORECASE)
_SQL_TOKEN = re.compile(
    r"--[^\n]*|/\*.*?\*/|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|[A-Za-z_][A-Za-z0-9_$]*|[.]",
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


def _has_messages_from_or_join(sql: str) -> bool:
    tokens = _sql_tokens(sql)
    for index, token in enumerate(tokens[:-1]):
        if token.casefold() not in {"from", "join"}:
            continue
        next_index = index + 1
        table = _identifier(tokens[next_index]).casefold()
        if table == "messages":
            return True
        # Permit a qualified SQLite table name such as main.messages.
        if (
            next_index + 2 < len(tokens)
            and tokens[next_index + 1] == "."
            and _identifier(tokens[next_index + 2]).casefold() == "messages"
        ):
            return True
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


def _is_manual_content_constructor(node: ast.Call) -> bool:
    return isinstance(node.func, ast.Name) and node.func.id in {"MessageContent", "TelegramContent"}


def _content_violations(path: str, tree: ast.AST) -> list[Finding]:
    findings: list[Finding] = []
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
        elif isinstance(node, ast.Call) and _is_manual_content_constructor(node) and path != CONTENT_PROJECTOR_PATH:
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
    return findings


def _raw_message_content_violations(path: str, tree: ast.AST) -> list[Finding]:
    """Reject delivery code wrapping raw message fields directly."""
    if path not in MESSAGE_BODY_SERIALIZER_PATHS:
        return []
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "telegram_content":
            continue
        if not node.args:
            continue
        value = node.args[0]
        if isinstance(value, ast.Attribute) and value.attr in {"text", "media_description"}:
            findings.append(Finding(path, node.lineno, "raw message fields must use serialize_message_content"))
    return findings


def violations_for(path: Path, source: str) -> list[Finding]:
    relative = _relative(path)
    tree = ast.parse(source, filename=str(path))
    constants = _static_constants(tree)
    findings = _content_violations(relative, tree)
    findings.extend(_projection_import_violations(relative, tree))
    findings.extend(_raw_message_content_violations(relative, tree))

    sql_hits = [snippet for snippet in _sql_snippets(tree, constants) if _has_messages_from_or_join(snippet.text)]
    if sql_hits and relative not in MESSAGE_SQL_OWNER_PATHS and relative not in MESSAGE_SQL_LEGACY_EXCEPTION_PATHS:
        findings.extend(
            Finding(relative, snippet.line, "direct FROM/JOIN messages SQL is outside a reviewed owner")
            for snippet in sql_hits
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
        if any(_has_messages_from_or_join(snippet.text) for snippet in _sql_snippets(tree, constants)):
            observed_sql_paths.add(relative)
        findings.extend(violations_for(path, source))

    findings.extend(
        Finding(path, 1, "stale message SQL owner/legacy exception entry")
        for path in stale_sql_allowlist_entries(observed_sql_paths)
    )
    return findings


def stale_sql_allowlist_entries(
    observed_paths: Iterable[str],
    *,
    owner_paths: Iterable[str] = MESSAGE_SQL_OWNER_PATHS,
    legacy_paths: Iterable[str] = MESSAGE_SQL_LEGACY_EXCEPTION_PATHS,
) -> list[str]:
    """Return reviewed owner/exception paths that no longer contain message SQL."""
    allowed_paths = set(owner_paths) | set(legacy_paths)
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
