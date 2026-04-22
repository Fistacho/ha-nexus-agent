#!/usr/bin/env sh

OPTIONS="/data/options.json"

if [ -f "$OPTIONS" ]; then
    PORT=$(jq -r '.port // 7123' "$OPTIONS")
    LOG_LEVEL=$(jq -r '.log_level // "info"' "$OPTIONS")
    GIT_VERSIONING_AUTO=$(jq -r '.git_versioning_auto // true' "$OPTIONS")
    MAX_BACKUPS=$(jq -r '.max_backups // 30' "$OPTIONS")
    API_KEY=$(jq -r '.api_key // ""' "$OPTIONS")
else
    PORT=${PORT:-7123}
    LOG_LEVEL=${LOG_LEVEL:-info}
    GIT_VERSIONING_AUTO=${GIT_VERSIONING_AUTO:-true}
    MAX_BACKUPS=${MAX_BACKUPS:-30}
    API_KEY=""
fi

export PORT LOG_LEVEL GIT_VERSIONING_AUTO MAX_BACKUPS
export SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN}"
export HA_URL="http://supervisor/core"
export HA_CONFIG_PATH="/config"
export NEXUS_PORT="${PORT}"

if [ -n "${API_KEY}" ]; then
    export NEXUS_API_KEY="${API_KEY}"
fi

echo "Starting Nexus Agent on port ${PORT}..."

exec python3 server.py
