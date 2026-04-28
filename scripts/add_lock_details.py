"""Add detailed Tedee lock section to lukasz3 view."""
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
        r = client.post(url, headers=headers, json={
            "jsonrpc": "2.0", "method": "initialize", "id": 1,
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "claude-lock-detail", "version": "1.0"},
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

        # fetch
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
        config = json.loads(content[0]["text"]) if isinstance(content, list) else content

        # modify
        changed = False
        for view in config["views"]:
            if view.get("path") != "lukasz3":
                continue

            # Add new section "Zamek — szczegóły" after Wejście
            has_lock_details = any(
                s.get("cards") and s["cards"][0].get("heading") == "Zamek — szczegóły"
                for s in view.get("sections", [])
            )
            if has_lock_details:
                print("Lock details section already exists")
                continue

            new_section = {
                "cards": [
                    {"type": "heading", "heading": "Zamek — szczegóły", "icon": "mdi:lock-smart"},
                    {"type": "tile",
                     "entity": "sensor.domek_bateria",
                     "name": "Bateria",
                     "icon": "mdi:battery-60"},
                    {"type": "tile",
                     "entity": "binary_sensor.domek_pullspring_enabled",
                     "name": "Pullspring",
                     "icon": "mdi:spring"},
                    {"type": "tile",
                     "entity": "sensor.domek_pullspring_duration",
                     "name": "Czas pullspring",
                     "icon": "mdi:timer-outline"},
                    {"type": "conditional",
                     "conditions": [{
                         "condition": "state",
                         "entity": "binary_sensor.domek_pullspring_enabled",
                         "state": "off",
                     }],
                     "card": {
                         "type": "markdown",
                         "content": (
                             "⚠️ **Pullspring wyłączony** — usługa `lock.open` "
                             "nie zadziała w automatyzacjach. Włącz w aplikacji "
                             "Tedee: *Ustawienia → domek → Pullspring*."
                         ),
                     }},
                    {"type": "conditional",
                     "conditions": [{
                         "condition": "state",
                         "entity": "binary_sensor.domek_semi_locked",
                         "state": "on",
                     }],
                     "card": {
                         "type": "tile",
                         "entity": "binary_sensor.domek_semi_locked",
                         "name": "⚠️ Zamek nie domknięty",
                         "icon": "mdi:lock-alert",
                     }},
                    {"type": "conditional",
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
                     }},
                ],
            }

            # Remove the battery/semi_locked/ladowanie cards from Wejście (moved to new section)
            for section in view.get("sections", []):
                cards = section.get("cards", [])
                if cards and cards[0].get("heading") == "Wejście":
                    to_remove_entities = {
                        "sensor.domek_bateria",
                        "binary_sensor.domek_semi_locked",
                        "binary_sensor.domek_ladowanie",
                    }
                    section["cards"] = [
                        c for c in cards
                        if not (
                            c.get("entity") in to_remove_entities
                            or (
                                c.get("type") == "conditional"
                                and c.get("conditions", [{}])[0].get("entity") in to_remove_entities
                            )
                        )
                    ]

            # Insert new section right after Wejście (index 1)
            sections = view.get("sections", [])
            insert_at = 1
            for i, s in enumerate(sections):
                if s.get("cards") and s["cards"][0].get("heading") == "Wejście":
                    insert_at = i + 1
                    break
            sections.insert(insert_at, new_section)
            view["sections"] = sections
            changed = True
            print("Added 'Zamek — szczegóły' section")

        if not changed:
            print("Nothing to do")
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
