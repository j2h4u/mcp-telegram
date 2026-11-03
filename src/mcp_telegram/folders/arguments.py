"""Public arguments for native Telegram folder operations."""

from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from pydantic.config import JsonSchemaExtraCallable

_FIRST_CUSTOM_FOLDER_ID = 2


def _command_schema(schema: dict[str, object]) -> None:
    changes = ("title", "add_dialog_ids", "remove_dialog_ids", "clear_excluded_dialog_ids")
    policies = {
        "create": (
            {"title", "dialog_ids", "expected_catalog_version"},
            {"folder_id", "expected_version", *changes[1:]},
        ),
        "update": ({"folder_id", "expected_version"}, {"dialog_ids", "expected_catalog_version"}),
        "delete": ({"folder_id", "expected_version"}, {"dialog_ids", "expected_catalog_version", *changes}),
    }
    conditions: list[dict[str, object]] = []
    for action, (required, forbidden) in policies.items():
        then: dict[str, object] = {
            "required": sorted(required),
            "not": {"anyOf": [{"required": [field]} for field in sorted(forbidden)]},
        }
        if action == "update":
            then["anyOf"] = [
                {"required": [field], "properties": {field: {"minItems": 1}}}
                if field != "title"
                else {"required": [field]}
                for field in changes
            ]
        conditions.append({"if": {"properties": {"action": {"const": action}}, "required": ["action"]}, "then": then})
    schema["allOf"] = conditions


class FolderReadArguments(BaseModel):
    """Read one retained folder definition without contacting Telegram."""

    model_config = ConfigDict(extra="forbid")

    folder_id: StrictInt


class FolderCommandArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra=cast(JsonSchemaExtraCallable, _command_schema))
    """Change one native folder using the version returned by the local mirror."""

    action: Literal["create", "update", "delete"]
    operation_key: str = Field(min_length=1, max_length=128)
    folder_id: StrictInt | None = None
    expected_version: str | None = Field(default=None, min_length=1, max_length=256)
    expected_catalog_version: str | None = Field(default=None, min_length=1, max_length=256)
    title: str | None = Field(default=None, min_length=1)
    dialog_ids: list[StrictInt] | None = Field(default=None, min_length=1)
    add_dialog_ids: list[StrictInt] | None = None
    remove_dialog_ids: list[StrictInt] | None = None
    clear_excluded_dialog_ids: list[StrictInt] | None = None

    @model_validator(mode="after")
    def validate_command(self) -> Self:
        if not self.operation_key.strip():
            raise ValueError("operation_key must not be blank")
        self._validate_action_fields()
        self._validate_values()
        self._validate_dialog_ids()
        return self

    def _validate_action_fields(self) -> None:
        supplied = self.model_fields_set
        required, allowed = {
            "create": (
                {"title", "dialog_ids", "expected_catalog_version"},
                {"title", "dialog_ids", "expected_catalog_version"},
            ),
            "update": (
                {"folder_id", "expected_version"},
                {
                    "folder_id",
                    "expected_version",
                    "title",
                    "add_dialog_ids",
                    "remove_dialog_ids",
                    "clear_excluded_dialog_ids",
                },
            ),
            "delete": ({"folder_id", "expected_version"}, {"folder_id", "expected_version"}),
        }[self.action]
        if not required <= supplied or supplied - allowed - {"action", "operation_key", "timezone"}:
            raise ValueError("arguments do not match action")
        if any(getattr(self, field) is None for field in required):
            raise ValueError("required arguments cannot be null")
        if any(getattr(self, field) is None for field in supplied - {"action", "operation_key", "timezone"}):
            raise ValueError("supplied arguments cannot be null")

    def _validate_values(self) -> None:
        if self.folder_id is not None and self.folder_id < _FIRST_CUSTOM_FOLDER_ID:
            raise ValueError("folder_id must identify a custom folder")
        if self.title is not None and not self.title.strip():
            raise ValueError("title must not be blank")
        if self.action == "create" and not self.dialog_ids:
            raise ValueError("create requires at least one dialog")
        if (
            self.action == "update"
            and self.title is None
            and not any((self.add_dialog_ids, self.remove_dialog_ids, self.clear_excluded_dialog_ids))
        ):
            raise ValueError("update requires a change")

    def _validate_dialog_ids(self) -> None:
        arrays = (self.dialog_ids, self.add_dialog_ids, self.remove_dialog_ids, self.clear_excluded_dialog_ids)
        if any(value == 0 for values in arrays if values is not None for value in values):
            raise ValueError("dialog IDs must be nonzero")
        groups = [set(values or ()) for values in arrays[1:]]
        if any(groups[left] & groups[right] for left, right in ((0, 1), (0, 2), (1, 2))):
            raise ValueError("dialog IDs must be disjoint across actions")
