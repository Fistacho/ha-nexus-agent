"""NeonPlan 3D (HACS integration `neonplan3d`) — read, check and edit the 3D house plan over HA's WebSocket API.

Transport (verified against the integration's source, v3.0.6, `websocket.py`):
every call is one HA WebSocket command through `ha_client` (`neonplan3d/...`),
with nexus's own HA token. Used commands, nothing else:

- read: `building/get {plan}` → `{building, revision, plan, version}`,
  `plans/list`, `history/list {plan}` (admin), `license/get` (admin),
  `packs/list`, `screens/list` (admin), `image/get {image_id}`;
- write (all admin): `building/save {building, plan}` → `{revision}`,
  `history/snapshot {plan}`, `history/restore {snapshot_id, plan}`,
  `image/set {image_id, data}`.

What the server does NOT do, so this module does it (spec `neonplan_schema.md`
§0, §10, §12, §13):

- `building/save` replaces the whole building and has no revision check
  (last write wins). Writes here read the revision, refuse on a mismatch with
  `expected_revision`, take a restore point, read the revision again right
  before the save (best effort: there is no compare-and-swap) and read the
  building back afterwards.
- The server schema (`BUILDING_SCHEMA`, voluptuous, extra keys allowed)
  checks types and ranges but no references: an opening's `room_id`/`edge`,
  duplicate ids, an `offset` outside its wall. `validate_plan` checks the
  structure the schema requires (not every range — the server still
  validates on save and answers `invalid_format`) plus those references, and
  against the live HA: every `area_id` in the area registry and every entity
  id in the state machine.
- Background images (`image/set`, data URL ≤ 8 MiB characters) that no plan
  references are deleted when the integration starts (`storage.py`).

Entity fields: `null` = NeonPlan binds automatically from the room's area,
`"none"` = nothing, `"domain.object"` = that entity.
"""
from __future__ import annotations

import base64
import copy
import math
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

import ha_client as ha
from tools._contract import destructive, read

mcp = FastMCP("neonplan")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}

_SUPPORTED_VERSION_PREFIX = "3.0."
_MAIN_PLAN = "main"
_PLAN_RE = re.compile(r"[a-z0-9_]{1,32}")  # these three: always fullmatch
_ID_RE = re.compile(r"[A-Za-z0-9_\-.]{1,64}")
_ENTITY_RE = re.compile(r"[a-z_]+\.[a-z0-9_]+")

_SAVE_TIMEOUT = 30.0
_IMAGE_TIMEOUT = 60.0
_MAX_IMAGE_CHARS = 8 * 1024 * 1024  # schema.py MAX_IMAGE_CHARS
_IMAGE_TYPES = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp"}
_HISTORY_MAX = 20  # const.py HISTORY_MAX

# schema.py limits
_MAX_FLOORS = 20
_MAX_ROOMS = 200
_MAX_ITEMS = 1000
_COORD = 1000.0

_OPENING_TYPES = {"door", "window", "garage"}
_OUTDOOR_TYPES = {"lawn", "terrace", "path", "driveway", "pool", "bed", "wild", "hedge", "fence", "pergola", "balcony"}
_FLOOR_MATERIALS = {"wood", "oak", "tiles", "carpet", "stone", "concrete"}  # model.ts FLOOR_MATERIALS
_PLACEABLE_DOMAINS = {  # devices.ts DOMAIN_KIND
    "light", "switch", "input_boolean", "fan", "cover", "climate", "media_player", "lock",
    "sensor", "binary_sensor", "camera",
}
# entity fields that take `null` (auto from the area) / "none" / an entity id
_OPENING_ENTITY_FIELDS = ("cover", "contact", "contact2", "tilt", "tilt2", "position", "tilt_angle", "lock")
_OPENING_AUTO_FIELDS = ("cover", "contact")
_FURNITURE_ENTITY_FIELDS = (
    "entity", "power", "state_entity", "state_entity2", "color_entity", "extra", "fan", "heat_sensor", "room_sensor",
)
_LAMP_PREFIX = "lamp_"

# devices.ts: cover classes a window blind can have, garage covers, contact classes
_BLIND_CLASSES = {None, "shutter", "blind", "awning", "shade", "curtain", "window"}
_GARAGE_COVER_CLASSES = {"garage", "gate"}
_WINDOW_CONTACT_CLASSES = {"window", "opening"}
_CLIMATE_SENSOR_CLASSES = {"temperature": "temperature", "humidity": "humidity", "carbon_dioxide": "co2"}

_SCREEN_KEYS = ("id", "kind", "since", "reloadable", "plan", "template", "name", "version")
_PACK_KEYS = ("id", "name", "version", "features")


class _NeonError(Exception):
    """Internal: carries one `{"error": code, ...}` result up to the tool."""

    def __init__(self, code: str, message: str | None = None, **extra: Any) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message
        self.extra = extra

    def as_dict(self) -> dict:
        result: dict[str, Any] = {"error": self.code}
        if self.message:
            result["message"] = self.message
        result.update(self.extra)
        return result


# --- WebSocket ----------------------------------------------------------------


def _ws(msg_type: str, *, timeout: float | None = None, **fields: Any) -> Any:
    """One `neonplan3d/<msg_type>` command; HA errors become `_NeonError`."""
    full = f"neonplan3d/{msg_type}"
    try:
        if timeout is None:
            return ha._ws_call(full, **fields)
        return ha._ws_call_timeout(full, timeout, **fields)
    except ha.HAWebSocketError as e:
        if e.code == "unknown_command":
            raise _NeonError(
                "neonplan_not_installed",
                "Home Assistant does not know the neonplan3d commands: NeonPlan 3D is not installed or not loaded.",
            ) from None
        if e.code == "not_loaded":
            raise _NeonError("neonplan_not_installed", "NeonPlan 3D is installed but not set up (no config entry loaded).") from None
        if e.code == "unauthorized":
            raise _NeonError("admin_required", f"{full} needs an admin user; nexus's token is not one.") from None
        if e.code == "not_found":
            raise _NeonError("not_found", e.message or "Not found.", command=full) from None
        if e.code == "invalid_format":
            raise _NeonError("invalid_request", e.message or "Rejected by the integration's schema.", command=full) from None
        raise _NeonError("ws_error", e.message or str(e), code=e.code, command=full) from None
    except (RuntimeError, OSError, TimeoutError) as e:
        raise _NeonError("ha_unreachable", f"No answer from Home Assistant for {full} ({type(e).__name__}).") from None


def _check_plan_id(plan: Any) -> str:
    if not isinstance(plan, str) or not _PLAN_RE.fullmatch(plan):
        raise _NeonError("invalid_plan", "plan must be a plan id such as `main` or `p0123456789` (neonplan_list_plans).")
    return plan


def _get_building(plan: str) -> dict:
    try:
        live = _ws("building/get", plan=plan)
    except _NeonError as e:
        if e.code == "not_found":
            raise _NeonError("plan_not_found", f"No NeonPlan plan {plan!r}; see neonplan_list_plans.", plan=plan) from None
        raise
    if not isinstance(live, dict) or not isinstance(live.get("building"), dict):
        raise _NeonError("neonplan_protocol_error", "building/get did not return a building.")
    return live


def _version_warning(version: Any) -> str | None:
    if isinstance(version, str) and version.startswith(_SUPPORTED_VERSION_PREFIX):
        return None
    return f"NeonPlan 3D {version} is not 3.0.x; these tools were built against 3.0.6 and the plan format may differ."


def _require_supported(version: Any, force: bool) -> None:
    if not force and _version_warning(version):
        raise _NeonError(
            "neonplan_version_unsupported",
            f"NeonPlan 3D {version} is not 3.0.x; nothing was written. Pass force=true only after checking the format.",
            version=version,
        )


def _summary(building: dict) -> dict:
    floors = [f for f in building.get("floors") or [] if isinstance(f, dict)]
    return {
        "floors": len(floors),
        "rooms": sum(len(f.get("rooms") or []) for f in floors),
        "openings": sum(len(f.get("openings") or []) for f in floors),
        "furniture": sum(len(f.get("furniture") or []) for f in floors),
        "placements": sum(len(f.get("placements") or []) for f in floors),
    }


# --- validation ---------------------------------------------------------------


def _is_num(v: Any, lo: float = -math.inf, hi: float = math.inf) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and lo <= v <= hi


def _is_id(v: Any) -> bool:
    return isinstance(v, str) and bool(_ID_RE.fullmatch(v))


def _is_point(p: Any) -> bool:
    return isinstance(p, (list, tuple)) and len(p) == 2 and all(_is_num(c, -_COORD, _COORD) for c in p)


def _list_at(obj: dict, key: str, path: str, errors: list[str], *, required: bool, limit: int) -> list:
    if key not in obj:
        if required:
            errors.append(f"{path}.{key}: missing")
        return []
    value = obj[key]
    if not isinstance(value, list):
        errors.append(f"{path}.{key}: must be a list")
        return []
    if len(value) > limit:
        errors.append(f"{path}.{key}: {len(value)} items, at most {limit}")
    return value


def _structure(b: Any) -> list[str]:
    """Structural errors against what `BUILDING_SCHEMA` (schema.py 3.0.6) requires."""
    errors: list[str] = []
    if not isinstance(b, dict):
        return ["building: must be a JSON object"]
    if b.get("version") != 1:
        errors.append("building.version: must be 1")
    settings = b.get("settings")
    if not isinstance(settings, dict):
        errors.append("building.settings: missing or not an object")
    else:
        for key, lo, hi in (("wall_exterior", 0.02, 1), ("wall_interior", 0.02, 1), ("grid", 0.01, 1)):
            if not _is_num(settings.get(key), lo, hi):
                errors.append(f"settings.{key}: number {lo}..{hi} required")
    floors = _list_at(b, "floors", "building", errors, required=True, limit=_MAX_FLOORS)
    for fi, f in enumerate(floors):
        fp = f"floors[{fi}]"
        if not isinstance(f, dict):
            errors.append(f"{fp}: must be an object")
            continue
        if not _is_id(f.get("id")):
            errors.append(f"{fp}.id: 1-64 characters of A-Z a-z 0-9 _ - . required")
        if not isinstance(f.get("name"), str) or len(f["name"]) > 100:
            errors.append(f"{fp}.name: string up to 100 characters required")
        for key, lo, hi in (("elevation", -100, 500), ("height", 1, 20), ("cut_height", 0.2, 20)):
            if not _is_num(f.get(key), lo, hi):
                errors.append(f"{fp}.{key}: number {lo}..{hi} required")
        if "background" not in f:
            errors.append(f"{fp}.background: missing (use null for none)")
        elif f["background"] is not None:
            bg = f["background"]
            if not isinstance(bg, dict) or not _is_id(bg.get("image_id")) or not _is_num(bg.get("width"), 0):
                errors.append(f"{fp}.background: null or {{image_id, x, z, width, ...}} required")
        for ri, r in enumerate(_list_at(f, "rooms", fp, errors, required=True, limit=_MAX_ROOMS)):
            rp = f"{fp}.rooms[{ri}]"
            if not isinstance(r, dict):
                errors.append(f"{rp}: must be an object")
                continue
            if not _is_id(r.get("id")):
                errors.append(f"{rp}.id: invalid")
            if not isinstance(r.get("name"), str) or len(r["name"]) > 100:
                errors.append(f"{rp}.name: string up to 100 characters required")
            if "area_id" not in r or not (r["area_id"] is None or isinstance(r["area_id"], str)):
                errors.append(f"{rp}.area_id: key required, string or null")
            pts = r.get("points")
            if not isinstance(pts, list) or not 3 <= len(pts) <= 200 or not all(_is_point(p) for p in pts):
                errors.append(f"{rp}.points: 3-200 [x, z] pairs within -1000..1000 required")
            if not isinstance(r.get("floor_material"), str) or len(r["floor_material"]) > 32:
                errors.append(f"{rp}.floor_material: string up to 32 characters required")
        for oi, o in enumerate(_list_at(f, "openings", fp, errors, required=True, limit=_MAX_ITEMS)):
            op = f"{fp}.openings[{oi}]"
            if not isinstance(o, dict):
                errors.append(f"{op}: must be an object")
                continue
            if not _is_id(o.get("id")) or not _is_id(o.get("room_id")):
                errors.append(f"{op}: id and room_id required")
            if not isinstance(o.get("edge"), int) or isinstance(o.get("edge"), bool) or not 0 <= o["edge"] <= 200:
                errors.append(f"{op}.edge: integer 0..200 required")
            for key in ("offset", "width"):
                if not _is_num(o.get(key), 0, 100):
                    errors.append(f"{op}.{key}: number 0..100 required")
            if o.get("type") not in _OPENING_TYPES:
                errors.append(f"{op}.type: one of {sorted(_OPENING_TYPES)} required")
            for key in ("sill", "height"):
                if not _is_num(o.get(key)):
                    errors.append(f"{op}.{key}: number required")
        for mi, m in enumerate(_list_at(f, "furniture", fp, errors, required=True, limit=_MAX_ITEMS)):
            mp = f"{fp}.furniture[{mi}]"
            if not isinstance(m, dict):
                errors.append(f"{mp}: must be an object")
                continue
            if not _is_id(m.get("id")) or not isinstance(m.get("type"), str) or not m["type"]:
                errors.append(f"{mp}: id and type required")
            for key in ("x", "z"):
                if not _is_num(m.get(key), -_COORD, _COORD):
                    errors.append(f"{mp}.{key}: number within -1000..1000 required")
            for key in ("rotation", "w", "d", "h"):
                if not _is_num(m.get(key)):
                    errors.append(f"{mp}.{key}: number required")
            if "variant" not in m:
                errors.append(f"{mp}.variant: key required (null for default)")
        for pi, p in enumerate(_list_at(f, "placements", fp, errors, required=True, limit=_MAX_ITEMS)):
            pp = f"{fp}.placements[{pi}]"
            if not isinstance(p, dict):
                errors.append(f"{pp}: must be an object")
                continue
            if not isinstance(p.get("entity_id"), str):
                errors.append(f"{pp}.entity_id: required")
            for key in ("x", "z"):
                if not _is_num(p.get(key), -_COORD, _COORD):
                    errors.append(f"{pp}.{key}: number within -1000..1000 required")
            if "y" not in p or not (p["y"] is None or _is_num(p["y"])):
                errors.append(f"{pp}.y: key required, number or null")
        for ai, a in enumerate(_list_at(f, "outdoor", fp, errors, required=False, limit=_MAX_ITEMS)):
            ap = f"{fp}.outdoor[{ai}]"
            if not isinstance(a, dict) or not _is_id(a.get("id")) or a.get("type") not in _OUTDOOR_TYPES:
                errors.append(f"{ap}: id and type ({', '.join(sorted(_OUTDOOR_TYPES))}) required")
            elif not isinstance(a.get("points"), list) or len(a["points"]) < 3 or not all(_is_point(q) for q in a["points"]):
                errors.append(f"{ap}.points: at least 3 [x, z] pairs required")
        for wi, w in enumerate(_list_at(f, "walls", fp, errors, required=False, limit=_MAX_ITEMS)):
            if not isinstance(w, dict) or not _is_id(w.get("id")) or not _is_point(w.get("a")) or not _is_point(w.get("b")):
                errors.append(f"{fp}.walls[{wi}]: id, a [x, z] and b [x, z] required")
    return errors


def _edge_len(points: list, edge: int) -> float:
    a, b = points[edge], points[(edge + 1) % len(points)]
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _area(points: list) -> float:
    s = 0.0
    for i, (x0, z0) in enumerate(points):
        x1, z1 = points[(i + 1) % len(points)]
        s += x0 * z1 - x1 * z0
    return abs(s) / 2


def _inside(x: float, z: float, poly: list) -> bool:
    inside = False
    for i in range(len(poly)):
        x0, z0 = poly[i]
        x1, z1 = poly[(i + 1) % len(poly)]
        if (z0 > z) != (z1 > z) and x < (x1 - x0) * (z - z0) / (z1 - z0) + x0:
            inside = not inside
    return inside


def _ref_ok(v: Any) -> bool:
    return v is None or v == "none" or (isinstance(v, str) and bool(_ENTITY_RE.fullmatch(v)))


def _entity_refs(value: Any, path: str, out: list[tuple[str, str]]) -> None:
    """Every string in `value` that has the shape of an entity id, with its path."""
    if isinstance(value, dict):
        for k, v in value.items():
            _entity_refs(v, f"{path}.{k}" if path else str(k), out)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _entity_refs(v, f"{path}[{i}]", out)
    elif isinstance(value, str) and _ENTITY_RE.fullmatch(value):
        out.append((path, value))


def _lint(b: dict) -> tuple[list[str], list[str]]:
    """References and plausibility the server schema does not check (spec `neonplan_validate.py`)."""
    errors: list[str] = []
    warns: list[str] = []
    seen: set[str] = set()

    def uniq(kind: str, ident: str) -> None:
        if f"{kind}:{ident}" in seen:
            errors.append(f"duplicate {kind} id '{ident}'")
        seen.add(f"{kind}:{ident}")

    floors = b["floors"]
    if floors and not any(f["rooms"] for f in floors):
        warns.append("no floor has rooms: the card shows 'no building' until one has")
    by_elev = sorted(floors, key=lambda f: f["elevation"])
    for lo, hi in zip(by_elev, by_elev[1:], strict=False):
        if lo["elevation"] + lo["height"] > hi["elevation"] + 1e-6:
            warns.append(f"floor '{lo['id']}' (top {lo['elevation'] + lo['height']:.2f}) overlaps floor '{hi['id']}'")
    linked: dict[str, str] = {}
    for f in floors:
        uniq("floor", f["id"])
        rooms = {r["id"]: r for r in f["rooms"]}
        walls = {w["id"]: w for w in f.get("walls") or []}
        auto_by_room: dict[str, int] = {}
        for r in f["rooms"]:
            uniq("room", r["id"])
            pts = r["points"]
            if list(pts[0]) == list(pts[-1]):
                errors.append(f"room '{r['id']}': first corner repeated at the end (do not repeat it)")
            if _area(pts) < 0.25:
                errors.append(f"room '{r['id']}': area {_area(pts):.3f} m2 is degenerate")
            if r["floor_material"] not in _FLOOR_MATERIALS:
                warns.append(f"room '{r['id']}': floor_material '{r['floor_material']}' unknown to the frontend")
            for key in ("wall_heights", "wall_thickness", "wall_slopes", "wall_splits"):
                if isinstance(r.get(key), list) and len(r[key]) not in (0, len(pts)):
                    warns.append(f"room '{r['id']}': {key} has {len(r[key])} entries for {len(pts)} edges")
            if r["area_id"] is None:
                warns.append(f"room '{r['id']}': area_id null, so nothing is bound automatically in this room")
            climate = r.get("climate") or {}
            for k in ("temperature", "humidity", "co2"):
                if not _ref_ok(climate.get(k) if isinstance(climate, dict) else None):
                    errors.append(f"room '{r['id']}': climate.{k} is not an entity id, null or 'none'")
        for o in f["openings"]:
            uniq("opening", o["id"])
            if o.get("wall"):
                w = walls.get(o["wall"])
                if not w:
                    errors.append(f"opening '{o['id']}': free wall '{o['wall']}' is not on floor '{f['id']}'")
                    continue
                length = math.hypot(w["b"][0] - w["a"][0], w["b"][1] - w["a"][1])
            else:
                r = rooms.get(o["room_id"])
                if not r:
                    errors.append(f"opening '{o['id']}': room_id '{o['room_id']}' is not on floor '{f['id']}'")
                    continue
                if o["edge"] >= len(r["points"]):
                    errors.append(f"opening '{o['id']}': edge {o['edge']} but room '{r['id']}' has {len(r['points'])} edges (0..{len(r['points']) - 1})")
                    continue
                length = _edge_len(r["points"], o["edge"])
                if r["area_id"]:
                    nulls = sum(1 for k in _OPENING_AUTO_FIELDS if o.get(k) is None)
                    auto_by_room[r["id"]] = auto_by_room.get(r["id"], 0) + nulls
            if o["offset"] - o["width"] / 2 < -1e-6 or o["offset"] + o["width"] / 2 > length + 1e-6:
                errors.append(
                    f"opening '{o['id']}': centre {o['offset']} +/- {o['width'] / 2} leaves its edge (length {length:.2f} m)"
                )
            if o["sill"] + o["height"] > f["height"] + 1e-6:
                warns.append(f"opening '{o['id']}': top {o['sill'] + o['height']:.2f} m is above floor height {f['height']}")
            if o["type"] == "door" and o["sill"] > 0:
                warns.append(f"opening '{o['id']}': door with sill {o['sill']}")
            for k in _OPENING_ENTITY_FIELDS:
                if not _ref_ok(o.get(k)):
                    errors.append(f"opening '{o['id']}': {k} is not an entity id, null or 'none'")
        zones = [r["points"] for r in f["rooms"]] + [a["points"] for a in f.get("outdoor") or []]
        for m in f["furniture"]:
            uniq("furniture", m["id"])
            room = next((r for r in f["rooms"] if _inside(m["x"], m["z"], r["points"])), None)
            if zones and room is None and not any(_inside(m["x"], m["z"], z) for z in zones):
                warns.append(f"furniture '{m['id']}' ({m['x']}, {m['z']}) lies in no room or outdoor area")
            for k in _FURNITURE_ENTITY_FIELDS:
                if not _ref_ok(m.get(k)):
                    errors.append(f"furniture '{m['id']}': {k} is not an entity id, null or 'none'")
            if room is not None and room["area_id"] and m["type"].startswith(_LAMP_PREFIX) and m.get("entity") is None:
                auto_by_room[room["id"]] = auto_by_room.get(room["id"], 0) + 1
            ent = m.get("entity")
            if isinstance(ent, str) and ent != "none":
                if ent in linked:
                    warns.append(f"entity '{ent}' linked twice ({linked[ent]} and furniture {m['id']})")
                linked[ent] = f"furniture {m['id']}"
        for p in f["placements"]:
            ent = p["entity_id"]
            if not _ENTITY_RE.fullmatch(ent):
                errors.append(f"placement '{ent}': not an entity id")
                continue
            dom = ent.split(".", 1)[0]
            if dom not in _PLACEABLE_DOMAINS:
                warns.append(f"placement '{ent}': domain '{dom}' cannot be a pin (vacuum -> furniture robot_vacuum)")
            if dom == "light" and not p.get("pin"):
                warns.append(f"placement '{ent}': lights turn into lamp furniture on load unless pin: true")
            if zones and not any(_inside(p["x"], p["z"], z) for z in zones):
                warns.append(f"placement '{ent}' lies in no room or outdoor area")
            if ent in linked:
                warns.append(f"entity '{ent}' linked twice ({linked[ent]} and a placement)")
            linked[ent] = "placement"
        for a in f.get("outdoor") or []:
            uniq("outdoor", a["id"])
        for room_id, count in auto_by_room.items():
            if count:
                warns.append(
                    f"room '{room_id}': {count} entity field(s) are null and get bound automatically from area "
                    f"'{rooms[room_id]['area_id']}' — set an entity id or 'none' for a predictable result"
                )
        if f["background"] is not None:
            warns.append(
                f"floor '{f['id']}': background image '{f['background']['image_id']}' must exist (neonplan_upload_background_image)"
            )
    return errors, warns


def _ha_refs(b: dict) -> tuple[list[str], list[str]]:
    """Errors for `area_id`s missing from HA's area registry and entity ids missing from HA's states."""
    try:
        areas = {a.get("area_id") for a in ha.get_area_registry() or [] if isinstance(a, dict)}
        states = {s.get("entity_id") for s in ha.get_states() or [] if isinstance(s, dict)}
    except Exception as e:  # reported as a check that could not run
        raise _NeonError("ha_unreachable", f"Could not read HA areas/states to check references ({type(e).__name__}).") from None
    errors: list[str] = []
    for f in b["floors"]:
        for r in f["rooms"]:
            if r["area_id"] and r["area_id"] not in areas:
                errors.append(f"room '{r['id']}': area_id '{r['area_id']}' is not in HA's area registry")
        for a in f.get("outdoor") or []:
            if a.get("area_id") and a["area_id"] not in areas:
                errors.append(f"outdoor '{a['id']}': area_id '{a['area_id']}' is not in HA's area registry")
    refs: list[tuple[str, str]] = []
    _entity_refs(b, "", refs)
    missing = sorted({(path, ent) for path, ent in refs if ent not in states})
    for path, ent in missing[:50]:
        errors.append(f"{path}: entity '{ent}' does not exist in Home Assistant")
    if len(missing) > 50:
        errors.append(f"... and {len(missing) - 50} more missing entities")
    return errors, []


def _validate(building: Any, *, check_ha: bool) -> dict:
    errors = _structure(building)
    warnings: list[str] = []
    if not errors:
        lint_errors, warnings = _lint(building)
        errors.extend(lint_errors)
        if check_ha:
            ha_errors, _ = _ha_refs(building)
            errors.extend(ha_errors)
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "summary": _summary(building) if isinstance(building, dict) else None,
    }


# --- JSON Patch (RFC 6902) ------------------------------------------------------


def _pointer(path: Any) -> list[str]:
    if not isinstance(path, str) or (path and not path.startswith("/")):
        raise ValueError(f"path {path!r} is not a JSON Pointer (must be '' or start with '/')")
    if path == "":
        return []
    return [p.replace("~1", "/").replace("~0", "~") for p in path[1:].split("/")]


def _walk(doc: Any, segments: list[str]) -> Any:
    for t in segments:
        doc = _child(doc, t)
    return doc


def _index(container: list, segment: str, *, allow_end: bool) -> int:
    if segment == "-" and allow_end:
        return len(container)
    if not re.fullmatch(r"0|[1-9][0-9]*", segment):
        raise ValueError(f"'{segment}' is not an array index")
    i = int(segment)
    if i > len(container) or (i == len(container) and not allow_end):
        raise ValueError(f"index {i} is out of range (length {len(container)})")
    return i


def _child(doc: Any, segment: str) -> Any:
    if isinstance(doc, dict):
        if segment not in doc:
            raise ValueError(f"member '{segment}' does not exist")
        return doc[segment]
    if isinstance(doc, list):
        return doc[_index(doc, segment, allow_end=False)]
    raise ValueError(f"cannot descend into a {type(doc).__name__} at '{segment}'")


def _add(doc: Any, segments: list[str], value: Any) -> Any:
    if not segments:
        return value
    parent = _walk(doc, segments[:-1])
    last = segments[-1]
    if isinstance(parent, dict):
        parent[last] = value
    elif isinstance(parent, list):
        parent.insert(_index(parent, last, allow_end=True), value)
    else:
        raise ValueError(f"cannot add into a {type(parent).__name__}")
    return doc


def _remove(doc: Any, segments: list[str]) -> tuple[Any, Any]:
    if not segments:
        raise ValueError("cannot remove the whole document")
    parent = _walk(doc, segments[:-1])
    last = segments[-1]
    if isinstance(parent, dict):
        if last not in parent:
            raise ValueError(f"member '{last}' does not exist")
        return doc, parent.pop(last)
    if isinstance(parent, list):
        return doc, parent.pop(_index(parent, last, allow_end=False))
    raise ValueError(f"cannot remove from a {type(parent).__name__}")


def _apply_patch(doc: Any, operations: Any) -> Any:
    """Apply RFC 6902 operations to a copy of `doc`; raise `invalid_patch` naming the failing operation."""
    if not isinstance(operations, list) or not operations:
        raise _NeonError("invalid_patch", "operations must be a non-empty list of JSON Patch operations.")
    doc = copy.deepcopy(doc)
    for i, op in enumerate(operations):
        try:
            if not isinstance(op, dict):
                raise ValueError("operation is not an object")
            kind = op.get("op")
            segments = _pointer(op.get("path"))
            if kind in ("add", "replace", "test") and "value" not in op:
                raise ValueError(f"'{kind}' needs a value")
            if kind == "add":
                doc = _add(doc, segments, copy.deepcopy(op["value"]))
            elif kind == "remove":
                doc, _ = _remove(doc, segments)
            elif kind == "replace":
                _walk(doc, segments)  # must exist
                doc, _ = _remove(doc, segments) if segments else (doc, None)
                doc = _add(doc, segments, copy.deepcopy(op["value"]))
            elif kind in ("move", "copy"):
                source = _pointer(op.get("from"))
                if kind == "move" and segments[: len(source)] == source and len(segments) > len(source):
                    raise ValueError("cannot move a value into one of its own children")
                value = copy.deepcopy(_walk(doc, source))
                if kind == "move":
                    doc, _ = _remove(doc, source)
                doc = _add(doc, segments, value)
            elif kind == "test":
                if _walk(doc, segments) != op["value"]:
                    raise ValueError("test failed: the value differs")
            else:
                raise ValueError(f"unknown op {kind!r} (add, remove, replace, move, copy, test)")
        except ValueError as e:
            raise _NeonError("invalid_patch", f"operation {i}: {e}; nothing was written.", operation=i) from None
    return doc


# --- write pipeline -------------------------------------------------------------


def _norm(x: Any) -> Any:
    if isinstance(x, bool) or x is None or isinstance(x, str):
        return x
    if isinstance(x, (int, float)):
        return round(float(x), 6)
    if isinstance(x, dict):
        return {k: _norm(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_norm(v) for v in x]
    return x


def _differences(saved: Any, sent: Any, path: str, out: list[str]) -> None:
    """Paths where `saved` does not contain what was `sent` (the server may add defaults)."""
    if len(out) >= 10:
        return
    if isinstance(sent, dict):
        if not isinstance(saved, dict):
            out.append(path or "/")
            return
        for k, v in sent.items():
            if k not in saved:
                out.append(f"{path}/{k}")
            else:
                _differences(saved[k], v, f"{path}/{k}", out)
    elif isinstance(sent, list):
        if not isinstance(saved, list) or len(saved) != len(sent):
            out.append(path or "/")
            return
        for i, (a, b) in enumerate(zip(saved, sent, strict=True)):
            _differences(a, b, f"{path}/{i}", out)
    elif _norm(saved) != _norm(sent):
        out.append(path or "/")


def _save(plan: str, building: Any, live: dict, *, overwrite: bool, expected_revision: int | None) -> dict:
    """Validate, guard, snapshot, save and read back one plan; raise `_NeonError` before any write on a problem."""
    report = _validate(building, check_ha=True)
    if not report["valid"]:
        raise _NeonError(
            "validation_failed",
            "The plan has errors; nothing was written.",
            errors=report["errors"],
            warnings=report["warnings"],
        )
    revision = live.get("revision")
    had_floors = bool(live["building"].get("floors"))
    if had_floors and not overwrite:
        raise _NeonError(
            "plan_not_empty",
            "The plan already has floors; pass overwrite=true to replace it (a restore point is taken first), "
            "or use neonplan_patch_plan for a change.",
            current=_summary(live["building"]),
            revision=revision,
        )
    if expected_revision is not None and revision != expected_revision:
        raise _NeonError(
            "revision_conflict",
            "The plan was saved since expected_revision was read; nothing was written. Read it again.",
            expected_revision=expected_revision,
            current_revision=revision,
        )
    snapshot_taken = False
    if had_floors:
        _ws("history/snapshot", plan=plan)
        snapshot_taken = True
    again = _get_building(plan)
    if again.get("revision") != revision:
        raise _NeonError(
            "revision_conflict",
            "Someone saved the plan while nexus was preparing the save; nothing was written.",
            expected_revision=revision,
            current_revision=again.get("revision"),
            snapshot_taken=snapshot_taken,
        )
    try:
        result = _ws("building/save", timeout=_SAVE_TIMEOUT, plan=plan, building=building)
    except _NeonError as e:
        if e.code == "invalid_request":
            raise _NeonError(
                "invalid_building",
                f"The integration's schema rejected the plan; nothing was saved: {e.message}",
                snapshot_taken=snapshot_taken,
            ) from None
        raise
    new_revision = result.get("revision") if isinstance(result, dict) else None
    after = _get_building(plan)
    diffs: list[str] = []
    _differences(after["building"], building, "", diffs)
    return {
        "saved": True,
        "plan": plan,
        "revision_before": revision,
        "revision": new_revision,
        "snapshot_taken": snapshot_taken,
        "verified": not diffs and after.get("revision") == new_revision,
        "differences": diffs,
        "summary": _summary(after["building"]),
        "warnings": report["warnings"],
    }


# --- read tools -------------------------------------------------------------------

_PlanParam = Annotated[
    str, Field(description="Plan id from `neonplan_list_plans`; `main` (default) is the plan every install has.")
]


def _optional(fetch, project) -> Any:
    """One `get_status` section projected by `project`; an integration error stays local to it."""
    try:
        value = fetch()
    except _NeonError as e:
        if e.code == "neonplan_not_installed":
            raise
        return e.as_dict()
    return project(value if isinstance(value, dict) else {})


@mcp.tool(annotations=read("Get NeonPlan 3D status"))
def get_status() -> dict:
    """Report whether NeonPlan 3D is installed and loaded, its version, license, packs, plans and open screens.

    Reads the `neonplan3d` config entry, then `neonplan3d/building/get`
    (version), `license/get` (only whether a license is active), `packs/list`,
    `plans/list` and `screens/list` (panels and cards open right now — an open
    editor with unsaved changes would overwrite a save made through nexus).
    A section the integration refuses holds `{"error", ...}`. Changes nothing.

    Use when: before any other `neonplan_*` tool, or checking that no editor
    is open before `neonplan_save_plan` or `neonplan_patch_plan`.
    Not for: the plan itself — use `neonplan_get_plan`; HACS updates — use
    `hacs_get_hacs_repository`.
    Returns: `{"installed", "loaded", "config_entry", "version",
    "version_warning", "license": {active, error}, "packs": [...], "plans":
    [...], "screens": [...], "screens_open"}`.
    Errors: `neonplan_not_installed` (with `config_entry` when an entry
    exists but is not loaded), `ha_unreachable`.
    """
    try:
        entries = ha.get_config_entries("neonplan3d") or []
    except Exception:  # the status still reports what the commands say
        entries = []
    entry = next((e for e in entries if isinstance(e, dict)), None)
    entry_info = {"state": entry.get("state"), "title": entry.get("title")} if entry else None
    try:
        live = _get_building(_MAIN_PLAN)
        lic = _optional(lambda: _ws("license/get"), lambda v: {"active": v.get("active") is True, "error": v.get("error")})
        packs = _optional(
            lambda: _ws("packs/list"),
            lambda v: [{k: p.get(k) for k in _PACK_KEYS if k in p} for p in v.get("packs") or [] if isinstance(p, dict)],
        )
        plans = _optional(lambda: _ws("plans/list"), lambda v: v.get("plans") or [])
        screens = _optional(
            lambda: _ws("screens/list"),
            lambda v: [{k: s.get(k) for k in _SCREEN_KEYS if k in s} for s in v.get("screens") or [] if isinstance(s, dict)],
        )
    except _NeonError as e:
        result = e.as_dict()
        result.update({"installed": entry is not None, "loaded": False, "config_entry": entry_info})
        return result
    screens_open = len(screens) if isinstance(screens, list) else None
    return {
        "installed": True,
        "loaded": True,
        "config_entry": entry_info,
        "version": live.get("version"),
        "version_warning": _version_warning(live.get("version")),
        "license": lic,
        "packs": packs,
        "plans": plans,
        "screens": screens,
        "screens_open": screens_open,
    }


@mcp.tool(annotations=read("List NeonPlan 3D plans"))
def list_plans() -> dict:
    """List the NeonPlan 3D floor plans with their revision and floor, room and furniture counts.

    Calls `neonplan3d/plans/list`: the plans in their order (`main` always
    exists, others have ids like `p0123456789`) and the plans in the bin.
    Changes nothing.

    Use when: choosing the `plan` for the other `neonplan_*` tools.
    Not for: one plan's content — use `neonplan_get_plan`.
    Returns: `{"plans": [{id, name, icon, revision, floors, rooms,
    furniture, ...}], "trash": [...], "locate_plan"}`.
    Errors: `neonplan_not_installed`, `ha_unreachable`, `ws_error`.
    """
    try:
        result = _ws("plans/list")
    except _NeonError as e:
        return e.as_dict()
    return result if isinstance(result, dict) else {"plans": result}


@mcp.tool(annotations=read("Get NeonPlan 3D plan"))
def get_plan(
    plan: _PlanParam = _MAIN_PLAN,
    summary_only: Annotated[
        bool,
        Field(description="True returns floors, rooms (with area_id), openings and bound entities instead of the full building."),
    ] = False,
) -> dict:
    """Read one NeonPlan 3D plan: the whole building object with its revision, or a compact summary.

    Calls `neonplan3d/building/get`. The full building is the object
    `neonplan_save_plan` takes and `neonplan_patch_plan` paths point into
    (units metres, points `[x, z]`, `z` grows down the drawing). The summary
    lists per floor its rooms (id, name, area_id, corner count), openings
    (id, type, room_id, edge, offset) and the entity ids the plan references.
    Changes nothing.

    Use when: reading the plan before an edit; keep `revision` for
    `expected_revision`.
    Not for: the list of plans — use `neonplan_list_plans`; checking a plan
    — use `neonplan_validate_plan`.
    Returns: `{"plan", "revision", "version", "version_warning", "building"}`
    or, with `summary_only`, `{"plan", "revision", "version",
    "version_warning", "totals", "floors": [...], "entities": [...]}`.
    Errors: `invalid_plan` (no I/O), `plan_not_found`,
    `neonplan_not_installed`, `ha_unreachable`.
    """
    try:
        live = _get_building(_check_plan_id(plan))
    except _NeonError as e:
        return e.as_dict()
    base = {
        "plan": live.get("plan", plan),
        "revision": live.get("revision"),
        "version": live.get("version"),
        "version_warning": _version_warning(live.get("version")),
    }
    building = live["building"]
    if not summary_only:
        return {**base, "building": building}
    floors = []
    for f in building.get("floors") or []:
        if not isinstance(f, dict):
            continue
        floors.append(
            {
                "id": f.get("id"),
                "name": f.get("name"),
                "elevation": f.get("elevation"),
                "height": f.get("height"),
                "rooms": [
                    {"id": r.get("id"), "name": r.get("name"), "area_id": r.get("area_id"), "corners": len(r.get("points") or [])}
                    for r in f.get("rooms") or [] if isinstance(r, dict)
                ],
                "openings": [
                    {k: o.get(k) for k in ("id", "type", "room_id", "edge", "offset", "width")}
                    for o in f.get("openings") or [] if isinstance(o, dict)
                ],
                "furniture": len(f.get("furniture") or []),
                "placements": len(f.get("placements") or []),
            }
        )
    refs: list[tuple[str, str]] = []
    _entity_refs(building, "", refs)
    return {**base, "totals": _summary(building), "floors": floors, "entities": sorted({e for _, e in refs})}


@mcp.tool(annotations=read("Validate NeonPlan 3D plan"))
def validate_plan(
    building: Annotated[
        dict[str, Any] | None,
        Field(description="Building object to check (as `neonplan_get_plan` returns it); omit to check the stored plan."),
    ] = None,
    plan: _PlanParam = _MAIN_PLAN,
    check_ha: Annotated[
        bool,
        Field(description="True (default) also checks every area_id and entity id against the live Home Assistant."),
    ] = True,
) -> dict:
    """Check a NeonPlan 3D building for structure, broken references and unknown HA areas or entities without saving it.

    Checks what the integration's schema requires (keys, types, main
    ranges), then what it does not check: duplicate ids, an opening's
    `room_id`/`edge` and `offset ± width/2` inside that wall, repeated first
    corner, degenerate rooms, entity fields that are not an entity id/null/
    `none`, furniture and pins outside every room; with `check_ha`, each
    `area_id` against HA's area registry and each entity id against HA's
    states. Warnings cover `null` fields NeonPlan binds automatically from
    the area. Writes nothing.

    Use when: before `neonplan_save_plan`, or auditing the stored plan.
    Not for: saving — `neonplan_save_plan` runs this itself.
    Returns: `{"valid", "errors": [...], "warnings": [...], "summary"}`
    plus `plan`/`revision` when the stored plan was checked.
    Errors: `invalid_plan`, `plan_not_found`, `neonplan_not_installed`,
    `ha_unreachable`.
    Limits: ranges the schema checks but this does not are caught by the
    server at save time (`invalid_building`).
    """
    try:
        extra: dict[str, Any] = {}
        if building is None:
            live = _get_building(_check_plan_id(plan))
            building = live["building"]
            extra = {"plan": plan, "revision": live.get("revision")}
        return {**_validate(building, check_ha=check_ha), **extra}
    except _NeonError as e:
        return e.as_dict()


@mcp.tool(annotations=read("List NeonPlan 3D restore points"))
def list_restore_points(plan: _PlanParam = _MAIN_PLAN) -> dict:
    """List a NeonPlan 3D plan's restore points (at most 20 kept), newest first.

    Calls `neonplan3d/history/list` (admin). The integration adds one by
    itself on a save when the last is older than 10 minutes; the
    `neonplan_*` write tools take one before every save. Changes nothing.

    Use when: picking `snapshot_id` for `neonplan_restore_restore_point`.
    Not for: nexus's Node-RED backups — use `nodered_list_backups`; HA
    backups — use `supervisor_list_backups`.
    Returns: `{"plan", "restore_points": [{id, revision, saved_at,
    saved_at_iso, floors, rooms, furniture}], "count", "max"}`.
    Errors: `invalid_plan`, `plan_not_found`, `admin_required`,
    `neonplan_not_installed`, `ha_unreachable`.
    """
    try:
        result = _ws("history/list", plan=_check_plan_id(plan))
    except _NeonError as e:
        if e.code == "not_found":
            return {"error": "plan_not_found", "plan": plan}
        return e.as_dict()
    points = [dict(s) for s in (result or {}).get("snapshots") or [] if isinstance(s, dict)]
    for p in points:
        if _is_num(p.get("saved_at")):
            p["saved_at_iso"] = datetime.fromtimestamp(p["saved_at"], UTC).isoformat(timespec="seconds")
    return {"plan": plan, "restore_points": points, "count": len(points), "max": _HISTORY_MAX}


def _area_entities(area_id: str) -> list[dict]:
    """Enabled entity-registry rows in `area_id` (own area, else the device's)."""
    devices = {d.get("id"): d.get("area_id") for d in ha.get_device_registry() or [] if isinstance(d, dict)}
    rows = []
    for e in ha.get_entity_registry() or []:
        if not isinstance(e, dict) or e.get("disabled_by"):
            continue
        area = e.get("area_id") or devices.get(e.get("device_id"))
        if area == area_id:
            rows.append(e)
    return rows


def _role(entity_id: str, device_class: Any) -> str | None:
    domain = entity_id.split(".", 1)[0]
    if domain == "cover":
        if device_class in _GARAGE_COVER_CLASSES:
            return "garage_cover"
        if device_class in _BLIND_CLASSES:
            return "window_cover"
        return None
    if domain == "binary_sensor":
        if device_class in _WINDOW_CONTACT_CLASSES:
            return "window_contact"
        if device_class == "door":
            return "door_contact"
        if device_class == "garage_door":
            return "garage_contact"
        return None
    if domain == "sensor":
        key = _CLIMATE_SENSOR_CLASSES.get(device_class)
        return f"climate_{key}" if key else None
    return {
        "light": "lamp",
        "switch": "lamp_switch",
        "climate": "climate_device",
        "media_player": "media",
        "vacuum": "vacuum",
        "camera": "camera",
        "lock": "lock",
    }.get(domain)


_ROLE_TARGETS = {
    "window_cover": "openings (type window).cover — one blind group may serve every window",
    "garage_cover": "openings (type garage).cover",
    "window_contact": "openings (type window).contact (contact2 for the second leaf)",
    "door_contact": "openings (type door).contact",
    "garage_contact": "openings (type garage).contact",
    "lock": "openings (type door).lock",
    "lamp": "furniture lamp_* .entity (a light in placements turns into a lamp unless pin: true)",
    "lamp_switch": "furniture lamp_* .entity, only when the switch drives a light",
    "climate_temperature": "rooms.climate.temperature",
    "climate_humidity": "rooms.climate.humidity",
    "climate_co2": "rooms.climate.co2",
    "climate_device": "furniture radiator or ac_unit .entity",
    "media": "furniture tv_wall or tv_board .entity",
    "vacuum": "furniture robot_vacuum .entity, and rooms.cleaning for a per-room clean script",
    "camera": "placements pin with fov/reach/tilt",
}


@mcp.tool(annotations=read("Suggest NeonPlan 3D entity bindings"))
def suggest_bindings(
    room_id: Annotated[str, Field(description="Room id from `neonplan_get_plan` (summary_only lists them).")],
    plan: _PlanParam = _MAIN_PLAN,
) -> dict:
    """Propose which entities of a NeonPlan room's HA area fit which plan field, without writing anything.

    Finds the room in the plan, reads HA's entity and device registries and
    states, keeps the enabled entities of the room's `area_id` and groups
    them by role: blinds → `opening.cover`, window/door contacts →
    `opening.contact`, lights → lamp furniture `entity`, temperature/
    humidity/CO2 sensors → `room.climate`, climate → radiator/ac_unit,
    media players → tv_wall, vacuums → robot_vacuum, cameras → pins, locks
    → `opening.lock`. Each entity says where the plan already uses it.

    Use when: filling entity fields explicitly instead of leaving `null`
    (automatic) before `neonplan_patch_plan`.
    Not for: the area's entities in general — use `areas_get_area_entities`.
    Returns: `{"plan", "room": {id, name, area_id}, "openings": [...],
    "suggestions": {role: {"target", "entities": [{entity_id, name,
    device_class, used_at}]}}, "unassigned": [entity ids]}`.
    Errors: `invalid_plan`, `plan_not_found`, `room_not_found`,
    `room_has_no_area`, `neonplan_not_installed`, `ha_unreachable`.
    """
    try:
        live = _get_building(_check_plan_id(plan))
    except _NeonError as e:
        return e.as_dict()
    building = live["building"]
    found = None
    for f in building.get("floors") or []:
        for r in (f.get("rooms") or []) if isinstance(f, dict) else []:
            if isinstance(r, dict) and r.get("id") == room_id:
                found = (f, r)
    if found is None:
        return {"error": "room_not_found", "message": f"No room {room_id!r} in plan {plan!r}.", "room_id": room_id}
    floor, room = found
    area_id = room.get("area_id")
    if not area_id:
        return {"error": "room_has_no_area", "message": f"Room {room_id!r} has no area_id to look entities up in."}
    try:
        rows = _area_entities(area_id)
        states = {s.get("entity_id"): s for s in ha.get_states() or [] if isinstance(s, dict)}
    except Exception as e:
        return {"error": "ha_unreachable", "message": f"Could not read HA registries ({type(e).__name__})."}
    used: dict[str, list[str]] = {}
    refs: list[tuple[str, str]] = []
    _entity_refs(building, "", refs)
    for path, ent in refs:
        used.setdefault(ent, []).append(path)
    suggestions: dict[str, dict] = {}
    unassigned: list[str] = []
    for row in sorted(rows, key=lambda r: r.get("entity_id") or ""):
        eid = row.get("entity_id")
        if not isinstance(eid, str):
            continue
        attrs = (states.get(eid) or {}).get("attributes") or {}
        device_class = attrs.get("device_class") or row.get("device_class") or row.get("original_device_class")
        role = _role(eid, device_class)
        if role is None:
            unassigned.append(eid)
            continue
        group = suggestions.setdefault(role, {"target": _ROLE_TARGETS[role], "entities": []})
        group["entities"].append(
            {
                "entity_id": eid,
                "name": attrs.get("friendly_name") or row.get("name") or row.get("original_name"),
                "device_class": device_class,
                "used_at": used.get(eid, [])[:5],
            }
        )
    openings = [
        {k: o.get(k) for k in ("id", "type", "edge", "offset", "cover", "contact", "lock")}
        for o in floor.get("openings") or [] if isinstance(o, dict) and o.get("room_id") == room_id
    ]
    return {
        "plan": plan,
        "revision": live.get("revision"),
        "room": {"id": room_id, "name": room.get("name"), "area_id": area_id},
        "openings": openings,
        "suggestions": suggestions,
        "unassigned": unassigned,
    }


# --- write tools --------------------------------------------------------------------

_ExpectedRevision = Annotated[
    int | None,
    Field(ge=0, description="`revision` from your last read; if the plan's revision differs, nothing is written."),
]
_Force = Annotated[
    bool,
    Field(description="True writes even when the integration is not version 3.0.x (format checked by you)."),
]


@mcp.tool(annotations=destructive("Save NeonPlan 3D plan", idempotent=True))
def save_plan(
    building: Annotated[
        dict[str, Any],
        Field(description="The complete building object (version, settings, floors, ...); it replaces the stored one."),
    ],
    plan: _PlanParam = _MAIN_PLAN,
    overwrite: Annotated[
        bool, Field(description="True allows replacing a plan that already has floors; false refuses that.")
    ] = False,
    expected_revision: _ExpectedRevision = None,
    force: _Force = False,
) -> dict:
    """Replace a whole NeonPlan 3D plan after validating it, with a restore point and a read-back check.

    Runs `neonplan_validate_plan` (any error blocks), refuses a plan with
    floors unless `overwrite`, checks `expected_revision`, takes a restore
    point (`history/snapshot`) when the plan has floors, reads the revision
    again, sends `neonplan3d/building/save` and reads the plan back. The
    integration itself has no revision check: an editor open with unsaved
    changes (see `neonplan_get_status` screens) can still overwrite this.

    Use when: building a plan from scratch or replacing it entirely.
    Not for: small edits — use `neonplan_patch_plan`; undo — use
    `neonplan_restore_restore_point`.
    Returns: `{"saved": true, "plan", "revision_before", "revision",
    "snapshot_taken", "verified", "differences", "summary", "warnings"}`.
    Errors: `validation_failed` (with `errors`), `plan_not_empty`,
    `revision_conflict`, `invalid_building` (the server's schema refused),
    `neonplan_version_unsupported`, `invalid_plan`, `plan_not_found`,
    `admin_required`, `neonplan_not_installed`, `ha_unreachable`.
    """
    try:
        plan = _check_plan_id(plan)
        live = _get_building(plan)
        _require_supported(live.get("version"), force)
        return _save(plan, building, live, overwrite=overwrite, expected_revision=expected_revision)
    except _NeonError as e:
        return e.as_dict()


@mcp.tool(annotations=destructive("Patch NeonPlan 3D plan", idempotent=False))
def patch_plan(
    operations: Annotated[
        list[dict[str, Any]],
        Field(
            description=(
                "JSON Patch (RFC 6902) operations on the building, applied in order, e.g. "
                "`[{\"op\": \"replace\", \"path\": \"/floors/0/openings/2/cover\", \"value\": \"cover.blind_kitchen\"}, "
                "{\"op\": \"add\", \"path\": \"/floors/0/furniture/-\", \"value\": {...}}]`; ops add, remove, "
                "replace, move, copy, test."
            )
        ),
    ],
    plan: _PlanParam = _MAIN_PLAN,
    expected_revision: _ExpectedRevision = None,
    force: _Force = False,
    overwrite: Annotated[
        bool,
        Field(description="True allows an operation on path '' (the whole plan) when the plan has floors, as save_plan."),
    ] = False,
) -> dict:
    """Change part of a NeonPlan 3D plan with JSON Patch operations, then validate and save it like `neonplan_save_plan`.

    Reads the plan, applies the operations to a copy (a failing operation,
    including a failed `test`, stops everything), validates the result
    (errors block) and saves it with the same guards as
    `neonplan_save_plan`: `expected_revision`, restore point, revision
    re-check, read-back. An operation on path `""` (the whole plan) needs
    `overwrite=true` when the plan has floors, like `neonplan_save_plan`.

    Use when: binding an entity, adding a lamp or window, renaming a room —
    any edit of an existing plan.
    Not for: a new plan from scratch — use `neonplan_save_plan`; checking
    paths first — read with `neonplan_get_plan`.
    Returns: like `neonplan_save_plan`, plus `"operations"` (count).
    Errors: `invalid_patch` (with `operation` index), `validation_failed`,
    `plan_not_empty` (whole-plan operation without overwrite), `revision_conflict`, `invalid_building`, `neonplan_version_unsupported`,
    `invalid_plan`, `plan_not_found`, `admin_required`,
    `neonplan_not_installed`, `ha_unreachable`.
    """
    try:
        plan = _check_plan_id(plan)
        live = _get_building(plan)
        _require_supported(live.get("version"), force)
        if expected_revision is not None and live.get("revision") != expected_revision:
            raise _NeonError(
                "revision_conflict",
                "The plan was saved since expected_revision was read; nothing was written. Read it again.",
                expected_revision=expected_revision,
                current_revision=live.get("revision"),
            )
        whole = any(isinstance(op, dict) and op.get("path") == "" for op in operations or [])
        patched = _apply_patch(live["building"], operations)
        result = _save(plan, patched, live, overwrite=overwrite or not whole, expected_revision=expected_revision)
    except _NeonError as e:
        return e.as_dict()
    result["operations"] = len(operations)
    return result


@mcp.tool(annotations=destructive("Create NeonPlan 3D restore point", idempotent=False))
def create_restore_point(plan: _PlanParam = _MAIN_PLAN, force: _Force = False) -> dict:
    """Keep the current state of a NeonPlan 3D plan as a restore point; the oldest is dropped beyond 20.

    Calls `neonplan3d/history/snapshot` (admin) and lists the restore points
    afterwards. The integration keeps at most 20, so with 20 already the
    oldest is lost.

    Use when: before editing the plan in the NeonPlan editor or importing.
    Not for: before `neonplan_save_plan` or `neonplan_patch_plan` — they take
    one themselves; going back — use `neonplan_restore_restore_point`.
    Returns: `{"created": true, "plan", "restore_point": {id, revision,
    saved_at, ...}, "count", "dropped_oldest"}`.
    Errors: `neonplan_version_unsupported`, `invalid_plan`,
    `plan_not_found`, `admin_required`, `neonplan_not_installed`,
    `ha_unreachable`.
    """
    try:
        plan = _check_plan_id(plan)
        live = _get_building(plan)
        _require_supported(live.get("version"), force)
        before = (_ws("history/list", plan=plan) or {}).get("snapshots") or []
        _ws("history/snapshot", plan=plan)
        after = (_ws("history/list", plan=plan) or {}).get("snapshots") or []
    except _NeonError as e:
        return e.as_dict()
    return {
        "created": True,
        "plan": plan,
        "restore_point": after[0] if after else None,
        "count": len(after),
        "dropped_oldest": len(before) >= _HISTORY_MAX,
    }


@mcp.tool(annotations=destructive("Restore NeonPlan 3D restore point", idempotent=False))
def restore_restore_point(
    snapshot_id: Annotated[str, Field(description="Restore point id from `neonplan_list_restore_points`.")],
    plan: _PlanParam = _MAIN_PLAN,
    expected_revision: _ExpectedRevision = None,
    force: _Force = False,
) -> dict:
    """Bring a NeonPlan 3D plan back to a restore point; the integration keeps the current state as a new restore point.

    Checks the restore point exists (`history/list`) and `expected_revision`,
    then calls `neonplan3d/history/restore` (admin), which snapshots the
    current building and saves the restored one as a new revision.

    Use when: undoing a `neonplan_save_plan` or `neonplan_patch_plan` that
    went wrong.
    Not for: Supervisor backups — use `supervisor_restore_backup`.
    Returns: `{"restored": true, "plan", "snapshot_id", "revision_before",
    "revision", "summary"}`.
    Errors: `invalid_snapshot_id` (no I/O), `restore_point_not_found`,
    `revision_conflict`, `neonplan_version_unsupported`, `invalid_plan`,
    `plan_not_found`, `admin_required`, `neonplan_not_installed`,
    `ha_unreachable`.
    """
    if not _is_id(snapshot_id):
        return {"error": "invalid_snapshot_id", "message": "Use an id from neonplan_list_restore_points."}
    try:
        plan = _check_plan_id(plan)
        live = _get_building(plan)
        _require_supported(live.get("version"), force)
        points = (_ws("history/list", plan=plan) or {}).get("snapshots") or []
        if not any(isinstance(p, dict) and p.get("id") == snapshot_id for p in points):
            raise _NeonError("restore_point_not_found", f"No restore point {snapshot_id!r} in plan {plan!r}.")
        if expected_revision is not None and live.get("revision") != expected_revision:
            raise _NeonError(
                "revision_conflict",
                "The plan was saved since expected_revision was read; nothing was restored.",
                expected_revision=expected_revision,
                current_revision=live.get("revision"),
            )
        result = _ws("history/restore", timeout=_SAVE_TIMEOUT, plan=plan, snapshot_id=snapshot_id)
        after = _get_building(plan)
    except _NeonError as e:
        return e.as_dict()
    return {
        "restored": True,
        "plan": plan,
        "snapshot_id": snapshot_id,
        "revision_before": live.get("revision"),
        "revision": result.get("revision") if isinstance(result, dict) else after.get("revision"),
        "summary": _summary(after["building"]),
    }


def _image_roots() -> list[Path]:
    return [
        Path(os.getenv("HA_CONFIG_PATH", "/config")).resolve(),
        Path(os.getenv("NEXUS_MEDIA_PATH", "/media")).resolve(),
    ]


def _sniff(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return None


_MAX_IMAGE_BYTES = (_MAX_IMAGE_CHARS - len("data:image/jpeg;base64,")) // 4 * 3


def _image_bytes(path: str | None, data_base64: str | None) -> tuple[bytes, str]:
    if (path is None) == (data_base64 is None):
        raise _NeonError("invalid_source", "Pass exactly one of path or data_base64.")
    if path is not None:
        try:
            src = Path(path).resolve()
        except (OSError, RuntimeError):
            raise _NeonError("invalid_path", f"Cannot resolve {path!r}.") from None
        roots = _image_roots()
        if not any(src.is_relative_to(r) for r in roots):
            raise _NeonError("path_not_allowed", f"The file must be under one of {[str(r) for r in roots]}.")
        if src.suffix.lower() not in _IMAGE_TYPES:
            raise _NeonError("unsupported_image", "Only .png, .jpg, .jpeg and .webp files are accepted.")
        try:
            size = src.stat().st_size
            if size > _MAX_IMAGE_BYTES:
                raise _NeonError("image_too_large", f"{size} bytes; at most {_MAX_IMAGE_BYTES} fit the 8 MiB data URL limit.")
            raw = src.read_bytes()
        except FileNotFoundError:
            raise _NeonError("file_not_found", f"No file {path!r}.") from None
        except OSError as e:
            raise _NeonError("invalid_path", f"Cannot read {path!r} ({type(e).__name__}).") from None
    else:
        text = re.sub(r"^data:image/[a-z]+;base64,", "", str(data_base64).strip())
        if len(text) > _MAX_IMAGE_CHARS:
            raise _NeonError("image_too_large", "The base64 data is longer than the 8 MiB data URL limit.")
        try:
            raw = base64.b64decode(text, validate=True)
        except ValueError:
            raise _NeonError("invalid_base64", "data_base64 is not valid base64.") from None
    kind = _sniff(raw)
    if kind is None:
        raise _NeonError("unsupported_image", "The content is not a png, jpeg or webp image.")
    return raw, kind


@mcp.tool(annotations=destructive("Upload NeonPlan 3D background image", idempotent=True))
def upload_background_image(
    image_id: Annotated[str, Field(description="Id to store the image under, e.g. `ground_floor_scan` (A-Z a-z 0-9 _ - ., 1-64).")],
    path: Annotated[
        str | None,
        Field(description="png/jpeg/webp file under /config (or /media when mounted), e.g. `/config/www/plan.png`."),
    ] = None,
    data_base64: Annotated[
        str | None, Field(description="The image as base64 (a `data:image/...;base64,` prefix is accepted) instead of path.")
    ] = None,
    overwrite: Annotated[bool, Field(description="True replaces an image already stored under image_id.")] = False,
    force: _Force = False,
) -> dict:
    """Store a png, jpeg or webp image as a NeonPlan 3D background (floor tracing) image and return its image_id.

    Reads the file (or decodes the base64), checks the real content type
    and the 8 MiB data URL limit, refuses an existing `image_id` unless
    `overwrite` (`image/get`), and sends `neonplan3d/image/set` (admin).
    Images are shared by all plans. WARNING: the integration deletes images
    no plan references at its next start, so reference it right away in a
    floor's `background` (`{"image_id", "x", "z", "width", "opacity",
    "rotation"}`) with `neonplan_patch_plan`.

    Use when: tracing a scanned floor plan in the NeonPlan editor.
    Not for: Lovelace or card images — use `card_builder_upload_media`.
    Returns: `{"uploaded": true, "image_id", "type", "bytes",
    "data_url_chars", "replaced", "warning"}`.
    Errors: `invalid_image_id`, `invalid_source`, `invalid_path`,
    `path_not_allowed`, `file_not_found`, `unsupported_image`,
    `image_too_large`, `invalid_base64` (all without I/O to HA),
    `image_exists`, `neonplan_version_unsupported`, `admin_required`,
    `neonplan_not_installed`, `ha_unreachable`.
    """
    if not _is_id(image_id):
        return {"error": "invalid_image_id", "message": "image_id must be 1-64 characters of A-Z a-z 0-9 _ - ."}
    try:
        raw, kind = _image_bytes(path, data_base64)
        data_url = f"data:image/{kind};base64," + base64.b64encode(raw).decode("ascii")
        if len(data_url) > _MAX_IMAGE_CHARS:
            raise _NeonError("image_too_large", f"The data URL has {len(data_url)} characters; at most {_MAX_IMAGE_CHARS}.")
        live = _get_building(_MAIN_PLAN)
        _require_supported(live.get("version"), force)
        exists = True
        try:
            _ws("image/get", timeout=_IMAGE_TIMEOUT, image_id=image_id)
        except _NeonError as e:
            if e.code != "not_found":
                raise
            exists = False
        if exists and not overwrite:
            raise _NeonError("image_exists", f"An image {image_id!r} is stored already; pass overwrite=true to replace it.")
        _ws("image/set", timeout=_IMAGE_TIMEOUT, image_id=image_id, data=data_url)
    except _NeonError as e:
        return e.as_dict()
    return {
        "uploaded": True,
        "image_id": image_id,
        "type": kind,
        "bytes": len(raw),
        "data_url_chars": len(data_url),
        "replaced": exists,
        "warning": "Not referenced by any plan yet: the integration deletes it at its next start unless a floor's "
        "background.image_id points to it.",
    }
