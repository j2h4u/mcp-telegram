#!/usr/bin/env bash
set -euo pipefail

function die {
    local -r message="${1:-}"
    local -ri code="${2:-1}"

    echo "FATAL: ${message}"
    exit "$code"
} 1>&2

function main {
    command -v python3 >/dev/null 2>&1 || die "python3 is not installed"

    [[ -x /usr/local/bin/healthcheck_daemon.py ]] || die "missing daemon healthcheck script"
    python3 /usr/local/bin/healthcheck_daemon.py
}

main "$@"
