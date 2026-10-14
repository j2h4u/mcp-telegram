"""Value conversion shared by SQLite readers."""


def row_first_int(row: tuple[object | None, ...] | None) -> int:
    """Read an integer or decimal text from the first column, defaulting to zero."""
    if row is None:
        return 0
    value = row[0]
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return 0
