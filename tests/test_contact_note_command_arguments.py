import pytest
from pydantic import ValidationError

from mcp_telegram.tools.contact_notes import ManageContactNote


def test_note_preserves_whitespace_and_unicode() -> None:
    command = ManageContactNote(
        dialog_id=42,
        action="update",
        expected_version="a" * 64,
        operation_key="op",
        text=" 🐈\n ",
        drop_formatting=True,
    )
    assert command.text == " 🐈\n "


@pytest.mark.parametrize(
    "changes",
    [
        {"dialog_id": 0},
        {"operation_key": " "},
        {"expected_version": " "},
        {"text": ""},
        {"text": None},
        {"action": "clear", "text": "x"},
        {"action": "clear", "text": None},
        {"action": "clear", "drop_formatting": True},
        {"action": "create", "drop_formatting": True},
    ],
)
def test_note_rejects_invalid_arguments(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ManageContactNote.model_validate(
            {
                "dialog_id": 42,
                "action": "update",
                "expected_version": "a" * 64,
                "operation_key": "op",
                "text": "x",
                **changes,
            }
        )
