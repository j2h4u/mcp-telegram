from typing import cast

import pytest
from jsonschema import validate
from mcp.types import CallToolResult

from mcp_telegram import server, tools
from mcp_telegram.tools._base import (
    ToolResult,
    normalize_output_schema,
    omit_none_mapping_values,
    structured_result,
)


def test_omit_none_mapping_values_is_recursive_but_preserves_sequence_none() -> None:
    value = {
        "drop": None,
        "keep": False,
        "zero": 0,
        "empty": "",
        "nested": {"drop": None, "value": 1},
        "items": [None, {"drop": None, "value": 2}],
        "tuple_items": (None, {"drop": None, "value": 3}),
    }

    assert omit_none_mapping_values(value) == {
        "keep": False,
        "zero": 0,
        "empty": "",
        "nested": {"value": 1},
        "items": [None, {"value": 2}],
        "tuple_items": (None, {"value": 3}),
    }


def test_output_schema_normalizer_makes_nullable_properties_optional() -> None:
    schema = normalize_output_schema(
        {
            "type": "object",
            "properties": {
                "nullable": {"type": ["string", "null"]},
                "pure_null": {"type": "null"},
                "any_nullable": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "one_nullable": {"oneOf": [{"type": "integer"}, {"type": "null"}]},
                "items": {"type": "array", "items": {"type": ["string", "null"]}},
                "tuple_items": {"prefixItems": [{"type": "null"}, {"type": "string"}]},
            },
            "required": ["nullable", "pure_null", "any_nullable", "one_nullable", "items", "tuple_items"],
        }
    )

    assert schema is not None
    properties = cast(dict[str, object], schema["properties"])
    assert "pure_null" not in properties
    assert cast(dict[str, object], properties["nullable"]) == {"type": "string"}
    assert cast(dict[str, object], properties["any_nullable"]) == {"type": "string"}
    assert cast(dict[str, object], properties["one_nullable"]) == {"type": "integer"}
    assert cast(dict[str, object], properties["items"])["items"] == {"type": ["string", "null"]}
    assert cast(dict[str, object], properties["tuple_items"])["prefixItems"] == [
        {"type": "null"},
        {"type": "string"},
    ]
    assert schema["required"] == ["items", "tuple_items"]


def test_mixed_null_enum_is_optional_but_keeps_non_null_values() -> None:
    schema = normalize_output_schema(
        {
            "type": "object",
            "properties": {"value": {"type": "string", "enum": [None, "x"]}},
            "required": ["value"],
        }
    )

    assert schema is not None
    properties = cast(dict[str, object], schema["properties"])
    assert properties["value"] == {"type": "string", "enum": ["x"]}
    assert schema["required"] == []
    assert omit_none_mapping_values({"value": None}) == {}
    validate(instance={}, schema=schema)
    validate(instance={"value": "x"}, schema=schema)


def test_registered_output_schemas_do_not_advertise_null() -> None:
    for name, entry in tools.TOOL_REGISTRY.items():
        schema_name = name
        schema = entry.output_schema
        assert schema is not None, schema_name

        def check(node: object, *, schema_name: str = schema_name) -> None:
            if isinstance(node, list):
                for item in node:
                    check(item)
                return
            if not isinstance(node, dict):
                return
            assert not _schema_has_null(node), schema_name
            properties = node.get("properties")
            required = node.get("required")
            if isinstance(properties, dict) and isinstance(required, list):
                assert set(required) <= set(properties), schema_name
            for child in node.values():
                check(child)

        check(schema)


def _schema_has_null(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    schema_type = value.get("type")
    if isinstance(schema_type, list) and "null" in schema_type:
        return True
    for key in ("anyOf", "oneOf"):
        variants = value.get(key)
        if isinstance(variants, list) and any(
            isinstance(item, dict) and item.get("type") == "null" for item in variants
        ):
            return True
    if value.get("const", object()) is None:
        return True
    enum = value.get("enum")
    return schema_type == "null" or (isinstance(enum, list) and None in enum)


@pytest.mark.asyncio
async def test_call_tool_omits_none_from_direct_tool_result(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_runner(_args: tools.ToolArgs) -> ToolResult:
        return ToolResult(
            structured_content={
                "drop": None,
                "nested": {"drop": None, "keep": 1},
                "items": [None, {"drop": None, "keep": 2}],
            }
        )

    monkeypatch.setattr(server.tools, "tool_runner", fake_runner)
    result = await server.call_tool("list_folders", {})
    assert isinstance(result, CallToolResult)
    assert cast(dict[str, object], result.structured_content) == {
        "nested": {"keep": 1},
        "items": [None, {"keep": 2}],
    }


@pytest.mark.asyncio
async def test_call_tool_omits_none_from_direct_structured_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_runner(_args: tools.ToolArgs) -> ToolResult:
        return ToolResult(
            content=(),
            is_error=True,
            structured_content={"error": {"detail": None, "code": "bad_request"}},
        )

    monkeypatch.setattr(server.tools, "tool_runner", fake_runner)
    result = await server.call_tool("list_folders", {})
    assert result.is_error is True
    assert cast(dict[str, object], result.structured_content) == {"error": {"code": "bad_request"}}


def test_structured_result_temporal_projection_precedes_wire_compaction() -> None:
    result = structured_result({"sent_at": 0, "nested": {"drop": None}})
    assert result.structured_content == {
        "sent_at": "1970-01-01T00:00:00+00:00",
        "nested": {"drop": None},
        "time_context": {
            "timezone": "UTC",
            "canonical": "UTC",
            "query_boundaries": "UTC",
            "telegram_event_timestamps": "source_provided_only",
            "technical_timestamps": "not_telegram_events",
        },
    }
