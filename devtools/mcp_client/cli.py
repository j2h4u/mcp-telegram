from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from devtools.mcp_client.client import (
    DEFAULT_TIMEOUT_SECONDS,
    McpClientError,
    StdioMcpClient,
    execute_script_steps,
    load_script_steps,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Small stdio MCP client for local testing.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_tools_parser = subparsers.add_parser("list-tools", help="Initialize the server and print tools/list.")
    _add_common_arguments(list_tools_parser)

    call_tool_parser = subparsers.add_parser("call-tool", help="Initialize the server and invoke one tool.")
    call_tool_parser.add_argument("--name", required=True, help="Tool name to invoke.")
    call_tool_parser.add_argument(
        "--arguments",
        default="{}",
        help="JSON object with tool arguments. Default: {}",
    )
    _add_common_arguments(call_tool_parser)

    script_parser = subparsers.add_parser("script", help="Run several MCP actions in one session from a JSON file.")
    script_parser.add_argument("--file", required=True, help="Path to a JSON script file.")
    _add_common_arguments(script_parser)

    return parser


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Per-request timeout in seconds. Default: {DEFAULT_TIMEOUT_SECONDS}",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Print one-line JSON instead of pretty output.",
    )
    parser.add_argument(
        "server_command",
        nargs=argparse.REMAINDER,
        help="Command used to launch the stdio MCP server. Prefix with '--'.",
    )


def parse_tool_arguments(raw_arguments: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--arguments must be valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("--arguments JSON must be an object")

    return payload


def normalize_server_command(server_command: list[str]) -> list[str]:
    command = list(server_command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ValueError("server command is required; pass it after '--'")
    return command


def print_json(payload: Any, *, compact: bool) -> None:
    if compact:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2))


async def _run_command(args: argparse.Namespace) -> Any:
    command = normalize_server_command(args.server_command)
    async with StdioMcpClient(command, timeout_seconds=args.timeout) as client:
        if args.command == "list-tools":
            return await client.list_tools()
        if args.command == "call-tool":
            return await client.call_tool(args.name, parse_tool_arguments(args.arguments))
        if args.command == "script":
            return await execute_script_steps(client, load_script_steps(Path(args.file)))
        raise ValueError(f"unsupported command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        payload = asyncio.run(_run_command(args))
    except (ValueError, McpClientError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print_json(payload, compact=args.compact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
