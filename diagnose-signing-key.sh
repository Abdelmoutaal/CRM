#!/bin/bash
# diagnose-signing-key.sh
# Run on the VPS to diagnose and fix the JWT signing key issue.
# Usage: bash diagnose-signing-key.sh
#
# Prerequisites:
#   - Docker running with twenty_server container
#   - Access to the PostgreSQL database

set -e

echo "=== Twenty CRM Signing Key Diagnostics ==="
echo ""

# 1. Check if server container is running
echo "[1] Checking twenty_server container..."
if docker ps --format '{{.Names}}' | grep -q twenty_server; then
    echo "  OK: twenty_server is running"
else
    echo "  ERROR: twenty_server is not running!"
    exit 1
fi

# 2. Check ENCRYPTION_KEY in the server container
echo ""
echo "[2] Checking ENCRYPTION_KEY in server container..."
ENC_KEY=$(docker exec twenty_server printenv ENCRYPTION_KEY 2>/dev/null || true)
APP_SEC=$(docker exec twenty_server printenv APP_SECRET 2>/dev/null || true)
if [ -n "$ENC_KEY" ]; then
    echo "  OK: ENCRYPTION_KEY is set (length: ${#ENC_KEY})"
elif [ -n "$APP_SEC" ]; then
    echo "  WARN: ENCRYPTION_KEY not set, using APP_SECRET as fallback (length: ${#APP_SEC})"
else
    echo "  ERROR: Neither ENCRYPTION_KEY nor APP_SECRET is set!"
    echo "  Fix: Add ENCRYPTION_KEY to your .env file or docker-compose.yml"
fi

# 3. Check SIGNING_KEY_ROTATION_DAYS
echo ""
echo "[3] Checking SIGNING_KEY_ROTATION_DAYS..."
ROT_DAYS=$(docker exec twenty_server printenv SIGNING_KEY_ROTATION_DAYS 2>/dev/null || true)
if [ -n "$ROT_DAYS" ]; then
    echo "  OK: SIGNING_KEY_ROTATION_DAYS=$ROT_DAYS"
else
    echo "  INFO: SIGNING_KEY_ROTATION_DAYS not set (auto-rotation disabled)"
fi

# 4. Check signing key table
echo ""
echo "[4] Checking core.signingKey table..."
docker exec twenty_db psql -U twenty -d twenty -c "
SELECT count(*) AS total_keys,
       count(*) FILTER (WHERE \"isCurrent\" = true) AS current_keys,
       count(*) FILTER (WHERE \"revokedAt\" IS NOT NULL) AS revoked_keys
FROM core.\"signingKey\";
" 2>/dev/null || echo "  ERROR: Could not query signingKey table"

# 5. Check table schema
echo ""
echo "[5] Checking signingKey table schema..."
docker exec twenty_db psql -U twenty -d twenty -c "\d core.\"signingKey\"" 2>/dev/null | head -30 || echo "  ERROR: Table may not exist"

# 6. Check server logs for signing key errors
echo ""
echo "[6] Recent server logs mentioning 'signing' or 'key'..."
docker logs twenty_server --tail 50 2>&1 | grep -i -E "sign|key|jwt|asymmetric" | tail -10 || echo "  No relevant logs found"

# 7. Check if table is empty and try to fix
echo ""
echo "[7] Checking if signing key is missing..."
KEY_COUNT=$(docker exec twenty_db psql -U twenty -d twenty -t -c "SELECT count(*) FROM core.\"signingKey\"" 2>/dev/null | tr -d ' ')
if [ "$KEY_COUNT" = "0" ]; then
    echo "  WARN: signingKey table is empty!"
    echo "  The server should auto-generate a key on next use (lazy generation)."
    echo "  If it's not working, try restarting the server:"
    echo "    docker restart twenty_server"
    echo ""
    echo "  If still failing after restart, check the full server logs:"
    echo "    docker logs twenty_server --tail 100"
else
    echo "  OK: $KEY_COUNT signing key(s) found"
fi

echo ""
echo "=== Diagnostics complete ==="
