#!/usr/bin/env bash
# Auto-create the /rls-bridge Traefik route in Coolify.
# Runs as Coolify's post-deploy command so the route is always present after a rebuild.
set -e

PROXY_DYN_DIR="${PROXY_DYN_DIR:-/data/coolify/proxy/dynamic}"

# Stop any stale bridge from the legacy app so DNS "earshot-bridge" hits the right container
LEGACY=$(docker ps --format '{{.Names}}' | grep '^earshot-bridge-u9s6mhbm7ankpjszfjq9kwr0$' || true)
if [ -n "$LEGACY" ]; then
  echo "[rls-route] Removing legacy bridge container: $LEGACY"
  docker update --restart=no "$LEGACY" >/dev/null 2>&1 || true
  docker rm -f "$LEGACY" >/dev/null 2>&1 || true
fi

mkdir -p "$PROXY_DYN_DIR"

cat > "$PROXY_DYN_DIR/earshot-bridge.yaml" << 'EOF'
http:
  middlewares:
    strip-rls:
      stripprefix:
        prefixes:
          - /rls-bridge
  routers:
    earshot-bridge-https:
      entryPoints:
        - https
      rule: 'Host(`crm.digitalstratify.co`) && PathPrefix(`/rls-bridge`)'
      middlewares:
        - gzip
        - strip-rls
      service: earshot-bridge
      tls:
        certresolver: letsencrypt
    earshot-bridge-http:
      entryPoints:
        - http
      rule: 'Host(`crm.digitalstratify.co`) && PathPrefix(`/rls-bridge`)'
      middlewares:
        - redirect-to-https
        - strip-rls
      service: earshot-bridge
  services:
    earshot-bridge:
      loadBalancer:
        servers:
          - url: 'http://earshot-bridge:8100'
EOF

echo "[rls-route] Writing $PROXY_DYN_DIR/earshot-bridge.yaml"

# Reload Traefik (Coolify proxy is named coolify-proxy)
if docker ps --format '{{.Names}}' | grep -q '^coolify-proxy$'; then
  docker exec coolify-proxy kill -1 1 || true
  echo "[rls-route] Coolify proxy reloaded"
fi

echo "[rls-route] Done"
