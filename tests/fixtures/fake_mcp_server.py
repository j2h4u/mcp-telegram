from __future__ import annotations

import json
import sys


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
        payload = json.loads(raw_line)
        request_id = payload.get("id")
        method = payload.get("method")

        if method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": payload["params"]["protocolVersion"],
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
            params = payload.get("params", {})
            name = params.get("name")
            if name == "Echo":
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(params.get("arguments", {}), ensure_ascii=False, sort_keys=True),
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
