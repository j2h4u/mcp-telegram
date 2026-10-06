"""Filter a completed v1 export in place for sharing with ordinary chat members.

Run: uv run python -m devtools.public_chat_export EXPORT.json
Works offline; cannot detect messages deleted after the original export.
"""

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import cast

from mcp_telegram.chat_export_checkpoint import base_records, fingerprint

# Account-relative state, administration details and unnecessary media handles.
PRIVATE_FIELDS = frozenset(
    {
        "out",
        "mentioned",
        "media_unread",
        "unread",
        "my",
        "self",
        "chosen",
        "chosen_order",
        "correct",
        "solution",
        "solution_entities",
        "read_max_id",
        "read_inbox_max_id",
        "read_outbox_max_id",
        "read_date",
        "unread_count",
        "unread_mentions_count",
        "unread_reactions_count",
        "notify_settings",
        "recent_repliers",
        "saved_peer_id",
        "saved_from_peer",
        "saved_from_msg_id",
        "saved_from_id",
        "saved_from_name",
        "saved_date",
        "saved_out",
        "quick_reply_shortcut_id",
        "report_delivery_until_date",
        "admin_rights",
        "banned_rights",
        "inviter_id",
        "promoted_by",
        "kicked_by",
        "approved_by",
        "phone",
        "phone_number",
        "access_hash",
        "file_reference",
        "raw",
    }
)


def public_facts(value: object) -> object:
    """Keep ordinary chat facts, recursively removing viewer-specific state."""
    if isinstance(value, dict):
        return {
            key: public_facts(item)
            for key, item in cast(dict[str, object], value).items()
            if key not in PRIVATE_FIELDS and not ((key == "role" or key.endswith("_role")) and item == "former_member")
        }
    if isinstance(value, list):
        return [public_facts(item) for item in cast(list[object], value)]
    return value


def sanitize_export(path: Path) -> dict[str, int]:
    """Stream records and replace only after parsing and checking the full input."""
    before = fingerprint(path)
    counts = {"messages": 0, "admin_events": 0, "reactors": 0}
    declared = None
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write('{"format_version":1')
            for kind, record in base_records(path):
                if kind in {"group", "metadata"}:
                    stream.write(f',"{kind}":')
                    json.dump(public_facts(record), stream, ensure_ascii=False, separators=(",", ":"))
                    if kind == "metadata":
                        stream.write(',"admin_events":[],"messages":[')
                elif kind == "admin_events.item":
                    counts["admin_events"] += 1
                elif kind == "messages.item":
                    if counts["messages"]:
                        stream.write(",")
                    json.dump(public_facts(record), stream, ensure_ascii=False, separators=(",", ":"))
                    counts["messages"] += 1
                    counts["reactors"] += len(cast(list[object], record["reactors"]))
                elif kind == "export":
                    declared = record
            if declared != counts:
                raise ValueError("Export counts do not match its records")
            counts["admin_events"] = 0
            stream.write('],"export":')
            json.dump(counts, stream, separators=(",", ":"))
            stream.write("}\n")
            stream.flush()
            os.fsync(stream.fileno())
        if fingerprint(path) != before:
            raise ValueError("Input changed during filtering")
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return counts
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path)
    print(json.dumps(sanitize_export(parser.parse_args().export)))
