#!/usr/bin/env bash
set -uo pipefail

function die {
    local -r message="${1:-}"
    local -ri code="${2:-1}"

    echo "FATAL: ${message}"
    exit "$code"
} 1>&2

function main {
    local -i status=0
    command -v python3 >/dev/null 2>&1 || die "python3 is not installed"

    [[ -x /usr/local/bin/healthcheck_daemon.py ]] || die "missing daemon healthcheck script"
    [[ -x /usr/local/bin/healthcheck_http.py ]] || die "missing HTTP healthcheck script"
    python3 /usr/local/bin/healthcheck_daemon.py || status=1
    python3 /usr/local/bin/healthcheck_http.py || status=1
    return "$status"
}

main "$@"
