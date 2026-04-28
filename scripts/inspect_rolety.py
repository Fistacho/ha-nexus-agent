"""Inspect rolety view button icons."""
import json
import httpx

NEXUS_URL = "http://192.168.8.141:7123/mcp"
TOKEN = "2HGEnLCbhl9K5mhDJjuopj39iXmpJzwr_5DytMQzuYI"


def parse_response(r):
    ct = r.headers.get("content-type", "")
    if "text/event-stream" in ct:
        for line in r.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
    return r.json()


def walk(card, indent=0, idx=""):
    if not isinstance(card, dict):
        return
    t = card.get("type", "?")
    name = card.get("name", "")
    icon = card.get("icon", "")
    service = card.get("tap_action", {}).get("service", "")
    print(f"{'  ' * indent}{idx} {t} name={name!r} icon={icon!r} svc={service!r}")
    for i, child in enumerate(card.get("cards", [])):
        walk(child, indent + 1, f"[{i}]")


def main():
    url = f"{NEXUS_URL}?token={TOKEN}"
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=120) as client:
        r = client.post(url, headers=headers, json={
            "jsonrpc": "2.0", "method": "initialize", "id": 1,
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "inspect", "version": "1.0"}},
        })
        session_id = r.headers.get("mcp-session-id")
        h = {**headers}
        if session_id:
            h["mcp-session-id"] = session_id
        client.post(url, headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})

        r = client.post(url, headers=h, json={
            "jsonrpc": "2.0", "method": "tools/call", "id": 2,
            "params": {"name": "dashboards_get_dashboard_config", "arguments": {}},
        })
        config = json.loads(parse_response(r)["result"]["content"][0]["text"])

        for view in config["views"]:
            if view.get("path") != "rolety":
                continue
            print(f"=== view {view['title']!r} has {len(view['cards'])} top cards ===")
            for i, card in enumerate(view.get("cards", [])):
                print(f"\n--- top[{i}] ---")
                walk(card, 0)


if __name__ == "__main__":
    main()
