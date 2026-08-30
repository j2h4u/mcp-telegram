"""Contract tests for canonical dialog selector classification."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from mcp_telegram.dialog_selector import (
    DialogSelector,
    DialogSelectorError,
    optional_dialog_selector,
    required_dialog_selector,
)

SQLITE_INT64_MIN = -(2**63)
SQLITE_INT64_MAX = 2**63 - 1


@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        (required_dialog_selector, DialogSelector(exact_id=42, query=None)),
        (optional_dialog_selector, DialogSelector(exact_id=42, query=None)),
    ],
)
def test_integer_exact_id_is_canonical_for_required_and_optional_scopes(
    factory: Callable[..., DialogSelector | None],
    expected: DialogSelector,
) -> None:
    assert callable(factory)
    assert factory(exact_id=42, dialog=None) == expected


def test_optional_absence_means_global_scope() -> None:
    assert optional_dialog_selector(exact_id=None, dialog=None) is None


def test_required_absence_has_stable_missing_error() -> None:
    with pytest.raises(DialogSelectorError) as raised:
        required_dialog_selector(exact_id=None, dialog=None)

    assert raised.value.code == "missing_dialog"


@pytest.mark.parametrize(
    ("exact_id", "dialog"),
    [
        (1, "chat"),
        (True, None),
        (False, None),
        (0, None),
        (None, "0"),
        (None, "+0"),
        (None, "-0"),
        (None, ""),
        (None, "   \t\n"),
    ],
)
@pytest.mark.parametrize("factory", [required_dialog_selector, optional_dialog_selector])
def test_malformed_selector_has_stable_invalid_error(
    factory: Callable[..., DialogSelector | None],
    exact_id: object | None,
    dialog: object | None,
) -> None:
    with pytest.raises(DialogSelectorError) as raised:
        factory(exact_id=exact_id, dialog=dialog)

    assert raised.value.code == "invalid_dialog_selector"


@pytest.mark.parametrize("exact_id", [SQLITE_INT64_MIN, -1, 1, SQLITE_INT64_MAX])
def test_signed_sqlite_int64_boundaries_are_valid(exact_id: int) -> None:
    selector = required_dialog_selector(exact_id=exact_id, dialog=None)

    assert selector == DialogSelector(exact_id=exact_id, query=None)
    assert selector.label == str(exact_id)


@pytest.mark.parametrize("exact_id", [SQLITE_INT64_MIN - 1, SQLITE_INT64_MAX + 1])
def test_integer_outside_sqlite_int64_is_invalid(exact_id: int) -> None:
    with pytest.raises(DialogSelectorError) as raised:
        required_dialog_selector(exact_id=exact_id, dialog=None)

    assert raised.value.code == "invalid_dialog_selector"


@pytest.mark.parametrize(
    ("dialog", "expected_id"),
    [
        ("42", 42),
        ("  +42\t", 42),
        (" -42 ", -42),
        (str(SQLITE_INT64_MIN), SQLITE_INT64_MIN),
        (f"+{SQLITE_INT64_MAX}", SQLITE_INT64_MAX),
    ],
)
def test_signed_ascii_numeric_dialog_is_promoted(dialog: str, expected_id: int) -> None:
    selector = required_dialog_selector(exact_id=None, dialog=dialog)

    assert selector == DialogSelector(exact_id=expected_id, query=None)


@pytest.mark.parametrize("dialog", [str(SQLITE_INT64_MIN - 1), str(SQLITE_INT64_MAX + 1)])
def test_numeric_dialog_outside_sqlite_int64_is_invalid(dialog: str) -> None:
    with pytest.raises(DialogSelectorError) as raised:
        required_dialog_selector(exact_id=None, dialog=dialog)

    assert raised.value.code == "invalid_dialog_selector"


@pytest.mark.parametrize(
    ("dialog", "expected_query"),
    [
        ("  Project Room  ", "Project Room"),
        ("@12345", "@12345"),
        (" https://t.me/project_room ", "https://t.me/project_room"),
        ("t.me/project_room", "t.me/project_room"),
        ("١٢٣", "١٢٣"),
        ("１２３", "１２３"),
        ("²", "²"),
    ],
)
def test_non_ascii_or_natural_dialog_remains_query(dialog: str, expected_query: str) -> None:
    selector = required_dialog_selector(exact_id=None, dialog=dialog)

    assert selector == DialogSelector(exact_id=None, query=expected_query)
    assert selector.label == expected_query


def test_dialog_selector_is_immutable() -> None:
    selector = DialogSelector(exact_id=1, query=None)

    with pytest.raises(AttributeError):
        selector.exact_id = 2  # type: ignore[misc]
