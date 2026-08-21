"""Shared explicit daemon policy for tests that exercise the composition seam."""

from mcp_telegram.config import FreshnessConfig, SchedulingConfig, TelemetryConfig
from mcp_telegram.daemon_api import DaemonApiPolicy


def make_daemon_api_policy() -> DaemonApiPolicy:
    freshness = FreshnessConfig()
    scheduling = SchedulingConfig()
    return DaemonApiPolicy(
        read_at_ttl_seconds=freshness.read_receipts.read_at_ttl_seconds,
        entity_detail_ttl_seconds=freshness.entities.detail_ttl_seconds,
        user_directory_ttl_seconds=freshness.entities.user_directory_ttl_seconds,
        group_directory_ttl_seconds=freshness.entities.group_directory_ttl_seconds,
        resolver_enrichment_ttl_seconds=freshness.entities.resolver_enrichment_ttl_seconds,
        folder_snapshot_stale_after_seconds=scheduling.folder_projection.stale_threshold_seconds,
        telemetry_retention_ttl_seconds=TelemetryConfig().retention_ttl_seconds,
        slow_request_seconds=5.0,
    )
