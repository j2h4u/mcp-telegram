### Stage 1: builder — builds Python venv ###
FROM python:3.14-slim AS builder

# uv from official image — no curl or shell pipe
COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /usr/local/bin/uv

# --- Dependency layer (cached when lock files unchanged) ---
COPY --from=src pyproject.toml uv.lock /src/mcp-telegram/
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy \
    uv sync --project /src/mcp-telegram --no-install-project

# --- Source layer (invalidated when src/ changes) ---
COPY --from=src . /src/mcp-telegram/
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy \
    uv sync --project /src/mcp-telegram --no-editable

RUN mkdir -p /root/.local/bin && \
    cp /opt/venv/bin/mcp-telegram /root/.local/bin/mcp-telegram


### Stage 2: runtime ###
FROM python:3.14-slim

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /root/.local/bin/mcp-telegram /root/.local/bin/mcp-telegram
ENV PATH="/root/.local/bin:$PATH"

COPY --from=src --chmod=755 deploy/scripts/healthcheck_daemon.py /usr/local/bin/healthcheck_daemon.py
COPY --from=src --chmod=755 deploy/scripts/healthcheck_all.sh /usr/local/bin/healthcheck_all.sh

HEALTHCHECK --interval=30s --timeout=15s --start-period=30s --retries=3 \
  CMD ["/usr/local/bin/healthcheck_all.sh"]

CMD ["mcp-telegram", "sync"]
