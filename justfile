set shell := ["bash", "-eu", "-o", "pipefail", "-c"]

compose_file := "/opt/docker/mcp-telegram/docker-compose.yml"
container := "mcp-telegram"
mcp_command := "docker exec -i mcp-telegram mcp-telegram run"

default:
    @just --list

# Run all local source checks.
check: lint typecheck test

# Run ruff over source, tests, and deploy helpers.
lint:
    uv run ruff check src tests deploy

# Run mypy over the package and deploy helpers.
typecheck:
    uv run mypy src/mcp_telegram deploy

# Run pytest. Extra args are forwarded, e.g. `just test tests/test_daemon_api.py -q`.
test *args:
    uv run pytest {{args}}

# Dead-code sieve (advisory — vulture has false positives, read with judgment).
deadcode:
    uv run vulture

# Test coverage report.
coverage:
    uv run pytest --cov=src/mcp_telegram --cov-report=term-missing

# Rebuild and restart the live Docker container.
runtime-build:
    docker compose -f {{compose_file}} up -d --build {{container}}

# Wait until the live Docker container reports healthy.
runtime-wait:
    for _ in {1..60}; do \
      status="$(docker inspect {{container}} 2>/dev/null | jq -r '.[0].State.Health.Status // empty')"; \
      echo "$status"; \
      [ "$status" = healthy ] && exit 0; \
      sleep 1; \
    done; \
    exit 1

# Run the redacted stdio MCP integration smoke against the live container.
runtime-smoke-stdio:
    uv run python -m devtools.mcp_client.cli script --redact --file devtools/mcp_client/smoke-integration.json -- {{mcp_command}}

# Run an HTTP MCP smoke against the live container.
runtime-smoke-http:
    uv run python -m devtools.mcp_client.cli list-tools --url http://127.0.0.1:3100/mcp > /tmp/mcp-telegram-http-tools.json
    count="$(jq 'length' /tmp/mcp-telegram-http-tools.json)"; echo "http_tool_count $count"; test "$count" -gt 0

# Run MCP smoke tests against the live container.
runtime-smoke: runtime-smoke-stdio runtime-smoke-http

# Rebuild the live container and run the redacted MCP smoke.
runtime-verify: runtime-build runtime-wait runtime-smoke

# Run local checks, rebuild the runtime, and smoke-test live MCP behavior.
verify: check runtime-verify

# Show live Docker container state.
runtime-status:
    docker compose -f {{compose_file}} ps {{container}}
