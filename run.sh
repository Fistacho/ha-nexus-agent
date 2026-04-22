#!/usr/bin/with-contenv bashio

PORT=$(bashio::config 'port')
LOG_LEVEL=$(bashio::config 'log_level')
GIT_VERSIONING_AUTO=$(bashio::config 'git_versioning_auto')
MAX_BACKUPS=$(bashio::config 'max_backups')
API_KEY=$(bashio::config 'api_key')

export PORT="${PORT}"
export LOG_LEVEL="${LOG_LEVEL}"
export GIT_VERSIONING_AUTO="${GIT_VERSIONING_AUTO}"
export MAX_BACKUPS="${MAX_BACKUPS}"
export SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN}"
export HA_URL="http://supervisor/core"
export HA_CONFIG_PATH="/config"
export NEXUS_PORT="${PORT}"

if [ -n "${API_KEY}" ]; then
  export NEXUS_API_KEY="${API_KEY}"
fi

bashio::log.info "Starting Nexus Agent on port ${PORT}..."

exec python3 server.py
