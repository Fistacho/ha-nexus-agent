"""One-shot: fix Ogród dead entities + enrich lukasz3 view.

Calls Nexus MCP over HTTP (Streamable HTTP transport) so the large
dashboard config stays server-side and never enters Claude's context.
"""
import json
import httpx

NEXUS_URL = "http://192.168.8.141:7123/mcp"
TOKEN = "2HGEnLCbhl9K5mhDJjuopj39iXmpJzwr_5DytMQzuYI"


def parse_response(r: httpx.Response) -> dict:
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
        # 1) initialize
        r = client.post(url, headers=headers, json={
            "jsonrpc": "2.0", "method": "initialize", "id": 1,
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "claude-updater", "version": "1.0"},
            },
        })
        r.raise_for_status()
        session_id = r.headers.get("mcp-session-id")
        init_resp = parse_response(r)
        print("Init:", init_resp.get("result", {}).get("serverInfo"))

        h = {**headers}
        if session_id:
            h["mcp-session-id"] = session_id

        # notify initialized
        client.post(url, headers=h, json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })

        # 2) fetch config
        r = client.post(url, headers=h, json={
            "jsonrpc": "2.0", "method": "tools/call", "id": 2,
            "params": {
                "name": "dashboards_get_dashboard_config",
                "arguments": {},
            },
        })
        r.raise_for_status()
        resp = parse_response(r)
        content = resp["result"]["content"]
        if isinstance(content, list):
            text = content[0]["text"]
            config = json.loads(text)
        else:
            config = content

        print(f"Got {len(config.get('views', []))} views")

        # 3) modify
        modified = []

        ENTITY_MAP = {
            "switch.zone_1_zone": "valve.domek15podlewanie_zone_1_zone",
            "switch.zone_2_zone": "valve.domek15podlewanie_zone_2_zone",
            "switch.zone_3_zone": "valve.domek15podlewanie_zone_3_zone",
            "switch.domek15podlewanie_podlewanie_program":
                "switch.domek15podlewanie_podlewanie_program_2",
        }

        for view in config["views"]:
            path = view.get("path")

            # --- fix ogrod ---
            if path == "ogrod":
                changes = 0
                for section in view.get("sections", []):
                    for card in section.get("cards", []):
                        eid = card.get("entity")
                        if eid in ENTITY_MAP:
                            card["entity"] = ENTITY_MAP[eid]
                            changes += 1
                if changes:
                    modified.append(f"ogrod: {changes} entity swaps")

            # --- enrich lukasz3 ---
            if path == "lukasz3":
                # add battery + alerts to Wejście
                for section in view.get("sections", []):
                    cards = section.get("cards", [])
                    if cards and cards[0].get("heading") == "Wejście":
                        has_battery = any(
                            c.get("entity") == "sensor.domek_bateria" for c in cards
                        )
                        if not has_battery:
                            for idx, c in enumerate(cards):
                                if c.get("entity") == "lock.domek":
                                    cards.insert(idx + 1, {
                                        "type": "tile",
                                        "entity": "sensor.domek_bateria",
                                        "name": "Bateria zamka",
                                        "icon": "mdi:battery-60",
                                    })
                                    cards.insert(idx + 2, {
                                        "type": "conditional",
                                        "conditions": [{
                                            "condition": "state",
                                            "entity": "binary_sensor.domek_semi_locked",
                                            "state": "on",
                                        }],
                                        "card": {
                                            "type": "tile",
                                            "entity": "binary_sensor.domek_semi_locked",
                                            "name": "⚠️ Zamek szarpnięty",
                                            "icon": "mdi:lock-alert",
                                        },
                                    })
                                    cards.insert(idx + 3, {
                                        "type": "conditional",
                                        "conditions": [{
                                            "condition": "state",
                                            "entity": "binary_sensor.domek_ladowanie",
                                            "state": "on",
                                        }],
                                        "card": {
                                            "type": "tile",
                                            "entity": "binary_sensor.domek_ladowanie",
                                            "name": "🔌 Ładowanie",
                                            "icon": "mdi:battery-charging",
                                        },
                                    })
                                    modified.append("lukasz3: Wejście + bateria/alerts")
                                    break

                # add Podlewanie section
                has_podlewanie = any(
                    s.get("cards") and s["cards"][0].get("heading") == "Podlewanie"
                    for s in view.get("sections", [])
                )
                if not has_podlewanie:
                    view.setdefault("sections", []).append({
                        "cards": [
                            {"type": "heading", "heading": "Podlewanie", "icon": "mdi:sprinkler"},
                            {"type": "tile",
                             "entity": "select.domek15podlewanie_device_mode",
                             "name": "Tryb", "icon": "mdi:cog"},
                            {"type": "tile",
                             "entity": "switch.domek15podlewanie_rain_delay",
                             "name": "Opóźnienie deszczowe", "icon": "mdi:weather-rainy"},
                            {"type": "conditional",
                             "conditions": [{
                                 "condition": "state",
                                 "entity": "binary_sensor.domek15podlewanie_fault",
                                 "state": "on",
                             }],
                             "card": {
                                 "type": "tile",
                                 "entity": "binary_sensor.domek15podlewanie_fault",
                                 "name": "⚠️ Błąd podlewania", "icon": "mdi:alert",
                             }},
                            {"type": "tile",
                             "entity": "valve.domek15podlewanie_zone_1_zone",
                             "name": "Strefa 1", "icon": "mdi:sprinkler-variant"},
                            {"type": "tile",
                             "entity": "valve.domek15podlewanie_zone_2_zone",
                             "name": "Strefa 2", "icon": "mdi:sprinkler-variant"},
                            {"type": "tile",
                             "entity": "valve.domek15podlewanie_zone_3_zone",
                             "name": "Strefa 3", "icon": "mdi:sprinkler-variant"},
                            {"type": "tile",
                             "entity": "switch.domek15podlewanie_podlewanie_program_2",
                             "name": "Program podlewania", "icon": "mdi:sprinkler"},
                            {"type": "tile",
                             "entity": "switch.domek15podlewanie_trawa_lato_program",
                             "name": "Trawa lato", "icon": "mdi:grass"},
                        ],
                    })
                    modified.append("lukasz3: +Podlewanie section")

        if not modified:
            print("No changes needed — all already applied.")
            return

        print("Changes:")
        for m in modified:
            print("  -", m)

        # 4) save
        r = client.post(url, headers=h, json={
            "jsonrpc": "2.0", "method": "tools/call", "id": 3,
            "params": {
                "name": "dashboards_save_dashboard_config",
                "arguments": {"config": config},
            },
        })
        r.raise_for_status()
        resp = parse_response(r)
        result = resp.get("result", resp)
        print("Save:", result)


if __name__ == "__main__":
    main()
