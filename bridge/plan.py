"""The plan Claude returns, and the checks it must pass before anything spawns."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import geo
from .catalog import AIR_DEFENCE_CLASSES, MANOEUVRE_CLASSES, Catalog
from .scenario import Scenario

MAX_SWEEP_LEG_KM = 250


@dataclass
class GroundGroup:
    name: str
    type: str
    count: int
    bearing_deg: float
    distance_km: float
    heading_deg: float
    reason: str
    role: str = "position"  # "position" (holds its spot) or "reserve" (tanks/APCs sent to detected enemy ground units)
    lat: float = 0.0
    lng: float = 0.0


@dataclass
class FighterTasking:
    type: str
    count: int
    airbase: str
    role: str  # "intercept" (ground alert, scrambled on contact) or "sweep" (launched at start)
    loadout: str
    sweep_route: list[dict]  # [{"bearing_deg", "distance_km"}] from the objective
    reason: str

    def route_points(self, scenario: Scenario) -> list[tuple[float, float]]:
        o = scenario.objective
        return [geo.project(o.lat, o.lng, p["bearing_deg"], p["distance_km"] * 1000) for p in self.sweep_route]


@dataclass
class Plan:
    summary: str
    ground_groups: list[GroundGroup] = field(default_factory=list)
    fighters: list[FighterTasking] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict) -> "Plan":
        return cls(
            summary=data.get("summary", ""),
            ground_groups=[GroundGroup(**g) for g in data.get("ground_groups", [])],
            fighters=[FighterTasking(**f) for f in data.get("fighters", [])],
        )

    def cost(self, catalog: Catalog) -> int:
        total = sum(catalog[g.type].price * g.count for g in self.ground_groups if g.type in catalog)
        return total + sum(catalog[f.type].price * f.count for f in self.fighters if f.type in catalog)


def json_schema(scenario: Scenario, catalog: Catalog, airbase_names: list[str]) -> dict:
    """Structured-output schema. Enums keep Claude to types and airbases that exist."""
    ground_types = catalog.names("groundunit")
    air_types = [n for n in catalog.names("aircraft") if catalog[n].cls == "fighter"]  # defence flies no ground attack
    point = {
        "type": "object",
        "properties": {"bearing_deg": {"type": "number"}, "distance_km": {"type": "number"}},
        "required": ["bearing_deg", "distance_km"],
        "additionalProperties": False,
    }
    ground = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Short unique group name, e.g. 'SA11-North'"},
            "type": {"type": "string", "enum": ground_types or [""]},
            "count": {"type": "integer"},
            "bearing_deg": {"type": "number", "description": "True bearing from the objective centre"},
            "distance_km": {"type": "number", "description": "Distance from the objective centre"},
            "heading_deg": {"type": "number", "description": "Direction the group faces"},
            "role": {"type": "string", "enum": ["position", "reserve"],
                     "description": "position: holds this spot. reserve (tanks and APCs only): waits here, then drives to detected enemy ground units"},
            "reason": {"type": "string", "description": "One line: why here"},
        },
        "required": ["name", "type", "count", "bearing_deg", "distance_km", "heading_deg", "role", "reason"],
        "additionalProperties": False,
    }
    fighter = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": air_types or [""]},
            "count": {"type": "integer"},
            "airbase": {"type": "string", "enum": airbase_names or [""]},
            "role": {"type": "string", "enum": ["intercept", "sweep"]},
            "loadout": {"type": "string"},
            "sweep_route": {"type": "array", "items": point, "description": "Waypoints for a sweep; empty for intercept"},
            "reason": {"type": "string"},
        },
        "required": ["type", "count", "airbase", "role", "loadout", "sweep_route", "reason"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Two or three sentences on the defensive concept"},
            "ground_groups": {"type": "array", "items": ground},
            "fighters": {"type": "array", "items": fighter},
        },
        "required": ["summary", "ground_groups", "fighters"],
        "additionalProperties": False,
    }


def validate(plan: Plan, scenario: Scenario, catalog: Catalog, red_airbases: dict[str, tuple[float, float]]) -> tuple[list[str], list[str]]:
    """Returns (errors, warnings). Errors go back to Claude for repair; warnings are only logged.

    Also fills in each ground group's lat/lng.
    """
    errors: list[str] = []
    warnings: list[str] = []
    rules = scenario.rules
    o = scenario.objective

    names = [g.name for g in plan.ground_groups]
    for dup in {n for n in names if names.count(n) > 1}:
        errors.append(f"Group name '{dup}' is used more than once")

    used: dict[str, int] = {}
    for g in plan.ground_groups:
        if g.type not in catalog:
            errors.append(f"{g.name}: unknown type '{g.type}'")
            continue
        item = catalog[g.type]
        if item.category != "groundunit":
            errors.append(f"{g.name}: '{g.type}' is not a ground unit")
        if g.count < 1:
            errors.append(f"{g.name}: count must be at least 1")
        if item.is_template and g.count != 1:
            errors.append(f"{g.name}: '{g.type}' spawns a whole battery, so count must be 1 (use separate groups)")
        if g.role not in ("position", "reserve"):
            errors.append(f"{g.name}: role must be 'position' or 'reserve'")
        elif g.role == "reserve" and item.cls not in MANOEUVRE_CLASSES:
            errors.append(f"{g.name}: only tanks and APCs can be a reserve, not {item.cls}")
        if g.distance_km < 0 or g.distance_km > rules.max_distance_from_objective_km:
            errors.append(f"{g.name}: {g.distance_km} km is outside the allowed 0-{rules.max_distance_from_objective_km} km")
        g.lat, g.lng = geo.project(o.lat, o.lng, g.bearing_deg % 360, g.distance_km * 1000)
        used[g.type] = used.get(g.type, 0) + g.count

    for f in plan.fighters:
        if f.type not in catalog or catalog[f.type].cls != "fighter":
            errors.append(f"Fighter tasking: unknown aircraft type '{f.type}'")
            continue
        if f.count < 1:
            errors.append(f"{f.type} at {f.airbase}: count must be at least 1")
        if f.airbase not in red_airbases:
            errors.append(f"{f.type}: '{f.airbase}' is not a Red airbase")
        if f.loadout not in catalog[f.type].a2a_loadouts:
            errors.append(f"{f.type}: loadout '{f.loadout}' is not one of {catalog[f.type].a2a_loadouts[:4]}")
        if f.role == "sweep":
            if not f.sweep_route:
                errors.append(f"{f.type} sweep from {f.airbase}: needs at least one waypoint")
            for p in f.sweep_route:
                if p["distance_km"] > MAX_SWEEP_LEG_KM:
                    errors.append(f"{f.type} sweep: waypoint {p['distance_km']} km out is beyond {MAX_SWEEP_LEG_KM} km")
        used[f.type] = used.get(f.type, 0) + f.count

    if scenario.mode == "inventory":
        allowed: dict[str, int] = {}
        bases: dict[str, set[str]] = {}
        for e in scenario.inventory:
            allowed[e.type] = allowed.get(e.type, 0) + e.count
            if e.base:
                bases.setdefault(e.type, set()).add(e.base)
        for t, n in used.items():
            if n > allowed.get(t, 0):
                errors.append(f"{t}: plan uses {n}, inventory has {allowed.get(t, 0)}")
        for t, n in allowed.items():
            if used.get(t, 0) < n:
                warnings.append(f"{t}: {n - used.get(t, 0)} of {n} left unused")
        for f in plan.fighters:
            if f.type in bases and f.airbase not in bases[f.type]:
                errors.append(f"{f.type}: inventory bases it at {sorted(bases[f.type])}, not {f.airbase}")

    if scenario.budget_points is not None:
        total = plan.cost(catalog)
        if total > scenario.budget_points:
            errors.append(f"Plan costs {total} points, budget is {scenario.budget_points}")

    covering = [
        g for g in plan.ground_groups
        if g.type in catalog and catalog[g.type].cls in AIR_DEFENCE_CLASSES
        and catalog[g.type].engagement_range_m >= g.distance_km * 1000 + o.radius_m * 0.5
    ]
    if not covering and any(i.cls in AIR_DEFENCE_CLASSES for i in catalog.items.values()):
        warnings.append("No air-defence group's engagement ring covers the objective")

    return errors, warnings
