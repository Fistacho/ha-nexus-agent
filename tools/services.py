"""Generic service dispatch plus fixed convenience actions for common domains.

`call_service`/`fire_event` are open-ended: the effect depends entirely on
the domain/service or event type passed in. Everything else here is a
narrow, named action (cover position, climate mode, light color, media
control, notifications) that wraps one specific Home Assistant service.
"""
from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

import ha_client as ha
import self_protection
import service_guard
from tools._contract import destructive, read, write

mcp = FastMCP("services")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}


@mcp.tool(annotations=destructive("Call any Home Assistant action", idempotent=False, open_world=True))
def call_service(
    domain: Annotated[str, Field(description="Service domain to call, e.g. 'light' or 'climate'.")],
    service: Annotated[str, Field(description="Service name within the domain, e.g. 'turn_on' or 'set_temperature'.")],
    data: Annotated[
        dict | None,
        Field(
            description=(
                "Service data: fields plus targets (entity_id/area_id/device_id). "
                "Omit for a call that needs no data."
            )
        ),
    ] = None,
    confirm: Annotated[
        bool,
        Field(
            description=(
                "Required True to actually run one of eight guarded services: "
                "homeassistant.restart/stop, hassio.host_reboot/host_shutdown/"
                "restore_full/restore_partial, update.install, group.remove. "
                "False (default) performs no action for those instead and "
                "returns a confirmation prompt; ignored for every other "
                "service."
            )
        ),
    ] = False,
) -> list[dict]:
    """Call any Home Assistant action by domain and service name over HTTP.

    Posts to `/api/services/<domain>/<service>` with `data` as the JSON
    body; `data` holds both fields and targets (`entity_id`/`area_id`/
    `device_id`). Effect depends entirely on the domain/service passed.
    `domain`/`service` must match `[A-Za-z0-9_]+` (ADR-0006 D3) or the call
    is refused before any request is built. Refuses `hassio.addon_stop`/
    `app_stop`/`addon_stdin`/`app_stdin` against nexus's own add-on
    (`self_protection.is_own_addon`); `addon_restart`/`app_restart` stay
    allowed. `homeassistant.restart`/`stop`, `hassio.host_reboot`/
    `host_shutdown`/`restore_full`/`restore_partial`, `update.install` and
    `group.remove` additionally require `confirm=True` (ADR-0006 D2) — a
    human-in-the-loop checkpoint, not a security boundary; ignored for
    every other service.

    Use when: no dedicated tool exists for the action needed.
    Not for: an action returning response data (e.g.
    `weather.get_forecasts`) — use `ws_call_service`, which refuses the
    same eight guarded services outright (ADR-0006 D5) instead of
    accepting `confirm`.
    Returns: the list of states changed during the call.
    Errors: `{"error": "invalid_service_name"}` for a malformed
    domain/service; `{"error": "self_addon_hassio_service_blocked",
    "message": ...}` for the blocked `hassio.*` cases; `{"error":
    "confirmation_required", "message": ..., "action": ...}` for a guarded
    service called without `confirm=True`.
    """
    if not (service_guard.validate_service_name(domain) and service_guard.validate_service_name(service)):
        return {"error": "invalid_service_name"}
    blocked = self_protection.blocked_hassio_service_call(domain, service, data)
    if blocked is not None:
        return blocked
    gated = service_guard.confirm_gate(domain, service, data, confirm=confirm)
    if gated is not None:
        return gated
    return ha.call_service(domain, service, data or {})


@mcp.tool(annotations=read("List available services"))
def list_services(
    domain: Annotated[
        str | None, Field(description="Service domain filter, e.g. 'light'. Omit to list every domain.")
    ] = None,
) -> list[dict]:
    """List all services Home Assistant currently has registered, optionally filtered by domain.

    Calls `ha.list_services()` (HTTP GET `/api/services`) and filters the
    result client-side by `domain` when given.

    Use when: discovering which services and fields a domain supports before
    calling `services_call_service`.
    Returns: list of dicts, one per domain, each with its available services
    and their fields.
    """
    services = ha.list_services()
    if domain:
        services = [s for s in services if s.get("domain") == domain]
    return services


@mcp.tool(annotations=destructive("Fire a Home Assistant event", idempotent=False, open_world=True))
def fire_event(
    event_type: Annotated[str, Field(description="Event type to fire, e.g. 'my_custom_event'.")],
    event_data: Annotated[
        dict | None, Field(description="Event data payload delivered to listeners. Omit for no payload.")
    ] = None,
) -> dict:
    """Fire an arbitrary Home Assistant event with an optional data payload.

    Posts to `/api/events/<event_type>` with `event_data` as the JSON body;
    any automation or integration listening for `event_type` reacts to it,
    so the effect depends entirely on what is registered to listen.

    Use when: triggering automations or integrations that listen for a
    specific event type.
    Returns: dict confirming the event was fired, as returned by Home
    Assistant.
    """
    return ha.fire_event(event_type, event_data)


@mcp.tool(annotations=read("Render a Jinja2 template"))
def render_template(
    template: Annotated[str, Field(description="Jinja2 template string to render, e.g. '{{ states(\"sensor.temp\") }}'.")],
) -> str:
    """Render a Jinja2 template string once through HA's template engine over HTTP.

    Posts to `/api/template` with `{"template": template}` and returns the
    rendered text.

    Use when: testing a template's output before saving it into an
    automation, script or template sensor.
    Not for: rendering through the WebSocket subscription transport — use
    `ws_render_template`.
    Returns: the rendered template as plain text.
    """
    return ha.render_template(template)


@mcp.tool(annotations=write("Reload domain configuration", idempotent=True))
def reload_config(
    domain: Annotated[str, Field(description="Domain to reload, e.g. 'automation', 'script' or 'scene'.")],
) -> list[dict]:
    """Reload one domain's configuration from disk by calling its reload action.

    Calls `<domain>.reload` (e.g. `automation.reload`, `input_boolean.reload`)
    over HTTP with no data.

    Use when: applying YAML changes for automations, scripts, scenes or
    input helpers without restarting Home Assistant.
    Not for: reloading every helper domain in one call — use
    `helpers_reload_helpers`.
    Returns: the list of states changed by the reload.
    """
    return ha.call_service(domain, "reload")


@mcp.tool(annotations=write("Press a button entity", idempotent=False))
def press_button(
    entity_id: Annotated[str, Field(description="Full button entity ID to press, e.g. 'button.restart_service'.")],
) -> list[dict]:
    """Press a button entity, running whatever action it is configured to perform.

    Calls `button.press` with `{"entity_id": entity_id}` over HTTP.

    Use when: running a button entity's configured action, such as a
    template button or a device's physical-button equivalent.
    Returns: the list of states changed by the press.
    """
    return ha.call_service("button", "press", {"entity_id": entity_id})


@mcp.tool(annotations=write("Set cover position", idempotent=True))
def set_cover_position(
    entity_id: Annotated[str, Field(description="Full cover entity ID to move, e.g. 'cover.living_room_blind'.")],
    position: Annotated[int, Field(description="Target position, 0 (closed) to 100 (open).")],
) -> list[dict]:
    """Set a cover/blind/shutter to a specific open position.

    Calls `cover.set_cover_position` with `{"entity_id": entity_id,
    "position": position}` over HTTP.

    Use when: moving a cover to a specific percentage rather than fully
    open/closed.
    Returns: the list of states changed by the call.
    """
    return ha.call_service("cover", "set_cover_position", {"entity_id": entity_id, "position": position})


@mcp.tool(annotations=write("Set cover tilt position", idempotent=True))
def set_cover_tilt(
    entity_id: Annotated[str, Field(description="Full cover entity ID to tilt, e.g. 'cover.living_room_blind'.")],
    tilt_position: Annotated[int, Field(description="Target tilt position, 0 (closed) to 100 (open).")],
) -> list[dict]:
    """Set a cover's slat/tilt position independently of its open/closed position.

    Calls `cover.set_cover_tilt_position` with `{"entity_id": entity_id,
    "tilt_position": tilt_position}` over HTTP.

    Use when: adjusting slat angle on a cover that supports tilt.
    Not for: the cover's overall open/closed position — use
    `services_set_cover_position`.
    Returns: the list of states changed by the call.
    """
    return ha.call_service("cover", "set_cover_tilt_position", {"entity_id": entity_id, "tilt_position": tilt_position})


@mcp.tool(annotations=write("Set climate mode", idempotent=True))
def set_climate_mode(
    entity_id: Annotated[str, Field(description="Full climate entity ID to change, e.g. 'climate.living_room'.")],
    hvac_mode: Annotated[str, Field(description="Target mode: one of heat, cool, auto, off, fan_only, dry.")],
) -> list[dict]:
    """Set a climate entity's heating/cooling mode.

    Calls `climate.set_hvac_mode` with `{"entity_id": entity_id, "hvac_mode":
    hvac_mode}` over HTTP.

    Use when: switching a thermostat between heat/cool/auto/off/fan_only/dry.
    Not for: the target temperature — use `entities_set_value`.
    Returns: the list of states changed by the call.
    """
    return ha.call_service("climate", "set_hvac_mode", {"entity_id": entity_id, "hvac_mode": hvac_mode})


@mcp.tool(annotations=write("Set light color and brightness", idempotent=True))
def set_light_color(
    entity_id: Annotated[str, Field(description="Full light entity ID to change, e.g. 'light.kitchen'.")],
    rgb_color: Annotated[
        list[int] | None, Field(description="Target RGB color as [r, g, b], each 0-255. Omit to leave color unchanged.")
    ] = None,
    color_temp: Annotated[
        int | None,
        Field(description="Deprecated mired color temperature. Converted to Kelvin; prefer color_temp_kelvin."),
    ] = None,
    color_temp_kelvin: Annotated[
        int | None, Field(description="Target color temperature in Kelvin. Omit to leave it unchanged.")
    ] = None,
    brightness: Annotated[
        int | None, Field(description="Target brightness, 0-255. Omit to leave brightness unchanged.")
    ] = None,
) -> list[dict]:
    """Set a light's color, color temperature and/or brightness in one call.

    Calls `light.turn_on` with only the given fields set (`rgb_color`,
    `color_temp_kelvin`, `brightness`); a given `color_temp` (mireds) is
    converted to Kelvin (`round(1_000_000 / mireds)`) first, since HA's
    `light.turn_on` no longer accepts mireds.

    Use when: adjusting a light's color/brightness, whether it is currently
    on or off — `light.turn_on` also turns it on.
    Not for: a plain on/off/toggle with no color change — use
    `entities_turn_on`, which supports the same brightness/color options for
    lights alongside generic on/off for any domain.
    Returns: the list of states changed by the call.
    """
    data: dict = {"entity_id": entity_id}
    if rgb_color:
        data["rgb_color"] = rgb_color
    if color_temp_kelvin is not None:
        data["color_temp_kelvin"] = color_temp_kelvin
    elif color_temp:
        data["color_temp_kelvin"] = round(1_000_000 / color_temp)
    if brightness is not None:
        data["brightness"] = brightness
    return ha.call_service("light", "turn_on", data)


@mcp.tool(annotations=write("Toggle media play/pause", idempotent=False))
def media_play_pause(
    entity_id: Annotated[
        str, Field(description="Full media_player entity ID to control, e.g. 'media_player.living_room'.")
    ],
) -> list[dict]:
    """Toggle a media player between playing and paused.

    Calls `media_player.media_play_pause` with `{"entity_id": entity_id}`
    over HTTP; the resulting state depends on the player's current state.

    Use when: a simple play/pause toggle is enough, without checking current
    state first.
    Returns: the list of states changed by the call.
    """
    return ha.call_service("media_player", "media_play_pause", {"entity_id": entity_id})


@mcp.tool(annotations=write("Seek media playback position", idempotent=True))
def media_seek(
    entity_id: Annotated[
        str, Field(description="Full media_player entity ID to seek, e.g. 'media_player.living_room'.")
    ],
    position: Annotated[
        float, Field(description="Target playback position in seconds from the start of the current media.")
    ],
) -> list[dict]:
    """Seek a media player to an absolute playback position.

    Calls `media_player.media_seek` with `{"entity_id": entity_id,
    "seek_position": position}` over HTTP.

    Use when: jumping to a specific point in the currently playing media.
    Returns: the list of states changed by the call.
    """
    return ha.call_service("media_player", "media_seek", {"entity_id": entity_id, "seek_position": position})


@mcp.tool(annotations=write("Set media player volume", idempotent=True))
def set_volume(
    entity_id: Annotated[
        str, Field(description="Full media_player entity ID to change, e.g. 'media_player.living_room'.")
    ],
    volume: Annotated[float, Field(description="Target volume level, 0.0 (silent) to 1.0 (maximum).")],
) -> list[dict]:
    """Set a media player's volume level.

    Calls `media_player.volume_set` with `{"entity_id": entity_id,
    "volume_level": volume}` over HTTP.

    Use when: setting an exact volume rather than stepping it up/down.
    Returns: the list of states changed by the call.
    """
    return ha.call_service("media_player", "volume_set", {"entity_id": entity_id, "volume_level": volume})


@mcp.tool(annotations=write("Send a notification", idempotent=False, open_world=True))
def send_notification(
    message: Annotated[str, Field(description="Notification body text to deliver.")],
    title: Annotated[str | None, Field(description="Optional notification title. Omit for no title.")] = None,
    target: Annotated[
        str | None,
        Field(
            description=(
                "Notify service to use, e.g. 'mobile_app_phone' or 'notify.mobile_app_phone'. "
                "Omit to use the default 'notify' service."
            )
        ),
    ] = None,
) -> list[dict]:
    """Send a notification through a Home Assistant notify service.

    Calls `notify.<target>` (or plain `notify.notify` when `target` is
    omitted) with `message` and optional `title`; `target` may include the
    domain (`notify.xyz`) or be a bare service name, but a dotted `target`
    must name the `notify` domain itself (ADR-0006 D6) — otherwise it would
    let any `domain.service` be reached under cover of a "notification".
    Each call dispatches a new notification to whatever device or channel
    that service delivers to, which can be outside the local network (e.g.
    a phone push).

    Use when: pushing a message to a person or device through a configured
    notify service.
    Not for: an in-UI banner tracked by an ID — use
    `services_notify_persistent_create`.
    Returns: the list of states changed by the call (notify services
    typically change none).
    Errors: `{"error": "invalid_notify_target", "message": ...}` when a
    dotted `target` names a domain other than `notify`; `{"error":
    "invalid_service_name"}` when the part after `notify.` is not itself a
    bare `[A-Za-z0-9_]+` service name (e.g. a second dot).
    """
    service = target or "notify"
    if "." in service:
        domain, svc = service.split(".", 1)
        if domain != "notify":
            return {
                "error": "invalid_notify_target",
                "message": (
                    f"target {target!r} names domain {domain!r}, not 'notify'. "
                    "Pass a bare service name or 'notify.<service>'."
                ),
            }
    else:
        domain, svc = "notify", service
    if not service_guard.validate_service_name(svc):
        return {"error": "invalid_service_name"}
    data: dict = {"message": message}
    if title:
        data["title"] = title
    return ha.call_service(domain, svc, data)


# --- Camera ---

@mcp.tool(annotations=destructive("Save a camera snapshot", idempotent=True))
def camera_snapshot(
    entity_id: Annotated[str, Field(description="Full camera entity ID to capture, e.g. 'camera.front_door'.")],
    filename: Annotated[
        str,
        Field(
            description=(
                "Destination path inside the HA config directory, e.g. "
                "'/config/www/snapshots/front.jpg'. Overwrites any existing file there."
            )
        ),
    ],
) -> dict:
    """Save a still snapshot from a camera entity to a file, overwriting any existing file at that path.

    Calls `camera.snapshot` with `{"entity_id": entity_id, "filename":
    filename}` over HTTP. `filename` must be inside the HA config directory
    and allow-listed via `allowlist_external_dirs`, typically under
    `/config/www/snapshots/...` so it can be served by HA.

    Use when: capturing a single still frame to a known path, e.g. for a
    dashboard image or Lovelace card.
    Not for: a video clip — use `services_camera_record`.
    Returns: dict with `entity_id`, `filename` and the raw service call
    result.
    """
    result = ha.call_service(
        "camera", "snapshot",
        {"entity_id": entity_id, "filename": filename},
    )
    return {"entity_id": entity_id, "filename": filename, "result": result}


@mcp.tool(annotations=destructive("Record a camera clip", idempotent=True))
def camera_record(
    entity_id: Annotated[str, Field(description="Full camera entity ID to record, e.g. 'camera.front_door'.")],
    filename: Annotated[
        str,
        Field(
            description=(
                "Destination path inside the HA config directory, e.g. "
                "'/config/www/snapshots/clip.mp4'. Overwrites any existing file there."
            )
        ),
    ],
    duration: Annotated[int, Field(description="Recording length in seconds.")] = 10,
) -> dict:
    """Record a short clip from a camera entity to a file, overwriting any existing file at that path.

    Calls `camera.record` with `{"entity_id": entity_id, "filename":
    filename, "duration": duration}` over HTTP.

    Use when: capturing a short video clip rather than a single still frame.
    Not for: a single still frame — use `services_camera_snapshot`.
    Returns: dict with `entity_id`, `filename`, `duration` and the raw
    service call result.
    Limits: blocks for roughly `duration` seconds while HA records.
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

@mcp.tool(annotations=write("Create a persistent notification", idempotent=False))
def notify_persistent_create(
    message: Annotated[str, Field(description="Notification body text to show in the HA UI.")],
    title: Annotated[str | None, Field(description="Optional notification title. Omit for no title.")] = None,
    notification_id: Annotated[
        str | None,
        Field(
            description=(
                "Stable ID for later dismissal via services_notify_persistent_dismiss. "
                "Omit to create a new, unaddressable notification each call."
            )
        ),
    ] = None,
) -> dict:
    """Create or replace a persistent notification banner in the Home Assistant UI.

    Calls `persistent_notification.create` with `message`, optional `title`
    and `notification_id`. Giving the same `notification_id` again replaces
    that notification's content in place; omitting it creates a new,
    separately addressable notification each time.

    Use when: showing a UI banner that a person can dismiss, as opposed to a
    push notification.
    Not for: a push notification to a device — use
    `services_send_notification`.
    Returns: dict with `notification_id` and the raw service call result.
    """
    data: dict = {"message": message}
    if title:
        data["title"] = title
    if notification_id:
        data["notification_id"] = notification_id
    result = ha.call_service("persistent_notification", "create", data)
    return {"notification_id": notification_id, "result": result}


@mcp.tool(annotations=write("Dismiss a persistent notification", idempotent=True))
def notify_persistent_dismiss(
    notification_id: Annotated[
        str,
        Field(description="ID of the persistent notification to dismiss, as passed to services_notify_persistent_create."),
    ],
) -> dict:
    """Dismiss one persistent notification by its ID.

    Calls `persistent_notification.dismiss` with `{"notification_id":
    notification_id}` over HTTP; dismissing an already-dismissed or unknown
    ID is a no-op from this tool's perspective.

    Use when: clearing one specific UI banner.
    Not for: clearing every banner — use
    `services_notify_persistent_dismiss_all`.
    Returns: dict with `notification_id` and the raw service call result.
    """
    result = ha.call_service(
        "persistent_notification", "dismiss",
        {"notification_id": notification_id},
    )
    return {"notification_id": notification_id, "result": result}


@mcp.tool(annotations=write("Dismiss all persistent notifications", idempotent=True))
def notify_persistent_dismiss_all() -> dict:
    """Dismiss every persistent notification currently shown in the Home Assistant UI.

    Calls `persistent_notification.dismiss_all` over HTTP with no data.

    Use when: clearing all UI banners at once.
    Not for: clearing a single banner — use
    `services_notify_persistent_dismiss`.
    Returns: dict with the raw service call result.
    """
    result = ha.call_service("persistent_notification", "dismiss_all")
    return {"result": result}
