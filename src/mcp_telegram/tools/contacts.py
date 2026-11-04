"""Locally listed contacts and explicit native contact commands."""

from mcp.types import ToolAnnotations

from ..contacts.arguments import ContactCommandArguments, ContactListArguments
from ._base import ToolArgs, ToolResult, mcp_tool


class ListContacts(ContactListArguments, ToolArgs):
    """List retained contacts, including contacts without a dialog, without contacting Telegram.

    Coverage describes the last accepted complete snapshot and its observation time; it does not promise freshness.
    """


class ManageContact(ContactCommandArguments, ToolArgs):
    """Create, rename or delete one contact using its version from get_entity_info.

    Telegram does not provide atomic compare-and-swap. Reuse the operation key to inspect an unknown outcome.
    A rename carries the locally known phone when available. If it is unknown, the result does not confirm whether
    Telegram preserved the hidden phone value.
    """


CONTACT_STATE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "membership": {"type": ["boolean", "null"]},
        "first_name": {"type": ["string", "null"], "description": "Untrusted Telegram content."},
        "last_name": {"type": ["string", "null"], "description": "Untrusted Telegram content."},
        "phone_state": {"enum": ["available", "unavailable", "unknown"]},
        "phone": {"type": ["string", "null"]},
        "expected_version": {"type": ["string", "null"]},
        "observation_order": {"type": ["integer", "null"]},
    },
    "required": [
        "membership",
        "first_name",
        "last_name",
        "phone_state",
        "phone",
        "expected_version",
        "observation_order",
    ],
    "additionalProperties": False,
}
LIST_CONTACTS_OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "account_id": {"type": "integer"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "integer"},
                    "membership": {"type": "boolean"},
                    "first_name": {"type": ["string", "null"], "description": "Untrusted Telegram content."},
                    "last_name": {"type": ["string", "null"], "description": "Untrusted Telegram content."},
                    "expected_version": {"type": ["string", "null"]},
                },
                "required": ["entity_id", "membership", "first_name", "last_name", "expected_version"],
                "additionalProperties": False,
            },
        },
        "next_after_entity_id": {"type": ["integer", "null"]},
        "coverage": {
            "type": "object",
            "properties": {"state": {"enum": ["unknown", "complete"]}, "observed_at": {"type": ["integer", "null"]}},
            "required": ["state", "observed_at"],
            "additionalProperties": False,
        },
    },
    "required": ["ok", "account_id", "items", "next_after_entity_id", "coverage"],
    "additionalProperties": False,
}
MANAGE_CONTACT_OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "account_id": {"type": "integer"},
        "entity_id": {"type": "integer"},
        "operation_key": {"type": "string"},
        "outcome": {"enum": ["unchanged", "not_sent", "rejected", "acknowledged", "unknown"]},
        "verification": {"type": "string"},
        "verification_order": {"type": "integer"},
        "current_applicability": {"enum": ["unknown"]},
        "telegram_error": {
            "type": "object",
            "properties": {"type": {"type": "string"}, "code": {"type": ["integer", "null"]}},
            "required": ["type", "code"],
            "additionalProperties": False,
        },
        "required_action": {"type": "string"},
    },
    "required": ["ok", "account_id", "entity_id", "operation_key", "outcome", "verification"],
    "additionalProperties": False,
}


@mcp_tool(
    name="list_contacts",
    title="List Contacts",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
    output_schema=LIST_CONTACTS_OUTPUT_SCHEMA,
)
async def list_contacts(args: ListContacts) -> ToolResult:
    from ._base import (
        DaemonNotRunningError,
        _check_daemon_response,
        _daemon_not_running_text,
        daemon_connection,
        error_result,
        structured_result,
    )

    try:
        async with daemon_connection() as conn:
            response = await conn.request(
                {"method": "list_contacts", **args.model_dump(exclude_unset=True, exclude={"timezone"})}
            )
    except DaemonNotRunningError as exc:
        return error_result(_daemon_not_running_text(exc))
    if error := _check_daemon_response(response):
        return error
    response.pop("request_id", None)
    return structured_result(response)


@mcp_tool(
    name="manage_contact",
    title="Manage Contact",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
    ),
    output_schema=MANAGE_CONTACT_OUTPUT_SCHEMA,
)
async def manage_contact(args: ManageContact) -> ToolResult:
    from ._base import (
        DaemonNotRunningError,
        _check_daemon_response,
        _daemon_not_running_text,
        daemon_connection,
        error_result,
        structured_result,
    )

    try:
        async with daemon_connection() as conn:
            response = await conn.request(
                {"method": "manage_contact", **args.model_dump(exclude_unset=True, exclude={"timezone"})}
            )
    except DaemonNotRunningError as exc:
        if exc.kind in {"not_running", "connect_timeout"}:
            return error_result(_daemon_not_running_text(exc))
        return error_result(
            "Contact completion is unconfirmed.\nAction: retry the exact operation with the same operation key.",
            error_code="contact_completion_unconfirmed",
        )
    if error := _check_daemon_response(response):
        return error
    response.pop("request_id", None)
    return structured_result(response)
