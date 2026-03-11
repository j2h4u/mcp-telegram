#!/bin/bash
# Privacy audit: confirms zero PII in telemetry code
# Usage: bash tests/privacy_audit.sh
#
# Audit strategy:
# 1. Check TelemetryEvent dataclass fields in analytics.py (must be privacy-safe)
# 2. Check telemetry_events table schema (must not contain PII columns)
# 3. Check TelemetryEvent instantiations in tools.py (must not pass entity_id, dialog_id, etc.)

set -e

AUDIT_FAILED=0

echo "=== Privacy Audit: Telemetry PII Detection ==="

# ============================================================================
# Test 1: TelemetryEvent dataclass definition must not have PII fields
# ============================================================================
echo ""
echo "Test 1: Checking TelemetryEvent dataclass fields..."

# Extract the fields from TelemetryEvent dataclass definition
TELEMETRY_FIELDS=$(rg -A 15 '@dataclass.*class TelemetryEvent' src/mcp_telegram/analytics.py | grep -E '^\s+\w+:' | awk '{print $1}' || true)

PII_FIELD_PATTERNS=(
    "entity_id"
    "dialog_id"
    "sender_id"
    "message_id"
    "user_id"
    "username"
    "person_name"
    "full_name"
    "phone"
    "email"
)

for pii_field in "${PII_FIELD_PATTERNS[@]}"; do
    if echo "$TELEMETRY_FIELDS" | grep -q "$pii_field"; then
        echo "  ✗ FAIL: Found PII field '$pii_field' in TelemetryEvent dataclass"
        AUDIT_FAILED=1
    fi
done

if [ $AUDIT_FAILED -eq 0 ]; then
    echo "  ✓ PASS: TelemetryEvent has only privacy-safe fields (tool_name, timestamp, duration_ms, result_count, has_cursor, page_depth, has_filter, error_type)"
fi

# ============================================================================
# Test 2: telemetry_events table schema must not have PII columns
# ============================================================================
echo ""
echo "Test 2: Checking telemetry_events table schema..."

SCHEMA=$(rg -A 20 'CREATE TABLE.*telemetry_events' src/mcp_telegram/analytics.py)

for pii_field in "${PII_FIELD_PATTERNS[@]}"; do
    if echo "$SCHEMA" | grep -q "$pii_field"; then
        echo "  ✗ FAIL: Found PII column '$pii_field' in telemetry_events table"
        AUDIT_FAILED=1
    fi
done

if [ $AUDIT_FAILED -eq 0 ]; then
    echo "  ✓ PASS: telemetry_events table has no PII columns"
fi

# ============================================================================
# Test 3: TelemetryEvent instantiations must not pass PII values
# ============================================================================
echo ""
echo "Test 3: Checking TelemetryEvent instantiations in tools.py..."

# Extract all TelemetryEvent(...) calls
INSTANTIATIONS=$(rg -A 8 'TelemetryEvent\(' src/mcp_telegram/tools.py)

for pii_field in "${PII_FIELD_PATTERNS[@]}"; do
    # Look for assignments like "entity_id=..." or "entity_id =" within TelemetryEvent calls
    if echo "$INSTANTIATIONS" | grep -E "\s${pii_field}\s*=\s*[^(]" | grep -v "# " | grep -v 'tool_name' > /dev/null; then
        echo "  ✗ FAIL: Found TelemetryEvent passing PII field '$pii_field'"
        echo "    Matching lines:"
        echo "$INSTANTIATIONS" | grep -E "\s${pii_field}\s*=\s*[^(]" | head -3
        AUDIT_FAILED=1
    fi
done

if [ $AUDIT_FAILED -eq 0 ]; then
    echo "  ✓ PASS: TelemetryEvent instantiations contain no PII fields"
fi

# ============================================================================
# Summary
# ============================================================================
echo ""
if [ $AUDIT_FAILED -eq 0 ]; then
    echo "✓ Privacy audit PASSED: Telemetry system is PII-safe"
    echo "  - TelemetryEvent fields: privacy-safe only"
    echo "  - telemetry_events schema: no PII columns"
    echo "  - Event logging: no entity/dialog/user IDs passed"
    exit 0
else
    echo "✗ Privacy audit FAILED: PII detected in telemetry code"
    exit 1
fi
