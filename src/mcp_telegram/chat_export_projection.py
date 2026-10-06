"""Pure projections of Telegram facts into export records without duplicate facts."""

from collections.abc import Mapping
from typing import cast

type Facts = dict[str, object]


def omit_empty_fields(value: object) -> object:
    """Drop empty object properties recursively while preserving every array slot."""
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            normalized = omit_empty_fields(item)
            if normalized is not None and normalized != {} and normalized != []:
                result[key] = normalized
        return result
    if isinstance(value, list):
        return [omit_empty_fields(item) for item in value]
    return value


def project_exporter(software_version: str) -> Facts:
    return {
        "name": "mcp-telegram",
        "version": software_version,
        "repository_url": "https://github.com/j2h4u/mcp-telegram",
    }


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


def _peer_matches(value: object, identity: Mapping[str, object]) -> bool:
    peer = _object(value)
    field = {"user": "user_id", "chat": "chat_id", "channel": "channel_id"}.get(str(identity.get("kind")))
    if field is None or set(peer) != {"_", field}:
        return False
    identifier = peer[field]
    if type(identifier) is not int or identifier <= 0:
        return False
    canonical = identifier if field == "user_id" else -identifier
    if field == "channel_id":
        canonical -= 10**12
    return peer["_"] == {"user_id": "PeerUser", "chat_id": "PeerChat", "channel_id": "PeerChannel"}[field] and _id(
        canonical
    ) == _id(identity.get("id"))


def _dialog_peer_matches(value: object, dialog_id: object) -> bool:
    return any(_peer_matches(value, {"id": dialog_id, "kind": kind}) for kind in ("user", "chat", "channel"))


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
    result.pop("reply_key", None)
    result["reactions"] = message.get("reactions")
    result = compact_export_record("messages.item", deduplicate_export_message(result))
    result.pop("reactions")
    return result


def deduplicate_export_message(record: Mapping[str, object]) -> Facts:
    """Remove only facts already represented by a projected v1 record, without mutating it."""
    result = dict(record)
    metadata = _deduplicate_message_metadata(record)
    if isinstance(record.get("metadata"), Mapping) and isinstance(result.get("metadata"), Mapping):
        result["metadata"] = metadata
    if isinstance(record.get("reactors"), list):
        result["reactors"] = [
            _deduplicate_reactor(reactor) if isinstance(reactor, Mapping) else reactor
            for reactor in cast(list[object], record["reactors"])
        ]
    return result


def _deduplicate_message_metadata(record: Mapping[str, object]) -> Facts:
    metadata = _object(record.get("metadata"))
    if _peer_matches(metadata.get("from_id"), {"id": record.get("author_id"), "kind": record.get("author_kind")}):
        metadata.pop("from_id")
    if _dialog_peer_matches(metadata.get("peer_id"), record.get("dialog_id")):
        metadata.pop("peer_id")
    reactions = _object(record.get("reactions"))
    if "reactions" in metadata and "aggregate" in reactions and metadata["reactions"] == reactions["aggregate"]:
        metadata.pop("reactions")
    _deduplicate_reply(metadata, record)
    return metadata


def _deduplicate_reply(metadata: Facts, record: Mapping[str, object]) -> None:
    if isinstance(metadata.get("reply_to"), Mapping):
        reply = _object(metadata["reply_to"])
        if (
            "reply_to_msg_id" in reply
            and "reply_to_message_id" in record
            and _id(reply["reply_to_msg_id"]) == record["reply_to_message_id"]
        ):
            reply.pop("reply_to_msg_id")
        if _dialog_peer_matches(reply.get("reply_to_peer_id"), record.get("reply_to_dialog_id")):
            reply.pop("reply_to_peer_id")
        metadata["reply_to"] = reply


def project_reactor(reactor: Mapping[str, object]) -> Facts:
    result = {key: value for key, value in clean_facts(reactor).items() if key != "peer"}
    result.update(project_identity(reactor.get("peer"), "actor_"))
    return compact_export_record("reactors.item", _deduplicate_reactor(result))


def compact_export_record(kind: str, record: Mapping[str, object]) -> Facts:
    """Remove only exact generated identity aliases from a legacy export record."""
    result = dict(record)
    if kind == "messages.item":
        _compact_message(result)
    elif kind == "admin_events.item":
        _compact_identity_label(result, "actor_")
        _compact_related_users(result)
    elif kind == "reactors.item":
        _compact_identity_label(result, "actor_")
    return result


def _compact_message(result: Facts) -> None:
    _compact_message_identity(result)
    topic = _object(result.get("topic"))
    for key in ("id", "topic_id"):
        if key in topic and "topic_id" in result and _id(topic[key]) == result["topic_id"]:
            topic.pop(key)
    if isinstance(result.get("topic"), Mapping):
        result["topic"] = topic
    _compact_identity_label(result, "author_")
    _compact_related_users(result)
    if isinstance(result.get("reactors"), list):
        result["reactors"] = [
            compact_export_record("reactors.item", _object(x)) for x in cast(list[object], result["reactors"])
        ]


def _compact_message_identity(result: Facts) -> None:
    for key, expected, error in (
        ("message_key", _key(result.get("dialog_id"), result.get("message_id")), "message identity"),
        (
            "reply_key",
            _key(result.get("reply_to_dialog_id"), result.get("reply_to_message_id")),
            "reply identity",
        ),
    ):
        if key in result:
            if result[key] != expected:
                raise ValueError(f"Legacy {key} does not match {error}")
            result.pop(key)
    metadata = _object(result.get("metadata"))
    expected_kind = {"message": "Message", "service": "MessageService"}.get(str(result.get("kind")))
    if metadata.get("_") == expected_kind and expected_kind is not None:
        metadata.pop("_")
    if "author_rank" in result and "from_rank" in metadata and metadata["from_rank"] == result["author_rank"]:
        metadata.pop("from_rank")
    if isinstance(result.get("metadata"), Mapping):
        result["metadata"] = metadata


def _compact_related_users(result: Facts) -> None:
    if isinstance(result.get("related_users"), list):
        result["related_users"] = [
            _compact_identity_label(_object(x), "") for x in cast(list[object], result["related_users"])
        ]


def _compact_identity_label(record: Facts, prefix: str) -> Facts:
    rank_key, metadata_key = prefix + "rank", prefix + "metadata"
    metadata = _object(record.get(metadata_key))
    if rank_key in record and "label" in metadata and metadata["label"] == record[rank_key]:
        metadata.pop("label")
    if isinstance(record.get(metadata_key), Mapping):
        record[metadata_key] = metadata
    return record


def compact_export_metadata(group: Mapping[str, object], metadata: Mapping[str, object]) -> Facts:
    result = dict(metadata)
    peers = result.get("peers")
    if isinstance(peers, list) and peers and isinstance(peers[0], Mapping) and dict(peers[0]) == dict(group):
        result["peers"] = [{"dialog_id": peers[0].get("dialog_id")}, *peers[1:]]
    return result


def _deduplicate_reactor(reactor: Mapping[str, object]) -> Facts:
    result = dict(reactor)
    if isinstance(result.get("raw"), Mapping):
        raw = _object(result["raw"])
        if _peer_matches(raw.get("peer_id"), {"id": reactor.get("actor_id"), "kind": reactor.get("actor_kind")}):
            raw.pop("peer_id")
        for key in ("reaction", "date"):
            if key in raw and key in reactor and raw[key] == reactor[key]:
                raw.pop(key)
        result["raw"] = raw
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
    return compact_export_record("admin_events.item", result)
