#!/usr/bin/env python3
"""Lightweight HTTP healthcheck for the in-container MCP endpoint."""

from __future__ import annotations

import http
import json
import os
import sys
import urllib.error
import urllib.request
from http.client import HTTPResponse
from typing import TypedDict, cast


class _HealthcheckPayload(TypedDict, total=False):
    ok: bool


def main() -> int:
    host = os.environ.get("MCP_TELEGRAM_HTTP_HEALTH_HOST", "127.0.0.1")
    port = os.environ.get("MCP_TELEGRAM_HTTP_PORT", "3100")
    url = f"http://{host}:{port}/health"

    try:
        with cast(HTTPResponse, urllib.request.urlopen(url, timeout=5)) as response:
            if response.status != http.HTTPStatus.OK:
                print(f"HTTP healthcheck failed: status={response.status}", file=sys.stderr)
                return 1
            payload = cast(_HealthcheckPayload, json.loads(response.read().decode("utf-8")))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"HTTP healthcheck failed: {exc}", file=sys.stderr)
        return 1

    if payload.get("ok") is not True:
        print(f"HTTP healthcheck failed: unexpected payload={payload!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
