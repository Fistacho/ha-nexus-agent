"""Fix rolety view: set each button's default icon to match its action,
so the icon stays correct when entity state is 'unknown' (intentional in
netatmo_bubendorff integration to keep all buttons active)."""
import json
import httpx

NEXUS_URL = "http://192.168.8.141:7123/mcp"
TOKEN = "2HGEnLCbhl9K5mhDJjuopj39iXmpJzwr_5DytMQzuYI"

# Map: tap_action.service -> correct default icon
SERVICE_ICON = {
    "cover.open_cover": "mdi:arrow-up",
    "cover.close_cover": "mdi:arrow-down",
    "cover.open_cover_tilt": "mdi:arrow-top-right",
    "cover.stop_cover": "mdi:square",
}


def parse_response(r):
    ct = r.headers.get("content-type", "")
    if "text/event-stream" in ct:
        for line in r.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        raise RuntimeError(f"No data in SSE: {r.text[:300]}")
    return r.json()


def fix_card(card, count):
    """Recursively walk cards and fix custom:button-card icons."""
    if not isinstance(card, dict):
        return count

    if card.get("type") == "custom:button-card":
        tap = card.get("tap_action", {})
        service = tap.get("service")
        if service in SERVICE_ICON:
            new_icon = SERVICE_ICON[service]
            old_icon = card.get("icon")
            if old_icon != new_icon:
                card["icon"] = new_icon
                count += 1
            # Also fix per-state overrides if their icons are wrong
            # (the action determines the visual, not the entity state)
            for state in card.get("state", []):
                if state.get("icon") != new_icon:
                    state["icon"] = new_icon
                    count += 1

    # Recurse into nested cards
    for child in card.get("cards", []):
        count = fix_card(child, count)
    return count


def main():
    url = f"{NEXUS_URL}?token={TOKEN}"
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=120) as client:
        r = client.post(url, headers=headers, json={
            "jsonrpc": "2.0", "method": "initialize", "id": 1,
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "claude-rolety-fix", "version": "1.0"},
            },
        })
        r.raise_for_status()
        session_id = r.headers.get("mcp-session-id")
        h = {**headers}
        if session_id:
            h["mcp-session-id"] = session_id

        client.post(url, headers=h, json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })

        r = client.post(url, headers=h, json={
            "jsonrpc": "2.0", "method": "tools/call", "id": 2,
            "params": {"name": "dashboards_get_dashboard_config", "arguments": {}},
        })
        resp = parse_response(r)
        content = resp["result"]["content"]
        config = json.loads(content[0]["text"]) if isinstance(content, list) else content

        total = 0
        for view in config["views"]:
            if view.get("path") != "rolety":
                continue
            for card in view.get("cards", []):
                total = fix_card(card, total)

        print(f"Fixed {total} icons")
        if total == 0:
            print("Nothing to change")
            return

        r = client.post(url, headers=h, json={
            "jsonrpc": "2.0", "method": "tools/call", "id": 3,
            "params": {
                "name": "dashboards_save_dashboard_config",
                "arguments": {"config": config},
            },
        })
        r.raise_for_status()
        print("Save:", parse_response(r).get("result", {}).get("structuredContent"))


if __name__ == "__main__":
    main()
