from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("services")


@mcp.tool()
def call_service(domain: str, service: str, data: dict | None = None) -> list[dict]:
    """Call any Home Assistant service. E.g. domain='light', service='turn_on', data={'entity_id':'light.living_room','brightness':200}."""
    return ha.call_service(domain, service, data or {})


@mcp.tool()
def list_services(domain: str | None = None) -> list[dict]:
    """List all available services, optionally filtered by domain."""
    services = ha.list_services()
    if domain:
        services = [s for s in services if s.get("domain") == domain]
    return services


@mcp.tool()
def fire_event(event_type: str, event_data: dict | None = None) -> dict:
    """Fire a Home Assistant event."""
    return ha.fire_event(event_type, event_data)


@mcp.tool()
def render_template(template: str) -> str:
    """Render a Jinja2 template string using HA template engine. Useful for testing templates before saving."""
    return ha.render_template(template)


@mcp.tool()
def reload_config(domain: str) -> list[dict]:
    """Reload configuration for a domain (automation, script, scene, input_boolean, etc.)."""
    return ha.call_service(domain, "reload")


@mcp.tool()
def press_button(entity_id: str) -> list[dict]:
    """Press a button entity."""
    return ha.call_service("button", "press", {"entity_id": entity_id})


@mcp.tool()
def set_cover_position(entity_id: str, position: int) -> list[dict]:
    """Set cover/blind/shutter position (0=closed, 100=open)."""
    return ha.call_service("cover", "set_cover_position", {"entity_id": entity_id, "position": position})


@mcp.tool()
def set_cover_tilt(entity_id: str, tilt_position: int) -> list[dict]:
    """Set cover tilt position (0=closed, 100=open)."""
    return ha.call_service("cover", "set_cover_tilt_position", {"entity_id": entity_id, "tilt_position": tilt_position})


@mcp.tool()
def set_climate_mode(entity_id: str, hvac_mode: str) -> list[dict]:
    """Set climate HVAC mode (heat, cool, auto, off, fan_only, dry)."""
    return ha.call_service("climate", "set_hvac_mode", {"entity_id": entity_id, "hvac_mode": hvac_mode})


@mcp.tool()
def set_light_color(entity_id: str, rgb_color: list[int] | None = None, color_temp: int | None = None, brightness: int | None = None) -> list[dict]:
    """Set light color, color temperature and/or brightness."""
    data: dict = {"entity_id": entity_id}
    if rgb_color:
        data["rgb_color"] = rgb_color
    if color_temp:
        data["color_temp"] = color_temp
    if brightness is not None:
        data["brightness"] = brightness
    return ha.call_service("light", "turn_on", data)


@mcp.tool()
def media_play_pause(entity_id: str) -> list[dict]:
    """Toggle play/pause on a media player."""
    return ha.call_service("media_player", "media_play_pause", {"entity_id": entity_id})


@mcp.tool()
def media_seek(entity_id: str, position: float) -> list[dict]:
    """Seek media player to position (seconds)."""
    return ha.call_service("media_player", "media_seek", {"entity_id": entity_id, "seek_position": position})


@mcp.tool()
def set_volume(entity_id: str, volume: float) -> list[dict]:
    """Set media player volume (0.0 to 1.0)."""
    return ha.call_service("media_player", "volume_set", {"entity_id": entity_id, "volume_level": volume})


@mcp.tool()
def send_notification(message: str, title: str | None = None, target: str | None = None) -> list[dict]:
    """Send a Home Assistant notification. target = notify service name (default: notify)."""
    service = target or "notify"
    data: dict = {"message": message}
    if title:
        data["title"] = title
    domain, svc = ("notify", service) if "." not in service else service.split(".", 1)
    return ha.call_service(domain, svc, data)


# --- Camera ---

@mcp.tool()
def camera_snapshot(entity_id: str, filename: str) -> dict:
    """Take a still snapshot from a camera entity.

    `filename` is a path inside the HA config dir, typically under
    `/config/www/snapshots/...` so it can be served by HA. The directory
    must exist and be allow-listed via `allowlist_external_dirs`.
    """
    result = ha.call_service(
        "camera", "snapshot",
        {"entity_id": entity_id, "filename": filename},
    )
    return {"entity_id": entity_id, "filename": filename, "result": result}


@mcp.tool()
def camera_record(entity_id: str, filename: str, duration: int = 10) -> dict:
    """Record a short clip from a camera entity to `filename` for `duration` seconds.

    `filename` is a path inside the HA config dir (e.g. `/config/www/snapshots/clip.mp4`).
    """
    result = ha.call_service(
        "camera", "record",
        {"entity_id": entity_id, "filename": filename, "duration": duration},
    )
    return {
        "entity_id": entity_id,
        "filename": filename,
        "duration": duration,
        "result": result,
    }


# --- Persistent notifications ---

@mcp.tool()
def notify_persistent_create(message: str, title: str | None = None, notification_id: str | None = None) -> dict:
    """Create or replace a persistent notification in the HA UI.

    Provide `notification_id` to allow the notification to be dismissed/updated
    later via `notify_persistent_dismiss`.
    """
    data: dict = {"message": message}
    if title:
        data["title"] = title
    if notification_id:
        data["notification_id"] = notification_id
    result = ha.call_service("persistent_notification", "create", data)
    return {"notification_id": notification_id, "result": result}


@mcp.tool()
def notify_persistent_dismiss(notification_id: str) -> dict:
    """Dismiss a persistent notification by its `notification_id`."""
    result = ha.call_service(
        "persistent_notification", "dismiss",
        {"notification_id": notification_id},
    )
    return {"notification_id": notification_id, "result": result}


@mcp.tool()
def notify_persistent_dismiss_all() -> dict:
    """Dismiss all persistent notifications."""
    result = ha.call_service("persistent_notification", "dismiss_all")
    return {"result": result}
