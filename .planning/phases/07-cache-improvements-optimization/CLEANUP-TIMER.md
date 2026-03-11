# Daily Analytics Cleanup Strategy (Phase 7, Plan 03)

## Timer Configuration

### Unit Name
- **Service:** `mcp-telegram-cleanup.service`
- **Timer:** `mcp-telegram-cleanup.timer`

### Schedule
```ini
[Timer]
OnCalendar=*-*-* 07:15:00
RandomizedDelaySec=600
Unit=mcp-telegram-cleanup.service
```

**Frequency:** Daily at 07:15 AM
**Jitter:** ±10 min (RandomizedDelaySec=600) per /home/j2h4u/AGENTS.md guidelines to prevent thundering herd

### Service Definition
```ini
[Unit]
Description=Daily cleanup of analytics.db telemetry events
Wants=mcp-telegram-cleanup.timer

[Service]
Type=oneshot
ExecStart=/path/to/mcp-telegram-cleanup.sh
StandardOutput=journal
StandardError=journal
```

### Cleanup Script
```bash
#!/bin/bash
# /usr/local/bin/mcp-telegram-cleanup.sh

set -e

# Load environment if needed (API keys, DB paths)
export PYTHONUNBUFFERED=1

# Run Python cleanup function
python3 -c "
import asyncio
from pathlib import Path
from mcp_telegram.analytics import cleanup_analytics_db

db_path = Path.home() / '.local/share/mcp-telegram/analytics.db'
asyncio.run(cleanup_analytics_db(db_path, retention_days=30))
"
```

## Cleanup Operations

### 1. Delete Stale Events
- **Query:** `DELETE FROM telemetry_events WHERE timestamp < ?`
- **Threshold:** 30 days (2,592,000 seconds)
- **Typical deletion:** ~30–50 events/day depending on tool usage

### 2. Rebuild Statistics
- **Operation:** `PRAGMA optimize`
- **Effect:** Rebuilds query planner statistics (automatic on WAL mode)
- **Cost:** <100 ms on typical ~50 MB database

### 3. Reclaim Disk Space
- **Operation:** `PRAGMA incremental_vacuum(1000)`
- **Effect:** Frees up to 1000 pages without blocking readers
- **Non-blocking:** Readers can access DB during vacuum (WAL mode advantage)

## Expected Performance

- **Runtime:** <5 seconds on typical ~50 MB database
- **Disk reclaimed:** ~1–5 MB per cleanup (depends on delete volume)
- **Blocking:** None (incremental_vacuum is non-blocking; readers not affected)

## Manual Verification (Required Before Wave 2 Completion)

Before shipping timer-based cleanup, verify each step manually:

```bash
# 1. Verify timer file exists and is correct
systemctl list-timers mcp-telegram-cleanup.timer
systemctl status mcp-telegram-cleanup.timer

# 2. Enable timer
sudo systemctl --user enable mcp-telegram-cleanup.timer

# 3. Test run: start service immediately
sudo systemctl --user start mcp-telegram-cleanup.service

# 4. Check logs
journalctl -u mcp-telegram-cleanup.service -n 20

# 5. Verify telemetry deletion (manual check)
# Before cleanup:
sqlite3 ~/.local/share/mcp-telegram/analytics.db \
  "SELECT COUNT(*) FROM telemetry_events;"
# Run cleanup:
python3 -c "
import asyncio
from pathlib import Path
from mcp_telegram.analytics import cleanup_analytics_db
db_path = Path.home() / '.local/share/mcp-telegram/analytics.db'
asyncio.run(cleanup_analytics_db(db_path, retention_days=30))
"
# After cleanup (count should be lower):
sqlite3 ~/.local/share/mcp-telegram/analytics.db \
  "SELECT COUNT(*) FROM telemetry_events;"
```

## Monitoring

### Log Queries
```bash
# View cleanup runs
journalctl -u mcp-telegram-cleanup.service

# View timer triggers
journalctl -u mcp-telegram-cleanup.timer

# Follow cleanup in real-time
journalctl -u mcp-telegram-cleanup.service -f
```

### Alerts (if integrated with Grafana Alloy)
- **High failure rate:** Timer should run successfully ≥95% of time
- **Exceeding runtime:** If cleanup takes >30 seconds, investigate:
  - Very large database (>200 MB?)
  - Filesystem issues (IOPS saturation?)
  - SQLite lock contention

## Database Bounded Growth

### Analytics DB Size Projection

| Retention | Daily Events | Disk Size |
|-----------|--------------|-----------|
| 7 days    | 50           | 2 MB      |
| 30 days   | 50           | 8 MB      |
| 30 days   | 500          | 80 MB     |
| 30 days   | 1000         | 160 MB    |

**Current setup:** 30-day retention, ~100 events/day → ~10 MB steady state

After cleanup: database shrinks to ~5–10 MB (daily usage) and remains stable.

## Design Decisions

1. **Async cleanup_analytics_db()** ensures main mcp-telegram service is not blocked during maintenance
2. **Separate analytics.db from entity_cache.db** (Phase 6 decision) prevents write contention
3. **Incremental VACUUM** chosen over full VACUUM to avoid locking readers
4. **PRAGMA optimize** after DELETE rebuilds indexes for ongoing query efficiency
5. **SystemD timer** with jitter prevents thundering herd if multiple instances run
6. **30-day retention** balances historical data with disk efficiency (tunable per REQUIREMENTS.md CACHE-03)

## Future Enhancements

- [ ] Archive old telemetry to separate "archive.db" for long-term analysis
- [ ] Implement adaptive retention (shrink if database grows >200 MB)
- [ ] Dashboard widget: "DB size trend, cleanup runs, deleted events"
- [ ] Alert if cleanup fails two days in a row
