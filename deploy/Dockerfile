### Stage 1: builder — builds Python venv ###
FROM python:3.14-slim@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1 AS builder

# uv from official image — no curl or shell pipe
COPY --from=ghcr.io/astral-sh/uv:0.11.17@sha256:03bdc89bb9798628846e60c3a9ad19006c8c3c724ccd2985a33145c039a0577b /uv /usr/local/bin/uv

# --- Dependency layer (cached when lock files unchanged) ---
COPY pyproject.toml uv.lock /src/mcp-telegram/
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy \
    uv sync --project /src/mcp-telegram --frozen --no-dev --no-install-project

# --- Source layer (invalidated when src/ changes) ---
COPY . /src/mcp-telegram/
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy \
    uv sync --project /src/mcp-telegram --frozen --no-dev --no-editable


### Stage 2: runtime ###
FROM python:3.14-slim@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1

COPY --from=builder /opt/venv /opt/venv
ENV HOME="/home/mcp-telegram" \
    XDG_CONFIG_HOME="/home/mcp-telegram/.config" \
    PATH="/opt/venv/bin:$PATH"

COPY --chmod=755 deploy/scripts/healthcheck_daemon.py /usr/local/bin/healthcheck_daemon.py
COPY --chmod=755 deploy/scripts/healthcheck_http.py /usr/local/bin/healthcheck_http.py
COPY --chmod=755 deploy/scripts/healthcheck_all.sh /usr/local/bin/healthcheck_all.sh

RUN groupadd --gid 10001 mcp-telegram && \
    useradd --uid 10001 --gid mcp-telegram --create-home \
      --home-dir /home/mcp-telegram --shell /usr/sbin/nologin mcp-telegram && \
    install -d -o mcp-telegram -g mcp-telegram -m 700 /home/mcp-telegram/.config/mcp-telegram && \
    install -d -o mcp-telegram -g mcp-telegram -m 700 /srv/mcp-telegram/database

HEALTHCHECK --interval=30s --timeout=15s --start-period=30s --retries=3 \
  CMD ["/usr/local/bin/healthcheck_all.sh"]

USER 10001:10001

CMD ["mcp-telegram", "serve"]
