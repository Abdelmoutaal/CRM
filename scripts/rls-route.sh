#!/usr/bin/env bash
# This script keeps the RLS bridge routed. The RLS bridge is intentionally a
# SEPARATE container (rls-bridge) from the legacy earshot bridge (u9s6mhbm7...)
# which continues to handle meeting webhooks. The two never collide on DNS.

PROXY_DYN_DIR="${PROXY_DYN_DIR:-/data/coolify/proxy/dynamic}"

mkdir -p "$PROXY_DYN_DIR"

cat > "$PROXY_DYN_DIR/rls-bridge.yaml" << 'EOF'
http:
  middlewares:
    strip-rls:
      stripprefix:
        prefixes:
          - /rls-bridge
  routers:
    rls-bridge-https:
      entryPoints:
        - https
      rule: 'Host(`crm.digitalstratify.co`) && PathPrefix(`/rls-bridge`)'
      middlewares:
        - gzip
        - strip-rls
      service: rls-bridge
      tls:
        certresolver: letsencrypt
    rls-bridge-http:
      entryPoints:
        - http
      rule: 'Host(`crm.digitalstratify.co`) && PathPrefix(`/rls-bridge`)'
      middlewares:
        - redirect-to-https
        - strip-rls
      service: rls-bridge
  services:
    rls-bridge:
      loadBalancer:
        servers:
          - url: 'http://rls-bridge:8100'
EOF

echo "[rls-route] Writing $PROXY_DYN_DIR/rls-bridge.yaml"

# Reload Traefik (Coolify proxy is named coolify-proxy)
if docker ps --format '{{.Names}}' | grep -q '^coolify-proxy$'; then
  docker exec coolify-proxy kill -1 1 || true
  echo "[rls-route] Coolify proxy reloaded"
fi

echo "[rls-route] Done"
