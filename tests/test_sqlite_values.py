import pytest

from mcp_telegram.sqlite_values import row_first_int


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (None, 0),
        ((None,), 0),
        ((0,), 0),
        ((-7,), -7),
        ((42, "ignored"), 42),
        ((True,), 1),
        (("0042",), 42),
        (("٤٢",), 42),
        (("-7",), 0),
        ((" 42",), 0),
        (("4.2",), 0),
        (("",), 0),
        ((4.2,), 0),
        ((b"42",), 0),
    ],
)
def test_row_first_int(row: tuple[object | None, ...] | None, expected: int) -> None:
    assert row_first_int(row) == expected


def test_row_first_int_requires_a_selected_column() -> None:
    with pytest.raises(IndexError):
        row_first_int(())
