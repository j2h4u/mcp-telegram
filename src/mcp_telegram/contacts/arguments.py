"""Strict command arguments for native contacts."""

from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from pydantic.config import JsonSchemaExtraCallable

from .contracts import CONTACT_PAGE_SIZE_LIMIT


class ContactListArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: StrictInt = Field(default=50, ge=1, le=CONTACT_PAGE_SIZE_LIMIT)
    after_entity_id: StrictInt | None = Field(default=None, gt=0)


def _contact_command_schema(schema: dict[str, object]) -> None:
    schema["allOf"] = [
        {
            "if": {"properties": {"action": {"const": "create"}}, "required": ["action"]},
            "then": {"required": ["first_name"]},
        },
        {
            "if": {"properties": {"action": {"const": "update"}}, "required": ["action"]},
            "then": {"anyOf": [{"required": ["first_name"]}, {"required": ["last_name"]}]},
        },
        {
            "if": {"properties": {"action": {"const": "delete"}}, "required": ["action"]},
            "then": {"not": {"anyOf": [{"required": ["first_name"]}, {"required": ["last_name"]}]}},
        },
    ]


class ContactCommandArguments(BaseModel):
    """Create, rename or delete one contact using its version from get_entity_info.

    Telegram does not provide atomic compare-and-swap. Reuse the operation key to inspect an unknown outcome.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra=cast(JsonSchemaExtraCallable, _contact_command_schema))
    entity_id: StrictInt = Field(gt=0)
    action: Literal["create", "update", "delete"]
    expected_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_key: str = Field(min_length=1, max_length=128)
    first_name: str | None = Field(default=None, min_length=1)
    last_name: str | None = None

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        names = self.model_fields_set & {"first_name", "last_name"}
        if not self.operation_key.strip():
            raise ValueError("operation_key must not be blank")
        if any(getattr(self, name) is None for name in names):
            raise ValueError("supplied names cannot be null")
        if self.first_name is not None and not self.first_name.strip():
            raise ValueError("first_name must not be blank")
        self._validate_names_for_action(names)
        return self

    def _validate_names_for_action(self, names: set[str]) -> None:
        if self.action == "create" and self.first_name is None:
            raise ValueError("create requires first_name")
        if self.action == "update" and not names:
            raise ValueError("update requires a name")
        if self.action == "delete" and names:
            raise ValueError("delete does not accept names")
