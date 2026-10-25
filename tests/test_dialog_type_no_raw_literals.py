"""Architectural guard: dialog-type vocabulary has exactly ONE source of truth.

``models.DialogType`` (+ ``from_entity`` / ``parse``) is the only place allowed to
spell dialog-type strings. Every other module must compare against / produce
``DialogType`` members, never bare string literals like ``"Channel"`` or
``== "User"``. This test FAILS the build if a raw dialog-type literal reappears
anywhere in ``src/mcp_telegram`` outside the allowed module — making it impossible
to silently reintroduce the divergent vocabularies / case-mismatch bugs that this
unification removed (phase-53).

Reviewed protocol-constructor scopes and the historical admission spellings are
classified separately below; neither is dialog-type production.

If this test fails: replace the flagged literal with a ``DialogType`` member, or
route the value through ``DialogType.from_entity`` / ``DialogType.parse``.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from mcp_telegram.entity_profile.field_manifest import PROFILE_FIELD_MANIFEST

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "mcp_telegram"

# The canonical enum lives here; it is the ONE place allowed to spell the literals.
ALLOWED_FILES = {"models.py"}

# Capitalized vocabulary — these quoted strings are almost always a dialog/entity
# type and must never appear as literals outside models.py.
CAPITALIZED_TYPE_LITERALS = {
    "User",
    "Bot",
    "Channel",
    "Supergroup",
    "Megagroup",
    "Group",
    "Forum",
    "Chat",
}
# Lowercase vocabulary — flagged only when used as a comparison operand (== / != / in),
# the high-signal "branching on a raw dialog-type string" pattern. We list only the
# DISTINCTIVE DialogType values that cannot collide with other vocabularies:
#   - "supergroup"/"megagroup"/"forum" are unambiguously DialogType.
#   - "channel" is intentionally EXCLUDED: it is overloaded — dialog_sync's offset-peer
#     cursor uses Telethon's InputPeer peer-class vocabulary ("user"/"chat"/"channel",
#     where "channel" covers both broadcasts AND supergroups), which is a different axis
#     from DialogType. The capitalized guard still catches "Channel", and genuine
#     dialog-type "channel" production goes through DialogType.from_entity/parse.
#   - "user"/"bot"/"group" collide with common words and other vocabularies, so they
#     are covered by the capitalized guard + the from_entity/parse funnel, not here.
LOWERCASE_TYPE_LITERALS = {
    "supergroup",
    "megagroup",
    "forum",
}


# These finite declarations/adapters spell TL constructors, not DialogType.
# Classify named scopes, never entire modules; constructor names must also exist
# in the exhaustive protocol manifest. New scopes require explicit review.
TL_CONSTRUCTOR_SCOPES = {
    "entity_profile/field_manifest.py": {"PARTIAL_PROFILE_CONSTRUCTORS", "PROFILE_FIELD_MANIFEST"},
    "entity_profile/full_user_normalization.py": {
        "_retain_authorized_legacy_fields",
        "_retain_membership",
        "_authorized_identity",
    },
    "entity_profile/semantic_normalization.py": {"normalize_semantic_profile", "_field_authority"},
    "entity_profile/output_schema.py": {"_reachable_constructors", "_root_rules", "COLLECTION_CONSTRUCTORS"},
    "telegram_gateway.py": {"_item_identity"},
}


def _literal_scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str | None:
    assignment = None
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if isinstance(target, ast.Name):
                assignment = target.id
    return assignment


def _capitalized_offenders(source: str, relative_path: str) -> list[str]:
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if node.value not in CAPITALIZED_TYPE_LITERALS:
            continue
        scope = _literal_scope(node, parents)
        if scope in TL_CONSTRUCTOR_SCOPES.get(relative_path, set()) and node.value in PROFILE_FIELD_MANIFEST:
            continue
        # The admission SQL intentionally accepts historical persisted spellings.
        if (
            relative_path == "entity_profile/repository.py"
            and scope == "_PROFILE_ADMISSION_TYPES"
            and node.value in {"User", "Bot"}
        ):
            continue
        offenders.append(f'{relative_path}:{node.lineno}  "{node.value}"')
    return offenders


def _iter_py_files():
    for path in sorted(SRC.rglob("*.py")):
        if path.name in ALLOWED_FILES:
            continue
        yield path


def test_no_capitalized_dialog_type_literals_outside_models():
    """No capitalized dialog-type string literals anywhere outside models.py."""
    offenders: list[str] = []
    for path in _iter_py_files():
        offenders.extend(_capitalized_offenders(path.read_text(encoding="utf-8"), str(path.relative_to(SRC))))
    assert not offenders, (
        "Raw capitalized dialog-type literals found — use models.DialogType instead:\n  " + "\n  ".join(offenders)
    )


def test_no_lowercase_dialog_type_comparison_literals_outside_models():
    """No `== / != / in` comparisons against distinctive lowercase type literals."""
    offenders: list[str] = []
    for path in _iter_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            operands = [node.left, *node.comparators]
            for op in operands:
                # `x in ("channel", "forum")` — unwrap tuple/list elements too.
                consts = []
                if isinstance(op, ast.Constant):
                    consts = [op]
                elif isinstance(op, (ast.Tuple, ast.List, ast.Set)):
                    consts = [e for e in op.elts if isinstance(e, ast.Constant)]
                offenders.extend(
                    f'{path.relative_to(SRC)}:{c.lineno}  compare vs "{c.value}"'
                    for c in consts
                    if isinstance(c.value, str) and c.value in LOWERCASE_TYPE_LITERALS
                )
    assert not offenders, (
        "Raw lowercase dialog-type comparison literals found — use models.DialogType:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("relative_path", [*TL_CONSTRUCTOR_SCOPES, "entity_profile/repository.py"])
def test_constructor_exceptions_do_not_allow_dialog_literals_elsewhere_in_same_file(relative_path: str) -> None:
    assert _capitalized_offenders('def dialog_kind():\n    return "User"\n', relative_path)


def test_constructor_scope_requires_a_known_protocol_constructor() -> None:
    assert _capitalized_offenders('PARTIAL_PROFILE_CONSTRUCTORS = {"Bot"}', "entity_profile/field_manifest.py")
