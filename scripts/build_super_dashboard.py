"""Replace lukasz3 view with a comprehensive super-dashboard.

10 sections, sections layout, 4 max columns:
1. Dom & Wejście     6. Energia & Solar
2. Osoby & Pogoda    7. Ogród (podlewanie + kosiarka)
3. Światła wewn.     8. Sprzątanie (odkurzacze)
4. Światła zewn.     9. Multimedia
5. Klimat (AC)       10. System & Baterie
"""
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


def tile(entity, name, icon=None, features=None, color=None):
    c = {"type": "tile", "entity": entity, "name": name}
    if icon:
        c["icon"] = icon
    if features:
        c["features"] = features
    if color:
        c["color"] = color
    return c


def heading(text, icon=None, level=2):
    h = {"type": "heading", "heading": text, "heading_style": "title"}
    if icon:
        h["icon"] = icon
    return h


def conditional(entity, state, card):
    return {
        "type": "conditional",
        "conditions": [{"condition": "state", "entity": entity, "state": state}],
        "card": card,
    }


# ---------------- Sections ----------------

SEC_DOM_WEJSCIE = {
    "cards": [
        heading("Dom & Wejście", "mdi:home"),
        tile("lock.domek", "Zamek", "mdi:door-closed-lock",
             [{"type": "lock-commands"}]),
        tile("cover.entry_gate_controller", "Brama wjazdowa", "mdi:gate",
             [{"type": "cover-open-close"}, {"type": "cover-position"}]),
        tile("cover.supla_supla_cc2f6365_a174_ffb9_81d1_cd587fe2d8c9_0",
             "Garaż", "mdi:garage-variant",
             [{"type": "cover-open-close"}]),
        tile("cover.zamel_sbw_02_garaz", "Garaż (czujnik)", "mdi:door"),
        conditional("binary_sensor.domek_semi_locked", "on",
                    tile("binary_sensor.domek_semi_locked",
                         "⚠️ Zamek nie domknięty", "mdi:lock-alert")),
    ],
}

SEC_OSOBY_POGODA = {
    "cards": [
        heading("Osoby & Pogoda", "mdi:account-multiple"),
        tile("person.fistach", "Łukasz"),
        tile("person.tata", "Tata"),
        tile("person.tats", "Tats"),
        {"type": "weather-forecast", "entity": "weather.dom", "show_forecast": True},
    ],
}

SEC_KLIMAT = {
    "cards": [
        heading("Klimatyzacja", "mdi:air-conditioner"),
        tile("climate.ac_salon_2", "Salon", "mdi:sofa-outline",
             [{"type": "climate-hvac-modes"}, {"type": "target-temperature"}]),
        tile("climate.ac_gabinet", "Gabinet", "mdi:desk",
             [{"type": "climate-hvac-modes"}, {"type": "target-temperature"}]),
        tile("climate.ac_sypialnia", "Sypialnia", "mdi:bed",
             [{"type": "climate-hvac-modes"}, {"type": "target-temperature"}]),
        tile("climate.ac_poklewy_ac_poklewy", "Jacek", "mdi:bed-single",
             [{"type": "climate-hvac-modes"}, {"type": "target-temperature"}]),
        tile("climate.ac_pokprawy_ac_pokprawy_2", "Agata", "mdi:bed-single",
             [{"type": "climate-hvac-modes"}, {"type": "target-temperature"}]),
    ],
}

# Indoor lights — split by area
SEC_SWIATLA_WEWN = {
    "cards": [
        heading("Światła wewnętrzne", "mdi:lightbulb-group"),
        # Salon / Jadalnia / Kuchnia
        tile("switch.0xa4c138415f370e08_l1", "Jadalnia 1", "mdi:lightbulb"),
        tile("switch.0xa4c138415f370e08_l2", "Jadalnia 2", "mdi:lightbulb"),
        tile("switch.0xa4c138912bce2216_l1", "Kuchnia 1", "mdi:lightbulb"),
        tile("switch.0xa4c138912bce2216_l2", "Kuchnia 2", "mdi:lightbulb"),
        tile("switch.0xa4c138dbf2831188_l1", "Barek 1", "mdi:lightbulb"),
        tile("switch.0xa4c138dbf2831188_l2", "Barek 2", "mdi:lightbulb"),
        # Korytarz / Klatka
        tile("switch.0xa4c13873bc8bc122_l1", "Korytarz 1", "mdi:lightbulb"),
        tile("switch.0xa4c13873bc8bc122_l2", "Korytarz 2", "mdi:lightbulb"),
        tile("switch.0xa4c1386dfe0ca5e0", "Korytarz góra", "mdi:lightbulb"),
        tile("switch.0xa4c138424fb496a2_l1", "Klatka 1", "mdi:stairs"),
        tile("switch.0xa4c138424fb496a2_l2", "Klatka 2", "mdi:stairs"),
        # Łazienka
        tile("switch.0xa4c1383245066715_l1", "Łazienka 1", "mdi:lightbulb"),
        tile("switch.0xa4c1383245066715_l2", "Łazienka 2", "mdi:lightbulb"),
        tile("switch.0xa4c138db76644bff", "Łazienka lustro", "mdi:mirror"),
        # Pokoje
        tile("switch.swiatlo_gabinet", "Gabinet", "mdi:floor-lamp"),
        tile("switch.0xa4c138bbce7a88be_l1", "Agata 1", "mdi:lightbulb"),
        tile("switch.0xa4c138bbce7a88be_l2", "Agata 2", "mdi:lightbulb"),
        tile("switch.0xa4c138434a4b55cd_l1", "Jacek 1", "mdi:lightbulb"),
        tile("switch.0xa4c138434a4b55cd_l2", "Jacek 2", "mdi:lightbulb"),
    ],
}

SEC_SWIATLA_ZEWN = {
    "cards": [
        heading("Światła zewnętrzne & garaż", "mdi:outdoor-lamp"),
        tile("switch.0xa4c13811c6049f4d", "Wiatrołap", "mdi:home-import-outline"),
        tile("switch.0xa4c1381cbacf2ece", "Dwór tył", "mdi:tree"),
        tile("switch.0xa4c138046ebd3475_l1", "Dwór przód 1", "mdi:lightbulb-outline"),
        tile("switch.0xa4c138046ebd3475_l2", "Dwór przód 2", "mdi:lightbulb-outline"),
        tile("switch.0xa4c13830c1ed52a5", "Garaż światło", "mdi:garage-variant"),
    ],
}

# Rolety with group controls
ROLETY_ALL = ["cover.roleta", "cover.roleta_2", "cover.roleta_3", "cover.roleta_4"]

def cover_button(name, icon, service):
    return {
        "type": "button",
        "name": name,
        "icon": icon,
        "tap_action": {
            "action": "perform-action",
            "perform_action": service,
            "target": {"entity_id": ROLETY_ALL},
        },
    }

SEC_ROLETY = {
    "cards": [
        heading("Rolety salonu", "mdi:blinds-horizontal"),
        {"type": "horizontal-stack", "cards": [
            cover_button("Góra", "mdi:arrow-up-box", "cover.open_cover"),
            cover_button("Tilt", "mdi:blinds", "cover.open_cover_tilt"),
            cover_button("Dół", "mdi:arrow-down-box", "cover.close_cover"),
            cover_button("Stop", "mdi:stop-circle-outline", "cover.stop_cover"),
        ]},
        tile("cover.roleta", "Roleta 1", None,
             [{"type": "cover-open-close"}, {"type": "cover-tilt"}]),
        tile("cover.roleta_2", "Roleta 2", None,
             [{"type": "cover-open-close"}, {"type": "cover-tilt"}]),
        tile("cover.roleta_3", "Roleta 3", None,
             [{"type": "cover-open-close"}, {"type": "cover-tilt"}]),
        tile("cover.roleta_4", "Roleta 4", None,
             [{"type": "cover-open-close"}, {"type": "cover-tilt"}]),
        tile("cover.sypialnia", "Zasłona Sypialnia", "mdi:curtains",
             [{"type": "cover-open-close"}]),
    ],
}

SEC_ENERGIA = {
    "cards": [
        heading("Energia & Solar", "mdi:solar-power"),
        {"type": "gauge", "entity": "sensor.daily_yield",
         "name": "Produkcja dzisiaj", "unit": "kWh",
         "min": 0, "max": 50, "needle": True,
         "segments": [
             {"from": 0, "color": "#db4437"},
             {"from": 5, "color": "#f4b400"},
             {"from": 15, "color": "#0f9d58"},
         ]},
        tile("sensor.total_yield", "Suma produkcji", "mdi:counter"),
        tile("sensor.inverter_pv_connection_status", "Status PV", "mdi:transmission-tower"),
        tile("sensor.inverter_off_grid_status", "Sieć", "mdi:transmission-tower-export"),
    ],
}

SEC_OGROD = {
    "cards": [
        heading("Ogród — podlewanie", "mdi:sprinkler"),
        tile("select.domek15podlewanie_device_mode", "Tryb", "mdi:cog"),
        tile("switch.domek15podlewanie_rain_delay", "Opóźnienie deszczowe",
             "mdi:weather-rainy"),
        conditional("binary_sensor.domek15podlewanie_fault", "on",
                    tile("binary_sensor.domek15podlewanie_fault",
                         "⚠️ Błąd podlewania", "mdi:alert")),
        tile("valve.domek15podlewanie_zone_1_zone", "Strefa 1", "mdi:sprinkler-variant"),
        tile("valve.domek15podlewanie_zone_2_zone", "Strefa 2", "mdi:sprinkler-variant"),
        tile("valve.domek15podlewanie_zone_3_zone", "Strefa 3", "mdi:sprinkler-variant"),
        tile("switch.domek15podlewanie_podlewanie_program_2",
             "Program podlewania", "mdi:sprinkler"),
        tile("switch.domek15podlewanie_trawa_lato_program",
             "Trawa lato", "mdi:grass"),
        heading("Kosiarka", "mdi:robot-mower"),
        tile("lawn_mower.automower_automower", "Automower", "mdi:robot-mower",
             [{"type": "lawn-mower-commands",
               "commands": ["start_pause", "dock"]}]),
        tile("sensor.automower_mower_status", "Status", "mdi:information"),
        tile("sensor.automower_mower_battery_charge", "Bateria", "mdi:battery"),
    ],
}

SEC_SPRZATANIE = {
    "cards": [
        heading("Sprzątanie", "mdi:robot-vacuum"),
        tile("vacuum.domek15", "Roborock", "mdi:robot-vacuum"),
        tile("vacuum.domek15_2", "Dreame (z mopem)", "mdi:robot-vacuum-variant"),
        tile("sensor.domek15_battery_level", "Bateria Dreame", "mdi:battery"),
        tile("binary_sensor.domek15_water_box_attached", "Zbiornik wody", "mdi:water"),
        tile("binary_sensor.domek15_mop_attached", "Mop", "mdi:square-rounded"),
        conditional("binary_sensor.domek15_water_shortage", "on",
                    tile("binary_sensor.domek15_water_shortage",
                         "⚠️ Brak wody", "mdi:water-alert")),
    ],
}

SEC_MULTIMEDIA = {
    "cards": [
        heading("Multimedia", "mdi:television"),
        tile("media_player.samsung_q77ca_55_2", "TV Salon", "mdi:television"),
        tile("media_player.tv_15", "TV 15", "mdi:television"),
        tile("media_player.soundbar", "Soundbar", "mdi:speaker"),
        tile("media_player.vlc_telnet", "VLC", "mdi:vlc"),
    ],
}

SEC_SYSTEM = {
    "cards": [
        heading("System & status", "mdi:gauge"),
        {"type": "gauge", "entity": "sensor.level_sensor_water_level",
         "name": "Poziom soli", "unit": "%",
         "min": 0, "max": 100, "needle": True,
         "segments": [
             {"from": 0, "color": "#db4437"},
             {"from": 20, "color": "#f4b400"},
             {"from": 50, "color": "#0f9d58"},
         ]},
        tile("switch.bieznia_socket", "Bieżnia", "mdi:run"),
        tile("sensor.domek_bateria", "Bateria zamka", "mdi:battery"),
        heading("Telefony — bateria"),
        tile("sensor.sm_s921b_battery_level", "S921B", "mdi:cellphone"),
        tile("sensor.sm_a536b_battery_level", "A536B", "mdi:cellphone"),
        tile("sensor.sm_s931b_battery_level", "S931B", "mdi:cellphone"),
        tile("sensor.fios26_battery_level", "Fios26", "mdi:cellphone"),
        heading("Odpady", "mdi:trash-can"),
        {"type": "entities", "entities": [
            {"entity": "sensor.next_garbage_collection", "name": "Najbliższy wywóz"},
            {"type": "divider"},
            {"type": "attribute", "entity": "sensor.next_garbage_collection",
             "attribute": "Odpady zmieszane", "name": "Zmieszane"},
            {"type": "attribute", "entity": "sensor.next_garbage_collection",
             "attribute": "Metale i tworzywa sztuczne", "name": "Plastik/metal"},
            {"type": "attribute", "entity": "sensor.next_garbage_collection",
             "attribute": "Papier", "name": "Papier"},
            {"type": "attribute", "entity": "sensor.next_garbage_collection",
             "attribute": "Szkło", "name": "Szkło"},
            {"type": "attribute", "entity": "sensor.next_garbage_collection",
             "attribute": "Bio", "name": "Bio"},
        ]},
    ],
}


SECTIONS = [
    SEC_DOM_WEJSCIE,
    SEC_OSOBY_POGODA,
    SEC_KLIMAT,
    SEC_SWIATLA_WEWN,
    SEC_SWIATLA_ZEWN,
    SEC_ROLETY,
    SEC_ENERGIA,
    SEC_OGROD,
    SEC_SPRZATANIE,
    SEC_MULTIMEDIA,
    SEC_SYSTEM,
]


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
                       "clientInfo": {"name": "super-dash", "version": "1.0"}},
        })
        session_id = r.headers.get("mcp-session-id")
        h = {**headers}
        if session_id:
            h["mcp-session-id"] = session_id
        client.post(url, headers=h, json={"jsonrpc": "2.0",
                                          "method": "notifications/initialized"})

        r = client.post(url, headers=h, json={
            "jsonrpc": "2.0", "method": "tools/call", "id": 2,
            "params": {"name": "dashboards_get_dashboard_config", "arguments": {}},
        })
        config = json.loads(parse_response(r)["result"]["content"][0]["text"])

        # Find lukasz3 and replace its sections
        for view in config["views"]:
            if view.get("path") != "lukasz3":
                continue
            # keep title, path, theme, type, max_columns, visible
            view["type"] = "sections"
            view["max_columns"] = 4
            view["sections"] = SECTIONS
            view["cards"] = []
            print(f"Replaced lukasz3 with {len(SECTIONS)} sections")

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
