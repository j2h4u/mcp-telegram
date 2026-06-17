from __future__ import annotations

import json
import sys
from typing import TypedDict, cast


class _JsonRpcRequest(TypedDict, total=False):
    id: int | str | None
    method: str
    params: dict[str, object]


TOOLS = [
    {
        "name": "Echo",
        "description": "Echo one payload back",
        "inputSchema": {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
            },
            "required": ["value"],
        },
    },
    {
        "name": "Fail",
        "description": "Return one JSON-RPC error",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    for raw_line in sys.stdin:
        if not raw_line.strip():
            continue
        payload = cast(_JsonRpcRequest, json.loads(raw_line))
        request_id = payload.get("id")
        method = payload.get("method")

        if method == "initialize":
            params = cast(dict[str, object], payload.get("params", {}))
            protocol_version = cast(str, params["protocolVersion"])
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": protocol_version,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake-mcp", "version": "1.0"},
                    },
                }
            )
            continue

        if method == "notifications/initialized":
            continue

        if method == "tools/list":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"tools": TOOLS},
                }
            )
            continue

        if method == "tools/call":
            params = cast(dict[str, object], payload.get("params", {}))
            name = cast(str | None, params.get("name"))
            if name == "Echo":
                arguments = cast(dict[str, object], params.get("arguments", {}))
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(arguments, ensure_ascii=False, sort_keys=True),
                                }
                            ],
                            "isError": False,
                        },
                    }
                )
                continue

            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32000,
                        "message": f"tool failed: {name}",
                    },
                }
            )
            continue

        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"unknown method: {method}",
                },
            }
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
