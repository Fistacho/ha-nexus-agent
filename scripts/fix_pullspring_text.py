"""Reword pullspring markdown to use Polish terminology (odciąganie)."""
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
                "clientInfo": {"name": "claude-reword", "version": "1.0"},
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

        changed = False
        for view in config["views"]:
            if view.get("path") != "lukasz3":
                continue
            for section in view.get("sections", []):
                for card in section.get("cards", []):
                    # rename tile names (Pullspring -> Odciąganie)
                    if card.get("entity") == "binary_sensor.domek_pullspring_enabled" and card.get("name") == "Pullspring":
                        card["name"] = "Odciąganie zapadki"
                        changed = True
                    if card.get("entity") == "sensor.domek_pullspring_duration" and card.get("name") == "Czas pullspring":
                        card["name"] = "Czas odciągania"
                        changed = True
                    # fix markdown text
                    if card.get("type") == "conditional":
                        inner = card.get("card", {})
                        if inner.get("type") == "markdown" and "Pullspring wyłączony" in inner.get("content", ""):
                            inner["content"] = (
                                "⚠️ **Odciąganie zapadki wyłączone** — po unlocku drzwi pozostaną "
                                "tylko odblokowane (musisz nacisnąć klamkę). Usługa `lock.open` "
                                "(auto-uchylenie) nie zadziała w automatyzacjach. Włącz w aplikacji "
                                "Tedee: *Ustawienia → domek → Auto Open / Pullspring*."
                            )
                            changed = True

        if not changed:
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
