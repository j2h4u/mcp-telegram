# MCP Test Client

Small Streamable HTTP MCP client for local regression testing. It connects to
`http://127.0.0.1:3100/mcp` by default; use `--url` to select another endpoint.

```bash
uv run python -m devtools.mcp_client.cli list-tools
uv run python -m devtools.mcp_client.cli list-prompts
uv run python -m devtools.mcp_client.cli get-prompt --name telegram_workflows
uv run python -m devtools.mcp_client.cli call-tool \
  --name list_topics \
  --arguments '{"dialog":"Studio Robots and Inbox"}'
```

## Smoke Tests

Run the integration smoke against the live HTTP service:

```bash
uv run python -m devtools.mcp_client.cli script \
  --file devtools/mcp_client/smoke-integration.json
```

## Script Format

Run several actions in one MCP session:

```json
{
  "steps": [
    {"action": "list_tools"},
    {"action": "list_prompts"},
    {"action": "get_prompt", "name": "telegram_workflows"},
    {"action": "call_tool", "name": "list_dialogs", "arguments": {}}
  ]
}
```

The `script` format supports assertions through `expect`, including tool and
prompt name checks, path checks, error state checks, and text containment checks.
