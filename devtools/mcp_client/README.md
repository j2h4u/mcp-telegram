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

Run several actions in one MCP session:

```json
{
  "steps": [
    {"action": "list_tools"},
    {
      "action": "call_tool",
      "name": "ListDialogs",
      "arguments": {}
    },
    {
      "action": "call_tool",
      "name": "ListTopics",
      "arguments": {"dialog": "Studio Robots and Inbox"}
    }
  ]
}
```

```bash
uv run python -m devtools.mcp_client.cli script \
  --file devtools/mcp_client/forum-smoke.json \
  -- docker exec mcp-telegram mcp-telegram run
```
