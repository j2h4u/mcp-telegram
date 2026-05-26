# MCP Test Client

Small MCP client for local regression testing.

Usage:

```bash
uv run python -m devtools.mcp_client.cli list-tools -- docker exec -i mcp-telegram mcp-telegram run
```

Streamable HTTP:

```bash
uv run python -m devtools.mcp_client.cli list-tools --url http://127.0.0.1:3100/mcp
```

```bash
uv run python -m devtools.mcp_client.cli call-tool \
  --name list_topics \
  --arguments '{"dialog":"Studio Robots and Inbox"}' \
  -- docker exec -i mcp-telegram mcp-telegram run
```

## Smoke Tests

Two smoke scripts cover all registered MCP tools:

**No-daemon smoke** — schema validation + graceful degradation (after every build):
```bash
uv run python -m devtools.mcp_client.cli script \
  --file devtools/mcp_client/smoke-no-daemon.json \
  -- docker exec -i mcp-telegram mcp-telegram run
```

**Integration smoke** — real Telegram data (requires running daemon):
```bash
uv run python -m devtools.mcp_client.cli script \
  --file devtools/mcp_client/smoke-integration.json \
  -- docker exec -i mcp-telegram mcp-telegram run
```

## Script Format

Run several actions in one MCP session:

```json
{
  "steps": [
    {"action": "list_tools"},
    {
      "action": "call_tool",
      "name": "list_dialogs",
      "arguments": {}
    }
  ]
}
```

```bash
uv run python -m devtools.mcp_client.cli script \
  --file devtools/mcp_client/your-script.json \
  -- docker exec -i mcp-telegram mcp-telegram run
```

The `script` format supports assertions:

- `expect.tool_names_include`
- `expect.tool_expectations`
- `expect.path_equals`
- `expect.is_error`
- `expect.content_text_contains`
- `expect.content_text_not_contains`
