"""Architectural guard: dialog-type vocabulary has exactly ONE source of truth.

``models.DialogType`` (+ ``from_entity`` / ``parse``) is the only place allowed to
spell dialog-type strings. Every other module must compare against / produce
``DialogType`` members, never bare string literals like ``"Channel"`` or
``== "User"``. This test FAILS the build if a raw dialog-type literal reappears
anywhere in ``src/mcp_telegram`` outside the allowed module — making it impossible
to silently reintroduce the divergent vocabularies / case-mismatch bugs that this
unification removed (phase-53).

If this test fails: replace the flagged literal with a ``DialogType`` member, or
route the value through ``DialogType.from_entity`` / ``DialogType.parse``.
"""
from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "mcp_telegram"

# The canonical enum lives here; it is the ONE place allowed to spell the literals.
ALLOWED_FILES = {"models.py"}

# Capitalized vocabulary — these quoted strings are almost always a dialog/entity
# type and must never appear as literals outside models.py.
CAPITALIZED_TYPE_LITERALS = {
    "User", "Bot", "Channel", "Supergroup", "Megagroup", "Group", "Forum", "Chat",
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
    "supergroup", "megagroup", "forum",
}


def _iter_py_files():
    for path in sorted(SRC.rglob("*.py")):
        if path.name in ALLOWED_FILES:
            continue
        yield path


def test_no_capitalized_dialog_type_literals_outside_models():
    """No capitalized dialog-type string literals anywhere outside models.py."""
    offenders: list[str] = []
    for path in _iter_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in CAPITALIZED_TYPE_LITERALS
            ):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}  \"{node.value}\"")
    assert not offenders, (
        "Raw capitalized dialog-type literals found — use models.DialogType instead:\n  "
        + "\n  ".join(offenders)
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
                for c in consts:
                    if isinstance(c.value, str) and c.value in LOWERCASE_TYPE_LITERALS:
                        offenders.append(
                            f"{path.relative_to(SRC)}:{c.lineno}  compare vs \"{c.value}\""
                        )
    assert not offenders, (
        "Raw lowercase dialog-type comparison literals found — use models.DialogType:\n  "
        + "\n  ".join(offenders)
    )
