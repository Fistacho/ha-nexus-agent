"""Remove 'Zamek — szczegóły' section, keep only battery tile at the very end."""
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
        raise RuntimeError(f"No data in SSE: {r.text[:300]}")
    return r.json()


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
                "clientInfo": {"name": "claude-simplify", "version": "1.0"},
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

        for view in config["views"]:
            if view.get("path") != "lukasz3":
                continue

            sections = view.get("sections", [])

            # 1) drop "Zamek — szczegóły" section entirely
            sections = [
                s for s in sections
                if not (s.get("cards") and s["cards"][0].get("heading") == "Zamek — szczegóły")
            ]

            # 2) drop existing battery/ładowanie/semi_locked from any section (cleanup)
            remove_entities = {
                "sensor.domek_bateria",
                "binary_sensor.domek_semi_locked",
                "binary_sensor.domek_ladowanie",
                "binary_sensor.domek_pullspring_enabled",
                "sensor.domek_pullspring_duration",
            }
            for section in sections:
                section["cards"] = [
                    c for c in section.get("cards", [])
                    if not (
                        c.get("entity") in remove_entities
                        or (
                            c.get("type") == "conditional"
                            and c.get("conditions", [{}])[0].get("entity") in remove_entities
                        )
                    )
                ]

            # 3) drop any existing "Zamek – status" to avoid duplicates on re-run
            sections = [
                s for s in sections
                if not (s.get("cards") and s["cards"][0].get("heading") == "Zamek – status")
            ]

            # 4) append a tiny final section with just the battery tile
            sections.append({
                "cards": [
                    {"type": "heading", "heading": "Zamek – status", "icon": "mdi:lock"},
                    {
                        "type": "tile",
                        "entity": "sensor.domek_bateria",
                        "name": "Bateria zamka",
                        "icon": "mdi:battery",
                    },
                ],
            })

            view["sections"] = sections
            print(f"lukasz3 now has {len(sections)} sections")

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
