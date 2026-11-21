#!/usr/bin/env sh

OPTIONS="/data/options.json"

if [ -f "$OPTIONS" ]; then
    LOG_LEVEL=$(jq -r '.log_level // "info"' "$OPTIONS")
    API_KEY=$(jq -r '.api_key // ""' "$OPTIONS")
    READ_ONLY=$(jq -r '.read_only // false' "$OPTIONS")
    DISABLED_NAMESPACES=$(jq -r '(.disabled_namespaces // []) | join(",")' "$OPTIONS")
    TOOL_MODE=$(jq -r '.tool_mode // "full"' "$OPTIONS")
    NODERED_WRITE_ENABLED=$(jq -r '.nodered_write_enabled // false' "$OPTIONS")
    ADDON_EXEC_OPTIONS_ENABLED=$(jq -r '.addon_exec_options_enabled // false' "$OPTIONS")

    # Add-on / Supervisor mode only: nexus always talks to HA Core through the
    # Supervisor proxy and reads/writes the Supervisor's own /config bind
    # mount here — never whatever HA_URL/HA_CONFIG_PATH a stray .env or
    # `docker -e` might otherwise set, since /data/options.json existing is
    # itself proof this is a real Supervisor-managed container.
    HA_URL="http://supervisor/core"
    HA_CONFIG_PATH="/config"
    # The LAN port comes from the add-on's Network tab (addon_network.py,
    # ADR-0004); this is only the fixed ingress_port from config.yaml. The
    # `port` option that used to feed it was removed in 1.0.0.
    NEXUS_PORT=7123
else
    LOG_LEVEL=${LOG_LEVEL:-info}
    # Documented standalone name first; PORT kept as a fallback for anyone
    # who set it before 1.0.0.
    NEXUS_PORT=${NEXUS_PORT:-${PORT:-7123}}
    API_KEY=""
    # Policy options: the documented standalone names are the NEXUS_* ones
    # (README "Environment variables", DOCS.md); the unprefixed names are
    # kept as a fallback for anyone who used them before. Reading only the
    # unprefixed names here used to re-export NEXUS_READ_ONLY=false over a
    # caller's own `-e NEXUS_READ_ONLY=true` -- see
    # tests/test_run_sh_standalone.py. When both names are set, the security
    # options fail closed: read_only is on if either one is not false,
    # Node-RED writes are off if either one is false, and the namespace
    # lists are joined. Any value that is not a recognised false is passed
    # through unchanged, so a typo still reaches policy.py, which refuses to
    # start.
    _is_false() {
        case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
            0|false|no|off) return 0 ;;
            *) return 1 ;;
        esac
    }
    READ_ONLY_LEGACY=${READ_ONLY:-}
    if [ -n "${NEXUS_READ_ONLY:-}" ] && ! _is_false "${NEXUS_READ_ONLY}"; then
        READ_ONLY=${NEXUS_READ_ONLY}
    elif [ -n "${READ_ONLY_LEGACY}" ] && ! _is_false "${READ_ONLY_LEGACY}"; then
        READ_ONLY=${READ_ONLY_LEGACY}
    else
        READ_ONLY=false
    fi
    NODERED_LEGACY=${NODERED_WRITE_ENABLED:-}
    if _is_false "${NEXUS_NODERED_WRITE_ENABLED:-}" || _is_false "${NODERED_LEGACY}"; then
        NODERED_WRITE_ENABLED=false
    else
        NODERED_WRITE_ENABLED=${NEXUS_NODERED_WRITE_ENABLED:-${NODERED_LEGACY:-false}}
    fi
    if [ -n "${NEXUS_DISABLED_NAMESPACES:-}" ] && [ -n "${DISABLED_NAMESPACES:-}" ]; then
        DISABLED_NAMESPACES="${NEXUS_DISABLED_NAMESPACES},${DISABLED_NAMESPACES}"
    else
        DISABLED_NAMESPACES="${NEXUS_DISABLED_NAMESPACES:-${DISABLED_NAMESPACES:-}}"
    fi
    TOOL_MODE=${NEXUS_TOOL_MODE:-${TOOL_MODE:-full}}
    # ADR-0008, new in 1.0.0: no unprefixed legacy name to merge.
    ADDON_EXEC_OPTIONS_ENABLED=${NEXUS_ADDON_EXEC_OPTIONS_ENABLED:-false}

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

export LOG_LEVEL
export SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN}"
export HA_URL
export HA_CONFIG_PATH
export NEXUS_PORT
export NEXUS_READ_ONLY="${READ_ONLY}"
export NEXUS_DISABLED_NAMESPACES="${DISABLED_NAMESPACES}"
export NEXUS_TOOL_MODE="${TOOL_MODE}"
export NEXUS_NODERED_WRITE_ENABLED="${NODERED_WRITE_ENABLED}"
export NEXUS_ADDON_EXEC_OPTIONS_ENABLED="${ADDON_EXEC_OPTIONS_ENABLED}"

if [ -n "${API_KEY}" ]; then
    export NEXUS_API_KEY="${API_KEY}"
fi

# stderr, not stdout: without SUPERVISOR_TOKEN/NEXUS_HTTP the server speaks MCP
# over stdio, and the MCP spec allows nothing but MCP messages on stdout
# (ADR-0005 condition (a)).
echo "Starting Nexus Agent..." >&2

exec python3 server.py
