set shell := ["bash", "-eu", "-o", "pipefail", "-c"]
export UV_LINK_MODE := "hardlink"

compose_file := "/opt/docker/mcp-telegram/docker-compose.yml"
container := "mcp-telegram"
mcp_command := "docker exec -i mcp-telegram mcp-telegram run"

default:
    @just --list

# Run all local source checks.
check: fmt-check lint preview-complexity-lint typecheck-pyright typecheck-tests import-contracts actionlint deptry compile deadcode

# Run ruff over source, tests, and deploy helpers.
lint:
    uv run ruff check src tests deploy

# Check selected complexity/refactor rules from Ruff preview.
preview-complexity-lint:
    uv run ruff check --preview --select PLR0914,PLR0916,PLR0917 src tests deploy

# Check formatting without writing.
fmt-check:
    uv run ruff format --check src tests deploy

# Run mypy over the package and deploy helpers.
typecheck:
    uv run mypy src/mcp_telegram deploy

# Run basedpyright over the package and deploy helpers.
typecheck-pyright:
    uv run basedpyright src/mcp_telegram deploy --warnings

# Type-check tests with basedpyright.
typecheck-tests:
    uv run basedpyright tests --warnings

# Check import-layer architecture contracts.
import-contracts:
    uv run lint-imports

# Check GitHub Actions workflow syntax and expressions.
actionlint:
    uv run actionlint

# Check declared dependencies against imports.
deptry:
    uv run deptry src/mcp_telegram deploy tests devtools --known-first-party mcp_telegram --known-first-party devtools --known-first-party tests --known-first-party helpers --known-first-party account_trace_fixtures --per-rule-ignores DEP004=radon

# Compile Python sources for syntax errors.
compile:
    uv run python -m compileall -q src tests deploy

# Run pytest. Extra args are forwarded, e.g. `just test tests/test_daemon_api.py -q`.
test *args:
    uv run pytest {{args}}

# Run a bounded parallel pytest slice. Avoid `-n auto` on this host: execnet can
# hit thread limits during worker teardown.
test-parallel *args:
    uv run pytest -n 2 {{args}}

# Unit tests.
unit:
    uv run pytest

# Dead-code sieve (advisory — vulture has false positives, read with judgment).
deadcode:
    uv run vulture

# Test coverage report.
coverage:
    uv run pytest -W error::ResourceWarning --cov=src/mcp_telegram --cov-report=term-missing

# Human CRAP report over the full suite.
crap:
    uv run pytest --cov=src/mcp_telegram --cov-report=term-missing --crap --crap-threshold=30 --crap-top-n=30

# CI/regression CRAP gate that checks the tracked baseline.
crap-check: crap-ratchet

# Regenerate the tracked CRAP baseline from the current coverage state.
crap-baseline:
    coverage_file="$(mktemp /tmp/mcp-telegram-crap-coverage.XXXXXX.json)"; \
    trap 'rm -f "$coverage_file"' EXIT; \
    uv run pytest --cov=src/mcp_telegram --cov-report=json:"$coverage_file"; \
    uv run python -m devtools.crap_ratchet --coverage "$coverage_file" --baseline reports/crap-baseline.json --src src/mcp_telegram --threshold 30 --write-baseline

# Tighten the tracked CRAP baseline by clamping existing entries downward and adding
# only new entries that are at/below threshold.
crap-tighten:
    coverage_file="$(mktemp /tmp/mcp-telegram-crap-coverage.XXXXXX.json)"; \
    trap 'rm -f "$coverage_file"' EXIT; \
    uv run pytest --cov=src/mcp_telegram --cov-report=json:"$coverage_file"; \
    uv run python -m devtools.crap_ratchet --coverage "$coverage_file" --baseline reports/crap-baseline.json --src src/mcp_telegram --threshold 30 --tighten-baseline

# Enforce the CRAP ratchet against the tracked baseline.
crap-ratchet:
    coverage_file="$(mktemp /tmp/mcp-telegram-crap-coverage.XXXXXX.json)"; \
    trap 'rm -f "$coverage_file"' EXIT; \
    uv run pytest --cov=src/mcp_telegram --cov-report=json:"$coverage_file"; \
    uv run python -m devtools.crap_ratchet --coverage "$coverage_file" --baseline reports/crap-baseline.json --src src/mcp_telegram --threshold 30

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

# Auto-fix ruff findings and formatting.
fix:
    uv run ruff check --fix src tests deploy
    uv run ruff format src tests deploy

# Run local checks, CRAP ratchet, rebuild the runtime, and smoke-test live MCP behavior.
verify: check crap-ratchet runtime-verify

# Show live Docker container state.
runtime-status:
    docker compose -f {{compose_file}} ps {{container}}
