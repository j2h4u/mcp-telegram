"""Pure projections of Telegram facts into Pandas-friendly export records."""

from collections.abc import Mapping
from typing import cast

type Facts = dict[str, object]


def clean_facts(data: Mapping[str, object]) -> Facts:
    return {
        key: value
        for key, value in data.items()
        if key not in {"observed_at", "identity_cached_at", "identity_source", "status", "source", "reason"}
    }


def _object(value: object) -> Facts:
    return dict(cast(Mapping[str, object], value)) if isinstance(value, Mapping) else {}


def _id(value: object) -> str | None:
    return str(value) if value is not None else None


def _key(dialog_id: object, message_id: object) -> str | None:
    return f"{dialog_id}:{message_id}" if dialog_id is not None and message_id is not None else None


def project_identity(value: object, prefix: str = "") -> Facts:
    identity = clean_facts(_object(value))
    fields = {
        "id": _id(identity.get("id")),
        "kind": identity.get("kind"),
        "name": identity.get("display_name", identity.get("name")),
        "username": identity.get("username"),
        "is_admin": identity.get("is_admin"),
        "role": identity.get("role"),
        "rank": identity.get("rank"),
    }
    extra = {
        key: item
        for key, item in identity.items()
        if key not in {"id", "kind", "display_name", "name", "username", "is_admin", "role", "rank", "telegram_id"}
    }
    fields["metadata"] = extra
    return {f"{prefix}{key}": item for key, item in fields.items()}


def _related_users(value: object) -> list[Facts]:
    return [project_identity(user) for user in cast(list[object], value)] if isinstance(value, list) else []


def project_group(group: Mapping[str, object]) -> Facts:
    result = clean_facts(group)
    result["dialog_id"] = _id(result.get("dialog_id"))
    return result


def _topic(value: object) -> Facts | None:
    if value is None:
        return None
    result = clean_facts(_object(value))
    for key in ("id", "topic_id"):
        if key in result:
            result[key] = _id(result[key])
    return result


def _reply(message: Mapping[str, object]) -> Facts:
    reply = _object(message.get("reply_to"))
    dialog_id = _object(reply.get("peer")).get("id", message.get("dialog_id")) if reply else None
    message_id = reply.get("message_id")
    return {
        "reply_to_dialog_id": _id(dialog_id),
        "reply_to_message_id": _id(message_id),
        "reply_key": _key(dialog_id, message_id),
    }


def project_message(message: Mapping[str, object]) -> Facts:
    raw = _object(message.get("raw"))
    result = {
        key: value
        for key, value in clean_facts(message).items()
        if key not in {"id", "raw", "author", "reply_to", "related_users", "topic", "reactions"}
    }
    result.update(
        {
            "dialog_id": _id(message.get("dialog_id")),
            "message_id": _id(message.get("id")),
            "message_key": _key(message.get("dialog_id"), message.get("id")),
            "date": message.get("date", raw.get("date")),
            "text": raw.get("message", ""),
            "edited_at": raw.get("edit_date"),
            "topic_id": _id(message.get("topic_id")),
            "grouped_id": _id(raw.get("grouped_id")),
            "entities": raw.get("entities"),
            "service_action": raw.get("action"),
            "topic": _topic(message.get("topic")),
            "related_users": _related_users(message.get("related_users")),
            "metadata": {
                key: value
                for key, value in raw.items()
                if key not in {"message", "id", "date", "edit_date", "grouped_id", "entities", "action"}
            },
        }
    )
    result.update(project_identity(message.get("author"), "author_"))
    result.update(_reply(message))
    return result


def project_reactor(reactor: Mapping[str, object]) -> Facts:
    result = {key: value for key, value in clean_facts(reactor).items() if key != "peer"}
    result.update(project_identity(reactor.get("peer"), "actor_"))
    return result


def project_admin_event(event: Mapping[str, object], dialog_id: int) -> Facts:
    result = {key: value for key, value in clean_facts(event).items() if key not in {"id", "actor", "related_users"}}
    result.update(
        {
            "event_id": _id(event.get("id")),
            "dialog_id": str(dialog_id),
            "related_users": _related_users(event.get("related_users")),
        }
    )
    result.update(project_identity(event.get("actor"), "actor_"))
    return result
