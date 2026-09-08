"""Shared explicit daemon policy for tests that exercise the composition seam."""

from mcp_telegram.config import EntityProfileConfig, FreshnessConfig, LoggingConfig, SchedulingConfig, TelemetryConfig
from mcp_telegram.daemon_api import DaemonApiPolicy
from mcp_telegram.entity_profile.refresh import RefreshLimits


def make_daemon_api_policy() -> DaemonApiPolicy:
    freshness = FreshnessConfig()
    entity_profile = EntityProfileConfig()
    scheduling = SchedulingConfig()
    return DaemonApiPolicy(
        read_at_ttl_seconds=freshness.read_receipts.read_at_ttl_seconds,
        deleted_message_visibility_seconds=freshness.inbox.deleted_message_visibility_seconds,
        entity_detail_ttl_seconds=freshness.entities.detail_ttl_seconds,
        user_directory_ttl_seconds=freshness.entities.user_directory_ttl_seconds,
        group_directory_ttl_seconds=freshness.entities.group_directory_ttl_seconds,
        resolver_enrichment_ttl_seconds=freshness.entities.resolver_enrichment_ttl_seconds,
        folder_snapshot_stale_after_seconds=scheduling.folder_projection.stale_threshold_seconds,
        telemetry=TelemetryConfig(),
        slow_request_seconds=LoggingConfig().daemon_api_slow_request_seconds,
        entity_profile=RefreshLimits(
            foreground_resolve_seconds=entity_profile.foreground_resolve_seconds,
            foreground_refresh_wait_seconds=entity_profile.foreground_refresh_wait_seconds,
            per_rpc_seconds=entity_profile.rpc_timeout_seconds,
            whole_refresh_seconds=entity_profile.refresh_timeout_seconds,
            max_concurrent_refreshes=entity_profile.max_concurrent_refreshes,
        ),
    )
