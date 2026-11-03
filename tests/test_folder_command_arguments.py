import pytest
from pydantic import ValidationError

from mcp_telegram.tools.folders import ManageFolder


def test_update_accepts_clear_exclusion_and_rejects_conflicts() -> None:
    command = ManageFolder(
        action="update", operation_key="op", folder_id=2, expected_version="v", clear_excluded_dialog_ids=[7]
    )
    assert command.clear_excluded_dialog_ids == [7]
    with pytest.raises(ValidationError):
        ManageFolder(
            action="update",
            operation_key="op",
            folder_id=2,
            expected_version="v",
            add_dialog_ids=[7],
            clear_excluded_dialog_ids=[7],
        )


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "create", "title": "x", "dialog_ids": [2], "expected_catalog_version": "v", "folder_id": 2},
        {"action": "delete", "folder_id": 2, "expected_version": "v", "clear_excluded_dialog_ids": []},
        {"action": "update", "folder_id": 2, "expected_version": "v", "add_dialog_ids": []},
        {"action": "create", "title": "x", "dialog_ids": [2], "expected_catalog_version": "v", "account_id": 1},
    ],
)
def test_wrong_action_arguments_are_rejected(arguments: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ManageFolder.model_validate({"operation_key": "op", **arguments})


@pytest.mark.parametrize(
    "changes",
    [
        {"operation_key": " "},
        {"folder_id": None},
        {"folder_id": 1},
        {"title": " "},
        {"add_dialog_ids": [0]},
        {"title": None},
    ],
)
def test_update_rejects_invalid_values(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ManageFolder.model_validate(
            {
                "action": "update",
                "operation_key": "op",
                "folder_id": 2,
                "expected_version": "v",
                "title": "new",
                **changes,
            }
        )


def test_create_rejects_empty_folder() -> None:
    with pytest.raises(ValidationError):
        ManageFolder(action="create", operation_key="op", title="new", dialog_ids=[], expected_catalog_version="v")
