#!/usr/bin/env sh

OPTIONS="/data/options.json"

if [ -f "$OPTIONS" ]; then
    PORT=$(jq -r '.port // 7123' "$OPTIONS")
    LOG_LEVEL=$(jq -r '.log_level // "info"' "$OPTIONS")
    GIT_VERSIONING_AUTO=$(jq -r '.git_versioning_auto // true' "$OPTIONS")
    MAX_BACKUPS=$(jq -r '.max_backups // 30' "$OPTIONS")
    API_KEY=$(jq -r '.api_key // ""' "$OPTIONS")
    READ_ONLY=$(jq -r '.read_only // false' "$OPTIONS")
    DISABLED_NAMESPACES=$(jq -r '(.disabled_namespaces // []) | join(",")' "$OPTIONS")
    TOOL_MODE=$(jq -r '.tool_mode // "full"' "$OPTIONS")

    # Add-on / Supervisor mode only: nexus always talks to HA Core through the
    # Supervisor proxy and reads/writes the Supervisor's own /config bind
    # mount here — never whatever HA_URL/HA_CONFIG_PATH a stray .env or
    # `docker -e` might otherwise set, since /data/options.json existing is
    # itself proof this is a real Supervisor-managed container.
    HA_URL="http://supervisor/core"
    HA_CONFIG_PATH="/config"
else
    PORT=${PORT:-7123}
    LOG_LEVEL=${LOG_LEVEL:-info}
    GIT_VERSIONING_AUTO=${GIT_VERSIONING_AUTO:-true}
    MAX_BACKUPS=${MAX_BACKUPS:-30}
    API_KEY=""
    READ_ONLY=${READ_ONLY:-false}
    DISABLED_NAMESPACES=${DISABLED_NAMESPACES:-}
    TOOL_MODE=${TOOL_MODE:-full}

    # Standalone mode (no Supervisor, e.g. `docker run -e HA_URL -e HA_TOKEN`
    # against the ghcr.io image — ADR-0005 condition (b)): honor whatever the
    # caller passed, falling back to the same default ha_client.py itself uses
    # when the var is absent, so behaviour is identical whether nexus reads it
    # or we do. Before this fix, run.sh unconditionally forced
    # HA_URL=http://supervisor/core here too, silently discarding a standalone
    # caller's own -e HA_URL — see tests/test_run_sh_standalone.py (RED
    # without this branch, GREEN with it).
    HA_URL=${HA_URL:-http://homeassistant.local:8123}
    HA_CONFIG_PATH=${HA_CONFIG_PATH:-/config}
fi

export PORT LOG_LEVEL GIT_VERSIONING_AUTO MAX_BACKUPS
export SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN}"
export HA_URL
export HA_CONFIG_PATH
export NEXUS_PORT="${PORT}"
export NEXUS_READ_ONLY="${READ_ONLY}"
export NEXUS_DISABLED_NAMESPACES="${DISABLED_NAMESPACES}"
export NEXUS_TOOL_MODE="${TOOL_MODE}"

if [ -n "${API_KEY}" ]; then
    export NEXUS_API_KEY="${API_KEY}"
fi

echo "Starting Nexus Agent on port ${PORT}..."

exec python3 server.py
