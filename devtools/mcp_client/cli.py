from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from devtools.mcp_client.client import (
    DEFAULT_TIMEOUT_SECONDS,
    HttpMcpClient,
    McpClientError,
    execute_script_steps,
    load_script_steps,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Small MCP client for local testing.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_tools_parser = subparsers.add_parser("list-tools", help="Initialize the server and print tools/list.")
    _add_common_arguments(list_tools_parser)
    _add_http_arguments(list_tools_parser)

    list_prompts_parser = subparsers.add_parser("list-prompts", help="Initialize the server and print prompts/list.")
    _add_common_arguments(list_prompts_parser)
    _add_http_arguments(list_prompts_parser)

    get_prompt_parser = subparsers.add_parser("get-prompt", help="Initialize the server and invoke prompts/get.")
    get_prompt_parser.add_argument("--name", required=True, help="Prompt name to retrieve.")
    get_prompt_parser.add_argument(
        "--arguments",
        default="{}",
        help="JSON object with string prompt arguments. Default: {}",
    )
    _add_common_arguments(get_prompt_parser)
    _add_http_arguments(get_prompt_parser)

    call_tool_parser = subparsers.add_parser("call-tool", help="Initialize the server and invoke one tool.")
    call_tool_parser.add_argument("--name", required=True, help="Tool name to invoke.")
    call_tool_parser.add_argument(
        "--arguments",
        default="{}",
        help="JSON object with tool arguments. Default: {}",
    )
    _add_common_arguments(call_tool_parser)
    _add_http_arguments(call_tool_parser)

    script_parser = subparsers.add_parser("script", help="Run several MCP actions in one session from a JSON file.")
    script_parser.add_argument("--file", required=True, help="Path to a JSON script file.")
    script_parser.add_argument(
        "--redact",
        action="store_true",
        help="Redact call_tool text content in printed script output.",
    )
    _add_common_arguments(script_parser)
    _add_http_arguments(script_parser)

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


def _add_http_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:3100/mcp",
        help="Streamable HTTP MCP endpoint. Default: http://127.0.0.1:3100/mcp",
    )


def parse_tool_arguments(raw_arguments: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--arguments must be valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("--arguments JSON must be an object")

    return payload


def parse_prompt_arguments(raw_arguments: str) -> dict[str, str]:
    payload = parse_tool_arguments(raw_arguments)
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in payload.items()):
        raise ValueError("--arguments JSON must be an object with string values for prompts/get")
    return payload


def print_json(payload: Any, *, compact: bool) -> None:
    if compact:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2))


async def _run_command(args: argparse.Namespace) -> Any:
    async with HttpMcpClient(args.url, timeout_seconds=args.timeout) as client:
        return await _run_client_command(client, args)


async def _run_client_command(client: HttpMcpClient, args: argparse.Namespace) -> Any:
    if args.command == "list-tools":
        return await client.list_tools()
    if args.command == "list-prompts":
        return await client.list_prompts()
    if args.command == "get-prompt":
        return await client.get_prompt(args.name, parse_prompt_arguments(args.arguments))
    if args.command == "call-tool":
        return await client.call_tool(args.name, parse_tool_arguments(args.arguments))
    if args.command == "script":
        payload = await execute_script_steps(client, load_script_steps(Path(args.file)))
        return redact_script_output(payload) if args.redact else payload
    raise ValueError(f"unsupported command: {args.command}")


def redact_script_output(payload: Any) -> Any:
    """Return a printable copy with call_tool text content redacted."""
    redacted = copy.deepcopy(payload)
    if not isinstance(redacted, list):
        return redacted

    for step in redacted:
        if not isinstance(step, dict) or step.get("action") != "call_tool":
            continue
        result = step.get("result")
        if not isinstance(result, dict):
            continue
        content = result.get("content")
        if not isinstance(content, list):
            content = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    item["text"] = f"[REDACTED {len(text)} chars]"
        if "structuredContent" in result:
            result["structuredContent"] = "[REDACTED structuredContent]"
    return redacted


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
