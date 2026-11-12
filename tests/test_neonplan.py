"""`tools/neonplan.py` — NeonPlan 3D tools against a fake `neonplan3d/*`
WebSocket API (`ha._ws_call` / `ha._ws_call_timeout` replaced, the pattern of
`test_areas_update.py`), answering like integration v3.0.6 (`websocket.py`):
`building/get` → `{building, revision, plan, version}`, `building/save` →
`{revision}` without any revision check, `history/*`, `image/*`, errors as
`HAWebSocketError` with HA's codes (`unknown_command`, `not_loaded`,
`not_found`, `invalid_format`, `unauthorized`).

Covered: status / not installed, reads, validation (structure, references,
HA areas and entities, auto-bind warnings), the save guards (overwrite,
expected_revision, restore point before the save, revision moved meanwhile,
server schema refusal, version gate), JSON Patch, restore points, background
image upload limits, binding suggestions.
"""
from __future__ import annotations

import base64
import copy

import pytest

import ha_client as ha
from tools import neonplan


def _unwrap(tool):
    return getattr(tool, "fn", tool)


get_status = _unwrap(neonplan.get_status)
list_plans = _unwrap(neonplan.list_plans)
get_plan = _unwrap(neonplan.get_plan)
validate_plan = _unwrap(neonplan.validate_plan)
list_restore_points = _unwrap(neonplan.list_restore_points)
suggest_bindings = _unwrap(neonplan.suggest_bindings)
save_plan = _unwrap(neonplan.save_plan)
patch_plan = _unwrap(neonplan.patch_plan)
create_restore_point = _unwrap(neonplan.create_restore_point)
restore_restore_point = _unwrap(neonplan.restore_restore_point)
upload_background_image = _unwrap(neonplan.upload_background_image)

_EMPTY = {
    "version": 1,
    "floors": [],
    "settings": {"wall_exterior": 0.24, "wall_interior": 0.12, "grid": 0.05},
    "energy": {},
    "presence": [],
}


def _building() -> dict:
    return {
        "version": 1,
        "settings": {"wall_exterior": 0.3, "wall_interior": 0.12, "grid": 0.05},
        "energy": {},
        "presence": [],
        "floors": [
            {
                "id": "floor_parter",
                "name": "Parter",
                "elevation": 0,
                "height": 2.6,
                "cut_height": 1.15,
                "background": None,
                "rooms": [
                    {
                        "id": "room_salon",
                        "name": "Salon",
                        "area_id": "salon",
                        "points": [[0, 0], [6, 0], [6, 4.5], [0, 4.5]],
                        "floor_material": "oak",
                        "climate": {"temperature": "sensor.salon_temp", "humidity": "none", "co2": "none"},
                    },
                    {
                        "id": "room_kuchnia",
                        "name": "Kuchnia",
                        "area_id": "kuchnia",
                        "points": [[6, 0], [10, 0], [10, 4.5], [6, 4.5]],
                        "floor_material": "tiles",
                    },
                ],
                "openings": [
                    {
                        "id": "win_salon",
                        "room_id": "room_salon",
                        "edge": 0,
                        "offset": 3.0,
                        "width": 1.5,
                        "type": "window",
                        "sill": 0.9,
                        "height": 1.4,
                        "cover": "cover.roleta_salon",
                        "contact": "none",
                    },
                    {
                        "id": "door_kuchnia",
                        "room_id": "room_kuchnia",
                        "edge": 3,
                        "offset": 2.0,
                        "width": 0.9,
                        "type": "door",
                        "sill": 0,
                        "height": 2.05,
                        "cover": "none",
                        "contact": "none",
                    },
                ],
                "furniture": [
                    {
                        "id": "lamp_salon",
                        "type": "lamp_ceiling",
                        "x": 3,
                        "z": 2,
                        "rotation": 0,
                        "w": 0.4,
                        "d": 0.4,
                        "h": 0.08,
                        "variant": None,
                        "entity": "light.salon",
                    }
                ],
                "placements": [{"entity_id": "camera.salon", "x": 1, "z": 1, "y": None}],
            }
        ],
    }


_STATES = [
    {"entity_id": "sensor.salon_temp", "attributes": {"device_class": "temperature", "friendly_name": "Salon temp"}},
    {"entity_id": "cover.roleta_salon", "attributes": {"device_class": "shutter", "friendly_name": "Roleta"}},
    {"entity_id": "light.salon", "attributes": {"friendly_name": "Salon light"}},
    {"entity_id": "camera.salon", "attributes": {}},
    {"entity_id": "binary_sensor.okno_salon", "attributes": {"device_class": "window"}},
    {"entity_id": "media_player.tv", "attributes": {"device_class": "tv"}},
    {"entity_id": "sensor.salon_battery", "attributes": {"device_class": "battery"}},
    {"entity_id": "light.kuchnia", "attributes": {}},
]
_ENTITY_REGISTRY = [
    {"entity_id": "sensor.salon_temp", "area_id": "salon", "device_id": None},
    {"entity_id": "cover.roleta_salon", "area_id": None, "device_id": "dev_blind"},
    {"entity_id": "light.salon", "area_id": "salon", "device_id": None},
    {"entity_id": "camera.salon", "area_id": "salon", "device_id": None},
    {"entity_id": "binary_sensor.okno_salon", "area_id": "salon", "device_id": None},
    {"entity_id": "media_player.tv", "area_id": "salon", "device_id": None},
    {"entity_id": "sensor.salon_battery", "area_id": "salon", "device_id": None},
    {"entity_id": "light.disabled", "area_id": "salon", "device_id": None, "disabled_by": "user"},
    {"entity_id": "light.kuchnia", "area_id": "kuchnia", "device_id": None},
]
_DEVICES = [{"id": "dev_blind", "area_id": "salon"}]
_AREAS = [{"area_id": "salon"}, {"area_id": "kuchnia"}]


class FakeNeon:
    def __init__(self) -> None:
        self.installed = True
        self.error_code: str | None = None
        self.version = "3.0.6"
        self.plans = {"main": {"building": copy.deepcopy(_EMPTY), "revision": 0}}
        self.history: dict[str, list[dict]] = {"main": []}
        self.images: dict[str, str] = {}
        self.calls: list[str] = []
        self.saved: list[dict] = []
        self.reject_save: str | None = None
        self.bump_after_snapshot = False
        self.admin = True

    def _err(self, code: str, message: str = "x"):
        raise ha.HAWebSocketError({"code": code, "message": message})

    def call(self, msg_type: str, **fields):
        self.calls.append(msg_type)
        if not self.installed:
            self._err("unknown_command", "Unknown command.")
        if self.error_code:
            self._err(self.error_code)
        name = msg_type.removeprefix("neonplan3d/")
        plan_id = fields.get("plan", "main")
        admin_only = {"building/save", "history/list", "history/snapshot", "history/restore", "image/set",
                      "license/get", "screens/list"}
        if name in admin_only and not self.admin:
            self._err("unauthorized", "Unauthorized")
        if name in ("building/get", "building/save", "history/list", "history/snapshot", "history/restore"):
            if plan_id not in self.plans:
                self._err("not_found", "Floor plan not found")
        plan = self.plans.get(plan_id)
        if name == "building/get":
            return {"building": copy.deepcopy(plan["building"]), "revision": plan["revision"], "plan": plan_id,
                    "version": self.version}
        if name == "building/save":
            if self.reject_save:
                self._err("invalid_format", self.reject_save)
            self.saved.append(copy.deepcopy(fields["building"]))
            plan["building"] = copy.deepcopy(fields["building"])
            plan["revision"] += 1
            return {"revision": plan["revision"]}
        if name == "history/snapshot":
            self.history[plan_id].append(
                {"id": f"snap{len(self.history[plan_id])}", "revision": plan["revision"], "saved_at": 1762858800.0,
                 "building": copy.deepcopy(plan["building"])}
            )
            self.history[plan_id] = self.history[plan_id][-20:]
            if self.bump_after_snapshot:
                plan["revision"] += 1
            return None
        if name == "history/list":
            return {"snapshots": [{k: v for k, v in h.items() if k != "building"} for h in reversed(self.history[plan_id])]}
        if name == "history/restore":
            snap = next((h for h in self.history[plan_id] if h["id"] == fields["snapshot_id"]), None)
            if snap is None:
                self._err("not_found", "Restore point not found")
            plan["building"] = copy.deepcopy(snap["building"])
            plan["revision"] += 1
            return {"revision": plan["revision"]}
        if name == "plans/list":
            return {"plans": [{"id": k, "revision": v["revision"]} for k, v in self.plans.items()], "trash": [],
                    "locate_plan": "main"}
        if name == "license/get":
            return {"instance": "0000aaaa1111bbbb", "active": False, "key_hint": "NP-XXXX", "licensee": "Someone",
                    "error": None}
        if name == "packs/list":
            return {"packs": [{"id": "garden", "name": "Garden", "version": "1.0", "features": [], "module": {"x": 1}}]}
        if name == "screens/list":
            return {"screens": [{"id": "abc", "kind": "panel", "since": 1.0, "reloadable": True, "user_agent": "x"}]}
        if name == "image/get":
            if fields["image_id"] not in self.images:
                self._err("not_found", "Image not found")
            return {"data": self.images[fields["image_id"]]}
        if name == "image/set":
            self.images[fields["image_id"]] = fields["data"]
            return None
        self._err("unknown_command")

    def writes(self) -> list[str]:
        return [c for c in self.calls if c.split("/", 1)[1] in
                ("building/save", "history/snapshot", "history/restore", "image/set")]


@pytest.fixture
def fake(monkeypatch, tmp_path) -> FakeNeon:
    f = FakeNeon()
    monkeypatch.setattr(ha, "_ws_call", lambda t, **kw: f.call(t, **kw))
    monkeypatch.setattr(ha, "_ws_call_timeout", lambda t, timeout, **kw: f.call(t, **kw))
    monkeypatch.setattr(ha, "get_states", lambda: copy.deepcopy(_STATES))
    monkeypatch.setattr(ha, "get_area_registry", lambda: copy.deepcopy(_AREAS))
    monkeypatch.setattr(ha, "get_entity_registry", lambda: copy.deepcopy(_ENTITY_REGISTRY))
    monkeypatch.setattr(ha, "get_device_registry", lambda: copy.deepcopy(_DEVICES))
    monkeypatch.setattr(ha, "get_config_entries", lambda domain=None: [{"state": "loaded", "title": "NeonPlan 3D"}])
    monkeypatch.setenv("HA_CONFIG_PATH", str(tmp_path))
    monkeypatch.setenv("NEXUS_MEDIA_PATH", str(tmp_path / "media"))
    return f


def _with_plan(fake: FakeNeon, building: dict | None = None, revision: int = 5) -> None:
    fake.plans["main"] = {"building": building or _building(), "revision": revision}


# --- status / reads ----------------------------------------------------------------


def test_get_status_reports_only_safe_license_fields(fake):
    result = get_status()

    assert result["installed"] is True and result["loaded"] is True
    assert result["version"] == "3.0.6"
    assert result["version_warning"] is None
    assert result["license"] == {"active": False, "error": None}
    assert result["packs"] == [{"id": "garden", "name": "Garden", "version": "1.0", "features": []}]
    assert result["screens"] == [{"id": "abc", "kind": "panel", "since": 1.0, "reloadable": True}]
    assert result["screens_open"] == 1
    assert "0000aaaa1111bbbb" not in str(result)


def test_get_status_section_error_stays_local(fake):
    fake.admin = False

    result = get_status()

    assert result["loaded"] is True
    assert result["license"]["error"] == "admin_required"
    assert result["screens"]["error"] == "admin_required"
    assert result["screens_open"] is None
    assert result["plans"] == [{"id": "main", "revision": 0}]


def test_unknown_command_over_a_real_websocket(monkeypatch):
    """`ha._ws_call` against a local WS server answering like HA without the integration."""
    import asyncio
    import json

    import websockets

    async def handler(ws):
        await ws.send(json.dumps({"type": "auth_required"}))
        await ws.recv()
        await ws.send(json.dumps({"type": "auth_ok"}))
        async for raw in ws:
            msg = json.loads(raw)
            await ws.send(json.dumps({"id": msg["id"], "type": "result", "success": False,
                                      "error": {"code": "unknown_command", "message": "Unknown command."}}))

    async def drive():
        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(ha, "_ws_url", lambda: f"ws://127.0.0.1:{port}/api/websocket")
        try:
            return await asyncio.wait_for(asyncio.to_thread(list_plans), timeout=30)
        finally:
            server.close()
            await server.wait_closed()

    assert asyncio.run(drive())["error"] == "neonplan_not_installed"


@pytest.mark.parametrize("tool", [get_status, list_plans, get_plan, list_restore_points])
def test_not_installed(fake, tool):
    fake.installed = False

    assert tool()["error"] == "neonplan_not_installed"


def test_installed_but_not_loaded(fake):
    fake.error_code = "not_loaded"

    result = get_status()

    assert result["error"] == "neonplan_not_installed"
    assert result["installed"] is True
    assert result["config_entry"]["state"] == "loaded"


def test_save_not_installed_writes_nothing(fake):
    fake.installed = False

    assert save_plan(_building())["error"] == "neonplan_not_installed"
    assert fake.saved == []


def test_get_plan_full_and_summary(fake):
    _with_plan(fake)

    full = get_plan()
    summary = get_plan(summary_only=True)

    assert full["revision"] == 5
    assert full["building"]["floors"][0]["id"] == "floor_parter"
    assert summary["totals"]["rooms"] == 2
    assert summary["floors"][0]["rooms"][0] == {"id": "room_salon", "name": "Salon", "area_id": "salon", "corners": 4}
    assert "light.salon" in summary["entities"]


def test_get_plan_warns_on_other_version(fake):
    fake.version = "3.1.0"

    result = get_plan()

    assert "3.1.0" in result["version_warning"]
    assert "building" in result


def test_get_plan_rejects_bad_plan_id_without_io(fake):
    assert get_plan(plan="../main")["error"] == "invalid_plan"
    assert fake.calls == []


def test_get_plan_unknown_plan(fake):
    assert get_plan(plan="p0000000000")["error"] == "plan_not_found"


def test_admin_required_is_mapped(fake):
    fake.admin = False

    assert list_restore_points()["error"] == "admin_required"


# --- validate_plan -------------------------------------------------------------------


def test_validate_valid_building(fake):
    result = validate_plan(_building())

    assert result["valid"] is True, result["errors"]
    assert result["summary"]["openings"] == 2


def test_validate_stored_plan(fake):
    _with_plan(fake)

    result = validate_plan()

    assert result["valid"] is True
    assert result["revision"] == 5


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (lambda b: b["floors"][0]["openings"][0].update(edge=7), "edge 7 but room 'room_salon' has 4 edges"),
        (lambda b: b["floors"][0]["openings"][0].update(room_id="room_nope"), "room_id 'room_nope' is not on floor"),
        (lambda b: b["floors"][0]["openings"][0].update(offset=5.5), "leaves its edge"),
        (lambda b: b["floors"][0]["furniture"][0].update(entity="light.ghost"), "entity 'light.ghost' does not exist"),
        (lambda b: b["floors"][0]["rooms"][1].update(area_id="garaz"), "area_id 'garaz' is not in HA's area registry"),
        (lambda b: b["floors"][0]["rooms"][0].pop("floor_material"), "floor_material: string"),
        (lambda b: b["floors"][0]["rooms"][1].update(id="room_salon"), "duplicate room id 'room_salon'"),
        (lambda b: b["floors"][0]["openings"][0].update(cover="Roleta salon"), "cover is not an entity id"),
        (lambda b: b.update(version=2), "building.version: must be 1"),
    ],
)
def test_validate_catches_errors(fake, mutate, expected):
    b = _building()
    mutate(b)

    result = validate_plan(b)

    assert result["valid"] is False
    assert any(expected in e for e in result["errors"]), result["errors"]


def test_validate_warns_about_auto_bound_nulls(fake):
    b = _building()
    b["floors"][0]["openings"][0]["cover"] = None
    b["floors"][0]["openings"][0]["contact"] = None

    result = validate_plan(b)

    assert result["valid"] is True
    assert any("room 'room_salon': 2 entity field(s) are null" in w for w in result["warnings"])


def test_validate_without_ha_check(fake):
    b = _building()
    b["floors"][0]["furniture"][0]["entity"] = "light.ghost"

    assert validate_plan(b, check_ha=False)["valid"] is True


# --- save_plan -----------------------------------------------------------------------


def test_save_into_empty_plan(fake):
    result = save_plan(_building())

    assert result["saved"] is True
    assert result["revision_before"] == 0 and result["revision"] == 1
    assert result["snapshot_taken"] is False  # nothing to keep in an empty plan
    assert result["verified"] is True
    assert fake.writes() == ["neonplan3d/building/save"]


def test_save_refuses_non_empty_plan_without_overwrite(fake):
    _with_plan(fake)

    result = save_plan(_building())

    assert result["error"] == "plan_not_empty"
    assert fake.writes() == []


def test_save_overwrite_takes_restore_point_first(fake):
    _with_plan(fake)
    new = _building()
    new["floors"][0]["name"] = "Ground floor"

    result = save_plan(new, overwrite=True, expected_revision=5)

    assert result["saved"] is True and result["snapshot_taken"] is True
    assert fake.writes() == ["neonplan3d/history/snapshot", "neonplan3d/building/save"]
    assert fake.history["main"][0]["building"]["floors"][0]["name"] == "Parter"
    assert fake.plans["main"]["building"]["floors"][0]["name"] == "Ground floor"


def test_save_expected_revision_conflict_writes_nothing(fake):
    _with_plan(fake)

    result = save_plan(_building(), overwrite=True, expected_revision=4)

    assert result["error"] == "revision_conflict"
    assert result["current_revision"] == 5
    assert fake.writes() == []


def test_save_detects_revision_moving_after_snapshot(fake):
    _with_plan(fake)
    fake.bump_after_snapshot = True

    result = save_plan(_building(), overwrite=True)

    assert result["error"] == "revision_conflict"
    assert result["snapshot_taken"] is True
    assert fake.saved == []


def test_save_validation_errors_block_any_write(fake):
    b = _building()
    b["floors"][0]["openings"][0]["edge"] = 9

    result = save_plan(b)

    assert result["error"] == "validation_failed"
    assert result["errors"]
    assert fake.writes() == []


def test_save_server_schema_refusal(fake):
    fake.reject_save = "expected float for dictionary value @ data['building']['floors'][0]['openings'][0]['sill']"

    result = save_plan(_building())

    assert result["error"] == "invalid_building"
    assert "sill" in result["message"]


def test_save_version_gate_and_force(fake):
    fake.version = "3.1.0"

    blocked = save_plan(_building())
    forced = save_plan(_building(), force=True)

    assert blocked["error"] == "neonplan_version_unsupported"
    assert forced["saved"] is True
    assert len(fake.saved) == 1


def test_save_reports_unverified_read_back(fake, monkeypatch):
    real = fake.call

    def lossy(msg_type, **fields):
        result = real(msg_type, **fields)
        if msg_type == "neonplan3d/building/get" and fake.saved:
            result["building"]["floors"][0]["name"] = "changed by server"
        return result

    monkeypatch.setattr(ha, "_ws_call", lambda t, **kw: lossy(t, **kw))

    result = save_plan(_building())

    assert result["saved"] is True and result["verified"] is False
    assert "/floors/0/name" in result["differences"]


# --- patch_plan ----------------------------------------------------------------------------


def test_patch_plan_replace_and_append(fake):
    _with_plan(fake)
    lamp = {"id": "lamp_kuchnia", "type": "lamp_ceiling", "x": 8, "z": 2, "rotation": 0, "w": 0.4, "d": 0.4,
            "h": 0.08, "variant": None, "entity": "light.kuchnia"}

    result = patch_plan(
        [
            {"op": "test", "path": "/floors/0/rooms/0/name", "value": "Salon"},
            {"op": "replace", "path": "/floors/0/rooms/0/name", "value": "Living room"},
            {"op": "add", "path": "/floors/0/furniture/-", "value": lamp},
        ],
        expected_revision=5,
    )

    assert result["saved"] is True and result["operations"] == 3
    stored = fake.plans["main"]["building"]["floors"][0]
    assert stored["rooms"][0]["name"] == "Living room"
    assert stored["furniture"][-1]["id"] == "lamp_kuchnia"
    assert fake.writes() == ["neonplan3d/history/snapshot", "neonplan3d/building/save"]


@pytest.mark.parametrize(
    "ops,index",
    [
        ([{"op": "replace", "path": "/floors/3/name", "value": "x"}], 0),
        ([{"op": "test", "path": "/floors/0/name", "value": "Wrong"}], 0),
        ([{"op": "replace", "path": "/floors/0/name", "value": "ok"}, {"op": "remove", "path": "floors"}], 1),
        ([{"op": "jump", "path": "/floors"}], 0),
        ([{"op": "add", "path": "/floors/0/rooms/9", "value": {}}], 0),
    ],
)
def test_patch_plan_bad_operation_writes_nothing(fake, ops, index):
    _with_plan(fake)

    result = patch_plan(ops)

    assert result["error"] == "invalid_patch"
    assert result["operation"] == index
    assert fake.writes() == []


def test_patch_plan_result_is_validated(fake):
    _with_plan(fake)

    result = patch_plan([{"op": "replace", "path": "/floors/0/openings/0/edge", "value": 12}])

    assert result["error"] == "validation_failed"
    assert fake.writes() == []


def test_patch_plan_expected_revision_conflict(fake):
    _with_plan(fake)

    result = patch_plan([{"op": "replace", "path": "/floors/0/name", "value": "x"}], expected_revision=1)

    assert result["error"] == "revision_conflict"
    assert fake.writes() == []


def test_json_patch_move_copy_remove_and_escapes():
    doc = {"a": {"b/c": 1, "d~e": 2}, "list": [1, 2, 3]}
    ops = [
        {"op": "copy", "from": "/a/b~1c", "path": "/x"},
        {"op": "move", "from": "/a/d~0e", "path": "/list/0"},
        {"op": "remove", "path": "/list/3"},
    ]

    result = neonplan._apply_patch(doc, ops)

    assert result == {"a": {"b/c": 1}, "x": 1, "list": [2, 1, 2]}
    assert doc["list"] == [1, 2, 3]  # input untouched


# --- restore points ----------------------------------------------------------------------------


def test_restore_point_round_trip(fake):
    _with_plan(fake)
    created = create_restore_point()
    patch_plan([{"op": "replace", "path": "/floors/0/name", "value": "Changed"}])
    points = list_restore_points()

    result = restore_restore_point(created["restore_point"]["id"])

    assert created["created"] is True and created["dropped_oldest"] is False
    assert points["count"] == 2 and points["restore_points"][0]["saved_at_iso"].startswith("2025")
    assert result["restored"] is True
    assert fake.plans["main"]["building"]["floors"][0]["name"] == "Parter"


def test_create_restore_point_reports_dropping_oldest(fake):
    _with_plan(fake)
    for _ in range(20):
        fake.call("neonplan3d/history/snapshot")

    assert create_restore_point()["dropped_oldest"] is True


def test_restore_rejects_bad_id_without_io(fake):
    assert restore_restore_point("../x")["error"] == "invalid_snapshot_id"
    assert fake.calls == []


def test_restore_unknown_point(fake):
    _with_plan(fake)

    assert restore_restore_point("nope")["error"] == "restore_point_not_found"
    assert fake.writes() == []


# --- upload_background_image -------------------------------------------------------------------

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_upload_from_config_file(fake, tmp_path):
    (tmp_path / "www").mkdir()
    (tmp_path / "www" / "plan.png").write_bytes(_PNG)

    result = upload_background_image("scan_parter", path=str(tmp_path / "www" / "plan.png"))

    assert result["uploaded"] is True and result["type"] == "png" and result["replaced"] is False
    assert "deletes it at its next start" in result["warning"]
    assert fake.images["scan_parter"].startswith("data:image/png;base64,")


def test_upload_from_base64_and_overwrite_guard(fake):
    data = base64.b64encode(_PNG).decode()
    first = upload_background_image("scan", data_base64="data:image/png;base64," + data)
    second = upload_background_image("scan", data_base64=data)
    third = upload_background_image("scan", data_base64=data, overwrite=True)

    assert first["uploaded"] is True
    assert second["error"] == "image_exists"
    assert third["replaced"] is True


def test_upload_rejects_path_outside_roots_without_io(fake, tmp_path):
    outside = tmp_path.parent / "elsewhere.png"
    outside.write_bytes(_PNG)

    result = upload_background_image("scan", path=str(outside))

    assert result["error"] == "path_not_allowed"
    assert fake.calls == []


def test_upload_rejects_too_large_without_io(fake, tmp_path):
    big = tmp_path / "big.png"
    big.write_bytes(_PNG + b"\x00" * (neonplan._MAX_IMAGE_BYTES + 1))

    assert upload_background_image("scan", path=str(big))["error"] == "image_too_large"
    too_long = "A" * (neonplan._MAX_IMAGE_CHARS + 4)
    assert upload_background_image("scan", data_base64=too_long)["error"] == "image_too_large"
    assert fake.calls == []


def test_upload_rejects_non_image_content(fake, tmp_path):
    fake_png = tmp_path / "secrets.png"
    fake_png.write_bytes(b"api_key: not-an-image")

    assert upload_background_image("scan", path=str(fake_png))["error"] == "unsupported_image"
    assert upload_background_image("scan", path=str(tmp_path / "x.yaml"))["error"] == "unsupported_image"
    assert fake.calls == []


@pytest.mark.parametrize("image_id", ["", "a b", "x" * 65, "../x"])
def test_upload_rejects_bad_image_id(fake, image_id):
    assert upload_background_image(image_id, data_base64="AAAA")["error"] == "invalid_image_id"


# --- suggest_bindings ---------------------------------------------------------------------------


def test_suggest_bindings_groups_area_entities_by_role(fake):
    _with_plan(fake)

    result = suggest_bindings("room_salon")

    s = result["suggestions"]
    assert [e["entity_id"] for e in s["window_cover"]["entities"]] == ["cover.roleta_salon"]  # via its device's area
    assert s["window_cover"]["entities"][0]["used_at"] == ["floors[0].openings[0].cover"]
    assert [e["entity_id"] for e in s["lamp"]["entities"]] == ["light.salon"]
    assert [e["entity_id"] for e in s["climate_temperature"]["entities"]] == ["sensor.salon_temp"]
    assert [e["entity_id"] for e in s["window_contact"]["entities"]] == ["binary_sensor.okno_salon"]
    assert [e["entity_id"] for e in s["media"]["entities"]] == ["media_player.tv"]
    assert "camera" in s
    assert result["unassigned"] == ["sensor.salon_battery"]
    assert "light.disabled" not in str(result)
    assert fake.writes() == []


def test_suggest_bindings_errors(fake):
    b = _building()
    b["floors"][0]["rooms"][1]["area_id"] = None
    _with_plan(fake, b)

    assert suggest_bindings("room_nope")["error"] == "room_not_found"
    assert suggest_bindings("room_kuchnia")["error"] == "room_has_no_area"


# --- annotations ------------------------------------------------------------------------------


def test_annotations_reads_and_destructive_writes():
    import asyncio

    tools = {t.name: t for t in asyncio.run(neonplan.mcp.list_tools())}
    reads = {n for n, t in tools.items() if t.annotations.readOnlyHint}
    assert reads == {"get_status", "list_plans", "get_plan", "validate_plan", "list_restore_points", "suggest_bindings"}
    assert {n for n, t in tools.items() if t.annotations.destructiveHint} == {
        "save_plan", "patch_plan", "create_restore_point", "restore_restore_point", "upload_background_image",
    }


# --- security review 0.29.0: L1 / L7 -----------------------------------------------------------


def test_ids_with_trailing_newline_are_rejected(fake):
    assert get_plan(plan="main\n")["error"] == "invalid_plan"
    assert restore_restore_point("snap0\n")["error"] == "invalid_snapshot_id"
    assert upload_background_image("scan\n", data_base64="AAAA")["error"] == "invalid_image_id"
    assert fake.calls == []


def test_patch_replacing_the_whole_plan_needs_overwrite(fake):
    _with_plan(fake)
    ops = [{"op": "replace", "path": "", "value": _building()}]

    refused = patch_plan(ops)
    allowed = patch_plan(ops, overwrite=True)

    assert refused["error"] == "plan_not_empty"
    assert allowed["saved"] is True
    assert fake.saved and len(fake.saved) == 1
