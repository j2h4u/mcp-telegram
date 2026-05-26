set shell := ["bash", "-eu", "-o", "pipefail", "-c"]

compose_file := "/opt/docker/mcp-telegram/docker-compose.yml"
container := "mcp-telegram"
mcp_command := "docker exec -i mcp-telegram mcp-telegram run"

default:
    @just --list

# Run all local source checks.
check: lint typecheck test

# Run ruff over source and tests.
lint:
    uv run ruff check src tests

# Run mypy over the package.
typecheck:
    uv run mypy src/mcp_telegram

# Run pytest. Extra args are forwarded, e.g. `just test tests/test_daemon_api.py -q`.
test *args:
    uv run pytest {{args}}

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

# Run the redacted MCP integration smoke against the live container.
runtime-smoke:
    uv run python -m devtools.mcp_client.cli script --redact --file devtools/mcp_client/smoke-integration.json -- {{mcp_command}}

# Rebuild the live container and run the redacted MCP smoke.
runtime-verify: runtime-build runtime-wait runtime-smoke

# Run local checks, rebuild the runtime, and smoke-test live MCP behavior.
verify: check runtime-verify

# Show live Docker container state.
runtime-status:
    docker compose -f {{compose_file}} ps {{container}}
