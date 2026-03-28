# MCP Test Client

Small stdio MCP client for local regression testing.

Usage:

```bash
uv run python -m devtools.mcp_client.cli list-tools -- docker exec mcp-telegram mcp-telegram run
```

```bash
uv run python -m devtools.mcp_client.cli call-tool \
  --name ListTopics \
  --arguments '{"dialog":"Studio Robots and Inbox"}' \
  -- docker exec mcp-telegram mcp-telegram run
```

## Smoke Tests

Two smoke scripts cover all 11 MCP tools:

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
      "name": "ListDialogs",
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
