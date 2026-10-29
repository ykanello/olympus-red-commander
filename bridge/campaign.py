"""Offensive campaign: Red buys a combined-arms force, drives it from a staging area to the target and takes it.

Claude plans the opening, then reviews the Red-only picture every few minutes and after big events and gives
new orders or buys reinforcements. Between reviews the deterministic loop fights, as in the defence mode:
alert fighters against detected aircraft, alert ground-attack aircraft against detected ground units, artillery
fire missions. A referee, which unlike Red sees everything, decides when the target is taken.
"""

from __future__ import annotations

import concurrent.futures
import fnmatch
import logging
import math
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from . import geo, missiles
from .catalog import ARTILLERY_CLASSES, Catalog
from .commander import GROUND_CATEGORIES, AlertSlot, Commander, SpawnedGroup
from .olympus import Unit
from .plan import FighterTasking, clean_text, cleaned
from .planner import Planner
from .scenario import Scenario

log = logging.getLogger(__name__)

MAX_POINT_KM = 400  # no route point, firing position or sweep waypoint further than this from the target
ARRIVED_M = 500  # a group this close to the end of its route has arrived
COLUMN_SPACING_M = 35  # between vehicles of a convoy at the staging area
SLOT_SPACING_M = 250  # between convoys at the staging area
GROUND_ROLES = ("convoy", "artillery")
REVIEW_EVENTS = {"group_lost", "arrived", "hold_started", "hold_broken", "target_taken", "campaign_lost", "defence_threatened",
                 "missiles_ready", "spawn_failed"}
WAITING, FIRING, READY, EMPTY, GONE, ABSENT = (
    "waiting for the mission's strike", "firing", "ready", "no missiles left", "destroyed", "not in the mission (yet)")
MISSION_ORDERS = "mission orders"  # status of a mission group Claude has not given an order yet
AIR_ROLES = ("sweep", "intercept", "cas")

CAMPAIGN_PROMPT = """You are the Red commander running an offensive in a DCS World mission. You plan and give orders; the DCS AI fights.

Goal: take the target zone. Red takes it when Red ground units hold the zone for hold_minutes with no enemy ground units inside. Aircraft cannot take it; only ground forces can.

How your plan is used:
- Every position is a bearing and distance from the target centre. The brief gives the staging area and the HQ airbase the same way, and that is your map.
- Convoys: each is one DCS group of up to max_group_units vehicles, types may be mixed. A convoy spawns at the staging area, sets off depart_min minutes after the start and drives its route, keeping to roads where it can. Where the route ends is where it stops. A convoy with an empty route waits at the staging area for your orders: a reserve.
- Give convoys their own short-range air defence (classes sam_short and aaa) so they are not defenceless from the air. Fixed SAM batteries cannot move and are not offered.
- Artillery batteries drive to their firing position, then fire on detected enemy ground units within range, never on a target close to Red ground units. Place them so their range reaches where your convoys will fight.
- All aircraft fly from the HQ airbase. sweep: launches at the start and flies its route weapons free. intercept: waits on alert and is scrambled in pairs against detected enemy aircraft near Red ground forces or the target. cas: waits on alert and is scrambled in pairs against detected enemy ground units and helicopters near Red ground forces or the target. Loadouts only from the lists on the card: air_to_air_loadouts for sweep and intercept, ground_attack_loadouts for cas.
- Red only knows what its own units detect. Early-warning radars see aircraft, not vehicles; ground units see a few km around them. Do not assume enemy positions you have not been told.
- Points you do not spend stay available. At later reviews you can buy reinforcements, which spawn at the staging area or the HQ.

Events in the situation report are about your own Red groups unless they say otherwise (group_lost means one of your groups was destroyed). Enemy units appear only under detected_enemy.

Reviews: every replan_minutes, and after big events (a group lost, a group arriving, the hold starting or breaking), you get a situation report and return the same structure. summary is your assessment in two or three sentences. orders change what existing ground groups do: move (with a new route), hold (stop where it is) or return (drive back to the staging area). convoys, artillery and air are reinforcements bought from points_left; leave them empty when none are needed. Return empty lists when nothing should change.

Choose types for the job and the budget, and say why in each reason. Keep every reason to one line."""

COMMAND_PROMPT = """You are the Red commander in a DCS World mission. You give orders to forces that are already in the mission; the DCS AI fights.

Goals:
1. Hold the defend_zones. Do not strip a defend zone of its defenders for the attack while it is threatened.
2. Take the target zone. Red takes it when Red ground units hold the zone for hold_minutes with no enemy ground units inside. Aircraft cannot take it; only ground forces can, so Red wins only if ground groups go in.
Holding the defend zones does not take every group. Groups that are not in a defend zone, or not needed to reinforce a threatened one, are your attack force: order them to move on the target, unless you have a concrete reason to wait (say so in the reason). "hold" on the attack force review after review means the target is never taken. Artillery whose range does not reach the target should move up until it does.

Your forces:
- Ground: only the groups in red_ground_groups, which are already in the mission. No ground units can be bought or spawned, so a group lost is gone for good. A group keeps doing what the mission gave it (status "mission orders": its own route, or standing where it is) until you order it. Orders: move (with a route; it drives the route, keeping to roads where it can, and stops at the last point), hold (stop where it is) or return (drive back to where it started).
- Artillery groups fire on detected enemy ground units within range, never on a target close to Red ground units. Move them so their range covers where your ground forces will fight.
- Everything else Red has (air defence, radars, missile launchers, ships) is run by the mission. You do not command it.
- Aircraft: only the airframes each airbase's warehouse holds (airbases[].airframes; at reviews, situation.airframes_left). Each air tasking names its airbase and spends that many airframes from it for good. sweep: launches at once and flies its route weapons free. intercept: waits on alert and is scrambled in pairs against detected enemy aircraft near the target, a defend zone or your ground groups. cas: the same against detected enemy ground units and helicopters. Loadouts only from the card: air_to_air_loadouts for sweep and intercept, ground_attack_loadouts for cas. cas is the only way your aircraft attack ground units and helicopters, so while the airbases hold ground-attack aircraft keep a cas pair on alert to support your ground forces. Pairs on alert launch only as the threat needs (rules: intercept_pairs_per_enemy_aircraft; cas, one pair per enemy group), so add alert taskings only when situation.on_alert runs low: every airframe you task is spent from its warehouse. Keep airframes back for later; the enemy will come more than once.
- Red only knows what its own units detect. Early-warning radars see aircraft, not vehicles; ground units see a few km around them. Do not assume enemy positions you have not been told.

Every position is a bearing and distance from the target centre; the brief gives the defend zones and airbases the same way.

Events in the situation report are about your own Red groups unless they say otherwise (group_lost means one of your groups was destroyed). Enemy units appear only under detected_enemy. defence_threatened means a detected enemy ground unit is inside a defend zone. spawn_failed means DCS would not create an air tasking's aircraft: nothing was lost, the airframes are back in airframes_left, and that airbase, type and loadout (situation.spawn_failures) cannot be tasked again; use another loadout or airbase.

You are asked for the opening orders, then for a review every replan_minutes and after big events (a group lost or arriving, a defend zone threatened, the hold starting or breaking). Each time return the same structure: summary is your assessment in two or three sentences, orders are for your ground groups, air adds air taskings from the airframes left. Return empty lists when nothing should change. Keep every reason to one line."""


MISSILE_NOTES = """

Missiles: missile_batteries are ballistic missile launchers already in the mission (missile_intent says what they are for). A battery takes your orders only once its status is ready: until then the mission fires its own strike with it. strikes fire a ready battery at one of strike_targets (by name) or at a detected enemy ground unit ("enemy <id>"), rounds being the missiles it fires. Missiles left are few and gone once fired, they need several minutes to arrive, and you get no damage report: judge from what Red sees afterwards (for example whether enemy aircraft still come from an airbase you struck) before striking again or turning to another target. A strike is refused if Red ground units are close to the aim point."""


def campaign_prompt(scenario: Scenario) -> str:
    camp = scenario.campaign
    notes = MISSILE_NOTES if scenario.missiles else ""
    if camp.notes:
        notes += "\n\ncommander_notes in the brief are your commander's intelligence and intent for this mission: plan by them."
    if not camp.spawn_ground:
        return COMMAND_PROMPT + notes
    prompt = CAMPAIGN_PROMPT + notes
    if camp.command_groups:
        prompt += "\n\nYou also command ground groups that were already in the mission; they are in red_ground_groups from the start. Give them orders like any other group; return sends them back to where they started."
    if scenario.warehouses:
        prompt += "\n\nAircraft cost no points but come only from the airframes each airbase's warehouse holds. Each air tasking names its airbase and spends that many airframes from it for good."
    return prompt


# ---------- plan ----------

@dataclass
class Element:
    type: str
    count: int


@dataclass
class Convoy:
    name: str
    elements: list[Element]
    depart_min: float
    route: list[dict]  # [{"bearing_deg", "distance_km"}] from the target
    reason: str

    @property
    def units(self) -> int:
        return sum(e.count for e in self.elements)


@dataclass
class Battery:
    name: str
    type: str
    count: int
    depart_min: float
    firing_position: dict  # {"bearing_deg", "distance_km"} from the target
    reason: str


@dataclass
class Order:
    group: str
    action: str  # move | hold | return
    route: list[dict]
    reason: str


@dataclass
class Strike:
    group: str
    target: str  # a strike target's name, or "enemy <unit id>"
    rounds: int
    reason: str


@dataclass
class CampaignPlan:
    summary: str
    convoys: list[Convoy] = field(default_factory=list)
    artillery: list[Battery] = field(default_factory=list)
    air: list[FighterTasking] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    strikes: list[Strike] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict, hq: str) -> "CampaignPlan":
        return cls(
            summary=clean_text(data.get("summary", "")),
            convoys=[Convoy(**{**cleaned(c), "elements": [Element(**e) for e in c.get("elements", [])]}) for c in data.get("convoys", [])],
            artillery=[Battery(**cleaned(b)) for b in data.get("artillery", [])],
            air=[FighterTasking(**{"sweep_route": [], **cleaned(a), "airbase": a.get("airbase") or hq}) for a in data.get("air", [])],
            orders=[Order(**{"route": [], "reason": "", **cleaned(o)}) for o in data.get("orders", [])],
            strikes=[Strike(**{"reason": "", **cleaned(st)}) for st in data.get("strikes", [])],
        )

    def cost(self, catalog: Catalog) -> int:
        price = lambda t: catalog[t].price if t in catalog else 0
        return (sum(price(e.type) * e.count for c in self.convoys for e in c.elements)
                + sum(price(b.type) * b.count for b in self.artillery)
                + sum(price(a.type) * a.count for a in self.air))

    def names(self) -> list[str]:
        return [c.name for c in self.convoys] + [b.name for b in self.artillery]


def to_latlng(scenario: Scenario, p: dict) -> tuple[float, float]:
    o = scenario.objective
    return geo.project(o.lat, o.lng, p["bearing_deg"] % 360, p["distance_km"] * 1000)


def convoy_types(catalog: Catalog) -> list[str]:
    return [n for n in catalog.names("groundunit") if not catalog[n].is_template and catalog[n].cls not in ARTILLERY_CLASSES]


def campaign_schema(catalog: Catalog, review: bool = False, ground: bool = True, orders: bool = False,
                    airbases: list[str] | None = None, missile_groups: list[str] | None = None) -> dict:
    """Structured-output schema for the opening plan, or for a review.

    ground: offer convoys and artillery to buy. orders: let the opening give orders (to the mission's own groups).
    airbases: each air tasking names one of these (warehouse mode); without, all aircraft fly from the HQ.
    missile_groups: the mission's missile groups Claude can order strikes from.
    The review schema is the same all mission (group names are checked by validate_campaign, not listed here),
    so it does not change the request from one review to the next and the cached brief stays valid.
    """
    point = {
        "type": "object",
        "properties": {"bearing_deg": {"type": "number"}, "distance_km": {"type": "number"}},
        "required": ["bearing_deg", "distance_km"],
        "additionalProperties": False,
    }
    route = {"type": "array", "items": point, "description": "Waypoints from the target centre; the last is where the group stops"}
    convoy = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Short unique name, e.g. 'Armour-1'"},
            "elements": {"type": "array", "items": {
                "type": "object",
                "properties": {"type": {"type": "string", "enum": convoy_types(catalog) or [""]}, "count": {"type": "integer"}},
                "required": ["type", "count"], "additionalProperties": False}},
            "depart_min": {"type": "number", "description": "Minutes after it spawns that it sets off"},
            "route": {**route, "description": "Empty: waits at the staging area for orders"},
            "reason": {"type": "string"},
        },
        "required": ["name", "elements", "depart_min", "route", "reason"],
        "additionalProperties": False,
    }
    battery = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "type": {"type": "string", "enum": [n for n in catalog.names("groundunit") if catalog[n].cls in ARTILLERY_CLASSES] or [""]},
            "count": {"type": "integer"},
            "depart_min": {"type": "number"},
            "firing_position": point,
            "reason": {"type": "string"},
        },
        "required": ["name", "type", "count", "depart_min", "firing_position", "reason"],
        "additionalProperties": False,
    }
    air = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": catalog.names("aircraft") or [""]},
            "count": {"type": "integer"},
            "role": {"type": "string", "enum": list(AIR_ROLES)},
            **({"airbase": {"type": "string", "enum": airbases}} if airbases else {}),
            "loadout": {"type": "string"},
            "sweep_route": {"type": "array", "items": point, "description": "Waypoints for a sweep; empty otherwise"},
            "reason": {"type": "string"},
        },
        "required": ["type", "count", "role", *(["airbase"] if airbases else []), "loadout", "sweep_route", "reason"],
        "additionalProperties": False,
    }
    properties = {
        "summary": {"type": "string", "description": "Two or three sentences: the concept, or at a review your assessment"},
        **({"convoys": {"type": "array", "items": convoy}, "artillery": {"type": "array", "items": battery}} if ground else {}),
        "air": {"type": "array", "items": air},
    }
    if review or orders:
        properties["orders"] = {"type": "array", "items": {
            "type": "object",
            "properties": {
                "group": {"type": "string", "description": "Name of one of your red_ground_groups"},
                "action": {"type": "string", "enum": ["move", "hold", "return"]},
                "route": {**route, "description": "New route for move; empty for hold and return"},
                "reason": {"type": "string"},
            },
            "required": ["group", "action", "route", "reason"],
            "additionalProperties": False,
        }}
    if missile_groups:
        properties["strikes"] = {"type": "array", "items": {
            "type": "object",
            "properties": {
                "group": {"type": "string", "enum": missile_groups},
                "target": {"type": "string", "description": "Name of one of strike_targets, or 'enemy <id>' for a detected enemy ground unit"},
                "rounds": {"type": "integer", "description": "Missiles to fire"},
                "reason": {"type": "string"},
            },
            "required": ["group", "target", "rounds", "reason"],
            "additionalProperties": False,
        }}
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def validate_campaign(plan: CampaignPlan, scenario: Scenario, catalog: Catalog, points: float | None,
                      existing: list[str] | None = None, airframes: dict[str, dict[str, int]] | None = None,
                      opening: bool | None = None, situation: dict | None = None) -> tuple[list[str], list[str]]:
    """Returns (errors, warnings).

    points: None when there is no budget. existing: labels of ground groups already in the field (at reviews, or the
    mission's own groups at the opening). airframes: {airbase: {type: count}} left in the warehouses, if they set the limit.
    situation: the report the plan answers, for checking strikes against the missile batteries and detected enemy units.
    """
    errors: list[str] = []
    warnings: list[str] = []
    camp = scenario.campaign
    existing = existing or []
    opening = not existing if opening is None else opening

    if not camp.spawn_ground and (plan.convoys or plan.artillery):
        errors.append("No ground units can be bought or spawned in this mission: give orders to your red_ground_groups instead")

    def check_points(what: str, pts: list[dict]) -> None:
        for p in pts:
            if not 0 <= p["distance_km"] <= MAX_POINT_KM:
                errors.append(f"{what}: point {p['distance_km']} km from the target is outside 0-{MAX_POINT_KM} km")

    names = plan.names()
    for dup in sorted({n for n in names if names.count(n) > 1 or n in existing}):
        errors.append(f"Group name '{dup}' is already used")

    for c in plan.convoys:
        if not c.elements:
            errors.append(f"{c.name}: a convoy needs at least one vehicle")
        for e in c.elements:
            item = catalog.items.get(e.type)
            if item is None or item.category != "groundunit":
                errors.append(f"{c.name}: unknown ground unit '{e.type}'")
            elif item.is_template:
                errors.append(f"{c.name}: '{e.type}' is a fixed battery and cannot move in a convoy")
            elif item.cls in ARTILLERY_CLASSES:
                errors.append(f"{c.name}: put artillery '{e.type}' in the artillery list, not in a convoy")
            if e.count < 1:
                errors.append(f"{c.name}: count of '{e.type}' must be at least 1")
        if c.units > camp.max_group_units:
            errors.append(f"{c.name}: {c.units} vehicles, a convoy takes at most {camp.max_group_units}")
        if c.depart_min < 0:
            errors.append(f"{c.name}: depart_min cannot be negative")
        check_points(c.name, c.route)

    for b in plan.artillery:
        item = catalog.items.get(b.type)
        if item is None or item.cls not in ARTILLERY_CLASSES:
            errors.append(f"{b.name}: '{b.type}' is not artillery")
        if not 1 <= b.count <= camp.max_group_units:
            errors.append(f"{b.name}: count must be 1-{camp.max_group_units}")
        if b.depart_min < 0:
            errors.append(f"{b.name}: depart_min cannot be negative")
        check_points(b.name, [b.firing_position])

    for a in plan.air:
        item = catalog.items.get(a.type)
        if item is None or item.category != "aircraft":
            errors.append(f"Air tasking: unknown aircraft type '{a.type}'")
            continue
        if a.count < 1:
            errors.append(f"{a.type}: count must be at least 1")
        if a.role not in AIR_ROLES:
            errors.append(f"{a.type}: role must be one of {list(AIR_ROLES)}")
        allowed = item.ground_attack_loadouts if a.role == "cas" else item.a2a_loadouts
        if a.loadout not in allowed:
            kind = "ground_attack_loadouts" if a.role == "cas" else "air_to_air_loadouts"
            errors.append(f"{a.type} {a.role}: loadout '{a.loadout}' is not in its {kind} {allowed[:4]}")
        if a.role == "sweep":
            if not a.sweep_route:
                errors.append(f"{a.type} sweep: needs at least one waypoint")
            check_points(f"{a.type} sweep", a.sweep_route)

    for f in (situation or {}).get("spawn_failures", []):
        for a in plan.air:
            if (a.airbase, a.type, a.loadout) == (f["airbase"], f["type"], f["loadout"]):
                errors.append(f"{a.type} with '{a.loadout}' did not spawn at {a.airbase} before; use another loadout or airbase")

    if airframes is not None:
        wanted: Counter = Counter()
        for a in plan.air:
            if a.airbase not in airframes:
                errors.append(f"{a.type}: '{a.airbase}' is not one of the airbases {sorted(airframes)}")
            else:
                wanted[(a.airbase, a.type)] += a.count
        for (base, t), n in sorted(wanted.items()):
            left = airframes[base].get(t, 0)
            if n > left:
                errors.append(f"{base} has {left} {t} left, the plan flies {n} from there")

    for o in plan.orders:
        if o.group not in existing:
            errors.append(f"Order for '{o.group}': no such group in the field")
        if o.action not in ("move", "hold", "return"):
            errors.append(f"Order for '{o.group}': action must be move, hold or return")
        if o.action == "move":
            if not o.route:
                errors.append(f"Order for '{o.group}': move needs a route")
            check_points(f"Order for '{o.group}'", o.route)

    if plan.strikes:
        errors += check_strikes(plan.strikes, scenario, situation or {})

    cost = plan.cost(catalog)
    if points is not None and cost > points:
        errors.append(f"This costs {cost} points, only {points:g} are available")

    if opening and camp.spawn_ground:
        r_km = scenario.objective.radius_m / 1000
        if not any(c.route and c.route[-1]["distance_km"] <= r_km for c in plan.convoys):
            warnings.append("No convoy's route ends inside the target zone")
    return errors, warnings


def strike_point(scenario: Scenario, target: str, situation: dict) -> tuple[float, float] | None:
    """Where a strike target is: a named strike target, or a detected enemy unit ("enemy <id>") of the situation report."""
    named = {t.name: t for t in scenario.missiles.targets}
    if target in named:
        return named[target].lat, named[target].lng
    word, _, ident = target.partition(" ")
    if word == "enemy" and ident.isdigit():
        for e in situation.get("detected_enemy", []):
            if e["id"] == int(ident):
                return to_latlng(scenario, e["position"])
    return None


def check_strikes(strikes: list[Strike], scenario: Scenario, situation: dict) -> list[str]:
    errors = []
    batteries = {b["group"]: b for b in situation.get("missile_batteries", [])}
    used = Counter()
    for st in strikes:
        used[st.group] += 1
        b = batteries.get(st.group)
        if b is None or b["status"] != READY:
            errors.append(f"Strike by {st.group}: the battery is {b['status'] if b else 'not available'}, not ready")
            continue
        if st.rounds < 1:
            errors.append(f"Strike by {st.group}: rounds must be at least 1")
        elif isinstance(b.get("missiles_left"), int) and st.rounds > b["missiles_left"]:
            errors.append(f"Strike by {st.group}: {st.rounds} rounds, it has {b['missiles_left']} missiles left")
        point = strike_point(scenario, st.target, situation)
        if point is None:
            errors.append(f"Strike by {st.group}: '{st.target}' is neither a strike target nor a detected enemy ('enemy <id>')")
            continue
        reach = b.get("range_km") or 0
        km = geo.distance_m(*to_latlng(scenario, b["position"]), *point) / 1000
        if reach and km > reach:
            errors.append(f"Strike by {st.group}: {st.target} is {km:.0f} km away, beyond its {reach:g} km range")
    for group, n in used.items():
        if n > 1:
            errors.append(f"{group} can take only one strike order at a time")
    return errors


# ---------- Claude ----------

class CampaignPlanner(Planner):
    def brief(self, scenario: Scenario, catalog: Catalog, red_airbases: dict, staging: tuple[float, float]) -> dict:
        """The part of the request that stays the same all mission."""
        o, camp, w = scenario.objective, scenario.campaign, scenario.warehouses
        where = lambda lat, lng: {"bearing_deg": round(geo.bearing_deg(o.lat, o.lng, lat, lng)),
                                  "distance_km": round(geo.distance_m(o.lat, o.lng, lat, lng) / 1000, 1)}
        brief = {
            "target": {"name": o.name, "radius_m": o.radius_m, "threat_axis_deg": o.threat_axis_deg,
                       "note": "All positions are bearing/distance from this target's centre."},
            "hq_airbase": {"name": camp.hq, **where(*red_airbases[camp.hq])},
        }
        if camp.spawn_ground:
            brief["staging_area"] = {"name": camp.staging_name or camp.hq, **where(*staging)}
        if w:
            brief["airbases"] = [{"name": n, **where(*red_airbases[n]), "airframes": w.stock[n]} for n in sorted(w.stock)]
        else:
            brief["other_red_airbases"] = [{"name": n, **where(*ll)} for n, ll in sorted(red_airbases.items()) if n != camp.hq]
        if camp.notes:
            brief["commander_notes"] = camp.notes
        if camp.defend:
            brief["defend_zones"] = [{"name": z.name, "radius_m": z.radius_m, **where(z.lat, z.lng)} for z in camp.defend]
        if scenario.missiles:
            brief["missile_intent"] = scenario.missiles.intent
            brief["strike_targets"] = [{"name": t.name, **where(t.lat, t.lng)} for t in scenario.missiles.targets]
        brief["rules"] = {
            **({"alert_pairs_go_first_after": scenario.rules.priority_air_targets} if scenario.rules.priority_air_targets else {}),
            "hold_minutes": camp.hold_minutes, "replan_minutes": camp.replan_minutes,
            "max_group_units": camp.max_group_units, "protect_radius_km": camp.protect_radius_km,
            "scramble_when_contact_within_km": scenario.rules.scramble_when_contact_within_km,
            "artillery_fire": scenario.rules.artillery_fire, "no_fire_near_friendly_m": scenario.rules.no_fire_near_friendly_m,
            "scramble_pair_size": scenario.rules.scramble_pair_size, "sweep_altitude_ft": scenario.rules.sweep_altitude_ft,
            "intercept_pairs_per_enemy_aircraft": round(1 / scenario.rules.enemy_aircraft_per_pair, 2),
            **({"max_airborne_pairs_per_role": scenario.rules.max_airborne_pairs} if scenario.rules.max_airborne_pairs else {}),
        }
        if camp.spawn_ground:
            brief["budget_points"] = scenario.budget_points
        brief["menu"] = [c for c in catalog.cards() if catalog[c["type"]].category == "aircraft" or camp.spawn_ground and (
            c["type"] in convoy_types(catalog) or catalog[c["type"]].cls in ARTILLERY_CLASSES)]
        return brief

    def _schema(self, scenario: Scenario, catalog: Catalog, review: bool) -> dict:
        camp, w = scenario.campaign, scenario.warehouses
        return campaign_schema(catalog, review=review, ground=camp.spawn_ground, orders=bool(camp.command_groups),
                               airbases=sorted(w.stock) if w else None,
                               missile_groups=scenario.missiles.groups if scenario.missiles else None)

    def plan_campaign(self, scenario: Scenario, catalog: Catalog, red_airbases: dict,
                      staging: tuple[float, float], situation: dict | None = None) -> tuple[CampaignPlan, list[str]]:
        """situation: what Red has at the start, when it commands groups already in the mission."""
        brief = self.brief(scenario, catalog, red_airbases, staging)
        camp, w = scenario.campaign, scenario.warehouses
        if camp.spawn_ground:
            extra = {"instruction": "Buy your opening force from the menu within budget_points (price per vehicle or airframe) and plan the opening of the offensive."}
        else:
            extra = {"instruction": "Plan the opening: orders for your ground groups and air taskings from the airframes the airbases hold."}
        if situation is not None:
            extra = {"situation": situation, **extra}
        labels = [g["name"] for g in (situation or {}).get("red_ground_groups", [])]
        airframes = (situation or {}).get("airframes_left", w.stock if w else None)

        def check(data):
            plan = CampaignPlan.from_json(data, camp.hq)
            return (plan, *validate_campaign(plan, scenario, catalog, scenario.budget_points if camp.spawn_ground else None,
                                             labels, airframes, opening=True, situation=situation))
        return self.solve(brief, self._schema(scenario, catalog, review=False), check, campaign_prompt(scenario), extra=extra,
                          what="opening plan")

    def review(self, scenario: Scenario, catalog: Catalog, red_airbases: dict, staging: tuple[float, float],
               report: dict) -> CampaignPlan:
        brief = self.brief(scenario, catalog, red_airbases, staging)
        what = "Reinforcements must fit within situation.points_left." if scenario.campaign.spawn_ground else \
            "Air taskings must fit within situation.airframes_left."
        extra = {"situation": report, "instruction": f"Review the situation and give your orders. {what}"}
        labels = [g["name"] for g in report["red_ground_groups"]]

        def check(data):
            plan = CampaignPlan.from_json(data, scenario.campaign.hq)
            return (plan, *validate_campaign(plan, scenario, catalog, report.get("points_left"), labels,
                                             report.get("airframes_left"), opening=False, situation=report))
        plan, _ = self.solve(brief, self._schema(scenario, catalog, review=True), check, campaign_prompt(scenario), extra=extra,
                             what="review")
        return plan


# ---------- execution ----------

def is_pattern(name: str) -> bool:
    return any(c in name for c in "*?[")


def matches(entry: str, group_name: str) -> bool:
    """A command_groups entry against a mission group name: case-insensitive, with * and ? wildcards."""
    return fnmatch.fnmatchcase(group_name.lower(), entry.lower())

@dataclass
class CampaignCommander(Commander):
    replanner: Callable[[dict], CampaignPlan] | None = None  # reviews; None: the opening plan runs to the end
    review_async: bool = True  # False in tests: review inside the tick
    points_left: float = 0.0
    started_at: float = 0.0
    hold_since: float | None = None
    outcome: str = ""  # "" | won | lost
    last_review: float = 0.0
    review_reasons: list = field(default_factory=list)
    assessments: list = field(default_factory=list)
    recent_events: list = field(default_factory=list)
    spawn_slot: int = 0
    airframes_left: dict = field(default_factory=dict)  # {airbase: {type: count}}, when the warehouses set the limit
    awaiting: list = field(default_factory=list)  # mission groups to command that are not (yet) in the mission
    threatened: list = field(default_factory=list)  # defend zones with detected enemy ground units inside
    batteries: dict = field(default_factory=dict)  # missile group -> {status, missiles_left, launched, last_launch, seen}
    _review: concurrent.futures.Future | None = None
    _executor: concurrent.futures.ThreadPoolExecutor | None = None

    @property
    def camp(self):
        return self.scenario.campaign

    @property
    def staging(self) -> tuple[float, float]:
        if self.camp.staging_lat is not None:
            return self.camp.staging_lat, self.camp.staging_lng
        return self.red_airbases[self.camp.hq]

    def _event(self, kind: str, **data) -> None:
        if kind in REVIEW_EVENTS:
            self.review_reasons.append(kind)
        self.recent_events = (self.recent_events + [{"event": kind, **{k: v for k, v in data.items() if k != "route"}}])[-20:]
        super()._event(kind, **data)

    def state(self) -> dict:
        return {**super().state(), "campaign": {
            "points_left": self.points_left, "started_at": self.started_at, "hold_since": self.hold_since,
            "outcome": self.outcome, "last_review": self.last_review, "assessments": self.assessments,
            "spawn_slot": self.spawn_slot, "airframes_left": self.airframes_left, "awaiting": self.awaiting,
            "threatened": self.threatened, "batteries": self.batteries}}

    def restore(self, state: dict) -> None:
        super().restore(state)
        for k, v in state.get("campaign", {}).items():
            setattr(self, k, v)

    def _return_airframes(self, airbase: str, tasking_type: str, count: int) -> None:
        if self.airframes_left:
            base = self.airframes_left.setdefault(airbase, {})
            base[tasking_type] = base.get(tasking_type, 0) + count

    def _labels(self) -> dict[str, SpawnedGroup]:
        return {g.label: g for g in self.groups.values() if g.role in GROUND_ROLES}

    # ----- the mission's own groups -----
    def take_command(self, units: dict[int, Unit]) -> None:
        """Start the mission: the warehouse stock and the mission's ground groups Red commands."""
        self.started_at = self.last_review = time.time()
        if self.scenario.warehouses:
            self.airframes_left = {base: dict(types) for base, types in self.scenario.warehouses.stock.items()}
        self.awaiting = [n for n in self.camp.command_groups if not is_pattern(n)]
        self._adopt(units)
        for pattern in filter(is_pattern, self.camp.command_groups):
            if not any(matches(pattern, g.name) for g in self.groups.values()):
                log.warning("No Red ground group in the mission matches '%s' (yet).", pattern)
        if self.scenario.missiles:
            first = WAITING if self.scenario.missiles.wait_for_mission_strike else READY
            self.batteries = {g: {"status": first, "missiles_left": None, "launched": 0, "last_launch": 0.0, "seen": False}
                              for g in self.scenario.missiles.groups}
            self._watch_missiles(units)
        if self.awaiting:
            log.warning("Not in the mission (yet): %s. They are taken under command if they appear, e.g. after a late activation.",
                        ", ".join(self.awaiting))

    def opening_report(self, units: dict[int, Unit]) -> dict:
        """What Red has at the start, for the opening plan."""
        self.take_command(units)
        return self.situation(units, ["opening"])

    def _adopt(self, units: dict[int, Unit]) -> None:
        """Take command of the listed mission groups that are in the mission now. Names match whatever the case; a name with
        * or ? is a pattern that takes every group it matches, including ones that appear later."""
        missile_groups = set(self.scenario.missiles.groups) if self.scenario.missiles else set()
        red = {u.group_name for u in units.values() if u.alive and u.coalition == self.scenario.coalition
               and u.category in GROUND_CATEGORIES and u.group_name} - set(self.groups) - missile_groups
        patterns = [n for n in self.camp.command_groups if is_pattern(n)]
        for name in sorted(red):
            entry = next((a for a in self.awaiting if matches(a, name)), None)
            if entry is None and ("_dismounted_" in name or not any(matches(p, name) for p in patterns)):
                continue  # the infantry DCS dismounts from a hit vehicle is not one of the groups the patterns mean
            if entry is not None:
                self.awaiting.remove(entry)
            members = [u for u in units.values() if u.group_name == name and u.alive and u.coalition == self.scenario.coalition
                       and u.category in GROUND_CATEGORIES]
            leader = self._leader(units, name)
            types = Counter(u.name for u in members)
            kinds = {t: self.catalog.describe(t) for t in types}
            guns = [t for t, k in kinds.items() if k.cls in ARTILLERY_CLASSES]
            main = guns[0] if guns else types.most_common(1)[0][0]
            g = SpawnedGroup(name, main, kinds[main].cls, leader.lat, leader.lng, role="artillery" if guns else "convoy",
                             label=name, status=MISSION_ORDERS)
            self.groups[name] = g
            self._event("command_taken", group=name, role=g.role, types=dict(types))

    # ----- spawning -----
    def execute(self, plan: CampaignPlan) -> None:
        units = self.client.get_units()
        self.take_command(units)
        self.points_left = (self.scenario.budget_points or 0) - plan.cost(self.catalog)
        o = self.scenario.objective
        self._event("plan", summary=plan.summary, cost=plan.cost(self.catalog), points_left=self.points_left)
        self._marker(o.lat, o.lng, f"Red target: {o.name}. {plan.summary}")
        if self.camp.spawn_ground:
            self._marker(*self.staging, f"Red staging area: {self.camp.staging_name or self.camp.hq}")
        for z in self.camp.defend:
            self._marker(z.lat, z.lng, f"Red holds: {z.name}")
        self._carry_out(plan)
        self.review_reasons.clear()

    def _carry_out(self, plan: CampaignPlan) -> None:
        for c in plan.convoys:
            types = [e.type for e in c.elements for _ in range(e.count)]
            self._spawn_column(c.name, types, "convoy", c.depart_min, [to_latlng(self.scenario, p) for p in c.route], c.reason)
        for b in plan.artillery:
            self._spawn_column(b.name, [b.type] * b.count, "artillery", b.depart_min,
                               [to_latlng(self.scenario, b.firing_position)], b.reason)
        for a in plan.air:
            if self.airframes_left:
                base = self.airframes_left.setdefault(a.airbase, {})
                base[a.type] = base.get(a.type, 0) - a.count
            if a.role == "sweep":
                self._launch_sweep(a)
            else:
                self.alerts.append(AlertSlot(a.type, a.airbase, a.loadout, a.count, role=a.role))
                self._event("alert", type=a.type, airbase=a.airbase, count=a.count, role=a.role, reason=a.reason)
        for order in plan.orders:
            self._order(order)
        for st in plan.strikes:
            self._strike(st)

    def _spawn_column(self, label: str, types: list[str], role: str, depart_min: float,
                      route: list[tuple[float, float]], reason: str) -> None:
        """Spawn a group at the staging area, lined up behind its own slot and facing the target."""
        o, rules = self.scenario.objective, self.scenario.rules
        s_lat, s_lng = self.staging
        heading = geo.bearing_deg(s_lat, s_lng, o.lat, o.lng)
        slot = self.spawn_slot
        self.spawn_slot += 1
        lat, lng = geo.project(s_lat, s_lng, heading + 90, ((slot % 7) - 3) * SLOT_SPACING_M)
        lat, lng = geo.project(lat, lng, heading + 180, (slot // 7) * (self.camp.max_group_units * COLUMN_SPACING_M + 200))
        units = []
        for i, t in enumerate(types):
            u_lat, u_lng = geo.project(lat, lng, heading + 180, i * COLUMN_SPACING_M)
            units.append({"unitType": t, "location": {"lat": u_lat, "lng": u_lng}, "heading": math.radians(heading),
                          "liveryID": "", "skill": rules.skill})
        requested = self._group_name(label)
        group_name = self._spawn_and_find(requested, lat, lng, lambda: self.client.spawn_ground(
            requested, units, coalition=self.scenario.coalition, country=self.scenario.country, spawn_points=0), radius_m=2000)
        if group_name is None:
            self._event("spawn_failed", name=label, role=role, types=dict(Counter(types)))
            return
        main = Counter(types).most_common(1)[0][0]
        g = SpawnedGroup(group_name, main, self.catalog[main].cls, lat, lng, role=role, label=label,
                         route=[list(p) for p in route], depart_at=time.time() + depart_min * 60, status="waiting")
        self.groups[group_name] = g
        leader = self._wait_for_group(group_name)
        if leader:
            self.client.set_roe(leader.id, "free")
            self.client.set_alarm_state(leader.id, "red")
        self._event("spawned", group=group_name, name=label, role=role, types=dict(Counter(types)), depart_min=depart_min, reason=reason)
        self._marker(lat, lng, f"{label} ({role}): {len(types)} vehicles. {reason}")

    def _set_off(self, g: SpawnedGroup, leader: Unit, route: list) -> None:
        g.route = [list(p) for p in route]
        g.status = "moving"
        self.client.set_follow_roads(leader.id, True)
        self.client.set_path(leader.id, [tuple(p) for p in route])

    def _order(self, order: Order) -> None:
        g = self._labels().get(order.group)
        leader = self._leader(self.client.get_units(), g.name) if g else None
        if leader is None:
            log.warning("Order for %s dropped: the group is gone", order.group)
            return
        if order.action == "move":
            self._set_off(g, leader, [to_latlng(self.scenario, p) for p in order.route])
        elif order.action == "return":  # where it started: its own spot at the staging area, or its mission position
            self._set_off(g, leader, [(g.lat, g.lng)])
        else:  # hold: a path to where it stands stops it
            self.client.set_path(leader.id, [(leader.lat, leader.lng)])
            g.status = "holding"
        self._event("order", group=g.name, name=order.group, action=order.action, reason=order.reason)

    # ----- watch loop -----
    def _ground_leaders(self, units: dict[int, Unit]) -> list[Unit]:
        return [l for g in self.groups.values() if g.role in GROUND_ROLES if (l := self._leader(units, g.name))]

    def _in_response_zone(self, threat: Unit, units: dict[int, Unit]) -> bool:
        if super()._in_response_zone(threat, units):
            return True
        scramble = self.scenario.rules.scramble_when_contact_within_km * 1000
        if any(geo.distance_m(z.lat, z.lng, threat.lat, threat.lng) <= scramble for z in self.camp.defend):
            return True
        reach = self.camp.protect_radius_km * 1000
        return any(geo.distance_m(l.lat, l.lng, threat.lat, threat.lng) <= reach for l in self._ground_leaders(units))

    def tick(self, units: dict[int, Unit] | None = None) -> None:
        units = self.client.get_units() if units is None else units
        if self.awaiting or any(map(is_pattern, self.camp.command_groups)):
            self._adopt(units)
        self._move_groups(units)
        super().tick(units)
        enemy_ground = self.detected_enemy_ground(units)
        # Attack helicopters fight low over the ground forces: close air support goes after them too.
        helicopters = {i: u for i, u in self.detected_enemy_air(units).items() if u.category == "Helicopter"}
        self._air_response(units, "cas", {**enemy_ground, **helicopters})
        self._watch_defence(enemy_ground)
        if self.batteries:
            self._watch_missiles(units)
        self._referee(units)
        self._maybe_review(units)

    def _move_groups(self, units: dict[int, Unit]) -> None:
        now = time.time()
        for g in [g for g in self.groups.values() if g.role in GROUND_ROLES]:
            leader = self._live_leader(units, g)
            if leader is None:
                continue
            if g.status == "waiting" and g.route and now >= g.depart_at:
                self._set_off(g, leader, g.route)
                self._event("depart", group=g.name, name=g.label)
            elif g.status == "moving" and geo.distance_m(leader.lat, leader.lng, *g.route[-1]) <= ARRIVED_M:
                g.status = "arrived"
                o = self.scenario.objective
                self._event("arrived", group=g.name, name=g.label,
                            km_from_target=round(geo.distance_m(o.lat, o.lng, leader.lat, leader.lng) / 1000, 1))

    def _watch_defence(self, enemy_ground: dict[int, Unit]) -> None:
        """A detected enemy ground unit inside a defend zone calls for a review."""
        for z in self.camp.defend:
            inside = [t for t in enemy_ground.values() if geo.distance_m(z.lat, z.lng, t.lat, t.lng) <= z.radius_m]
            if inside and z.name not in self.threatened:
                self.threatened.append(z.name)
                self._event("defence_threatened", zone=z.name, enemy_units=len(inside), types=dict(Counter(t.name for t in inside)))
            elif not inside and z.name in self.threatened:
                self.threatened.remove(z.name)
                self._event("defence_clear", zone=z.name)

    # ----- missiles -----
    def _watch_missiles(self, units: dict[int, Unit]) -> None:
        """Follow each missile group's launches. Its strike is over once it has not launched for quiet_minutes."""
        m, now = self.scenario.missiles, time.time()
        for name, b in self.batteries.items():
            members = [u for u in units.values() if u.group_name == name and u.alive]
            if not members:
                if b["seen"] and b["status"] != GONE:
                    b["status"] = GONE
                    self._event("missile_battery_lost", group=name, side=f"{self.scenario.coalition} (yours)")
                elif not b["seen"]:
                    b["status"] = ABSENT if b["status"] in (READY, ABSENT) or not m.wait_for_mission_strike else WAITING
                continue
            if not b["seen"] and b["status"] == ABSENT:
                b["status"] = READY
            b["seen"] = True
            left = sum(n for u in members for n in u.ammo.values()) if any(u.ammo for u in members) else None
            before = b["missiles_left"]
            if left is not None and before is not None and left < before:
                b["launched"] += before - left
                b["last_launch"] = now
                if b["status"] == WAITING:
                    b["status"] = FIRING
                self._event("missile_launch", group=name, missiles=before - left, missiles_left=left)
            b["missiles_left"] = left
            quiet = now - b["last_launch"] >= m.quiet_minutes * 60
            if left == 0:
                b["status"] = EMPTY
            elif b["status"] == FIRING and quiet:
                b["status"] = READY
                self._event("missiles_ready", group=name, missiles_left=left, launched=b["launched"])
            elif (b["status"] == WAITING and m.release_after_minutes is not None
                  and now - self.started_at >= m.release_after_minutes * 60):
                b["status"] = READY
                self._event("missiles_ready", group=name, missiles_left=left, launched=b["launched"], released_after_min=m.release_after_minutes)

    def _range_km(self, group: str, units: dict[int, Unit]) -> float:
        types = {u.name for u in units.values() if u.group_name == group and u.alive}
        ranges = self.scenario.missiles.range_km
        return max((ranges.get(t) or self.catalog.describe(t).engagement_range_m / 1000 for t in types), default=0)

    def _strike(self, st: Strike) -> None:
        units = self.client.get_units()
        b = self.batteries.get(st.group)
        leader = self._leader(units, st.group)
        point = strike_point(self.scenario, st.target, {"detected_enemy": [
            {"id": t.id, "position": self._where(t.lat, t.lng)} for t in self.detected_enemy_ground(units).values()]})
        refuse = lambda why: self._event("strike_refused", group=st.group, target=st.target, why=why)
        if b is None or b["status"] != READY or leader is None:
            return refuse(f"the battery is {b['status'] if b else 'unknown'}")
        if point is None:
            return refuse("no such target, or the enemy unit is no longer detected")
        reach = self._range_km(st.group, units)
        km = geo.distance_m(leader.lat, leader.lng, *point) / 1000
        if reach and km > reach:
            return refuse(f"{km:.0f} km away, beyond its {reach:g} km range")
        guard = self.scenario.missiles.no_fire_near_friendly_m
        if any(u.alive and u.coalition == self.scenario.coalition and u.category in GROUND_CATEGORIES
               and geo.distance_m(u.lat, u.lng, *point) <= guard for u in units.values()):
            return refuse(f"Red ground units within {guard:g} m of the aim point")
        missiles.fire(self.client, st.group, *point, st.rounds, self.work_dir)
        b["status"], b["last_launch"] = FIRING, time.time()
        self._event("missile_strike", group=st.group, target=st.target, rounds=st.rounds, km=round(km), reason=st.reason)
        self._marker(*point, f"Red missile strike by {st.group}: {st.target}")

    def _where(self, lat: float, lng: float) -> dict:
        o = self.scenario.objective
        return {"bearing_deg": round(geo.bearing_deg(o.lat, o.lng, lat, lng)),
                "distance_km": round(geo.distance_m(o.lat, o.lng, lat, lng) / 1000, 1)}

    def _referee(self, units: dict[int, Unit]) -> None:
        """Decides the outcome from the whole picture, not just what Red detects."""
        o = self.scenario.objective
        inside = lambda u: u.alive and u.category in GROUND_CATEGORIES and geo.distance_m(o.lat, o.lng, u.lat, u.lng) <= o.radius_m
        ours = {g.name for g in self.groups.values() if g.role in GROUND_ROLES}
        red_in = [u for u in units.values() if inside(u) and u.group_name in ours]
        blue_in = [u for u in units.values() if inside(u) and u.coalition not in (self.scenario.coalition, "neutral")]
        now = time.time()
        if red_in and not blue_in:
            if self.hold_since is None:
                self.hold_since = now
                self._event("hold_started", red_units=len(red_in))
            elif not self.outcome and now - self.hold_since >= self.camp.hold_minutes * 60:
                self.outcome = "won"
                self._event("target_taken", red_units=len(red_in), held_min=round((now - self.hold_since) / 60, 1))
                self._marker(o.lat, o.lng, f"Red has taken {o.name}")
                log.info("Red has taken %s. Claude reviews stop here; the units keep fighting.", o.name)
        elif self.hold_since is not None:
            self.hold_since = None
            self._event("hold_broken", red_units=len(red_in), enemy_units=len(blue_in))

        cheapest = min((i.price for i in self.catalog.items.values() if i.category == "groundunit"), default=0)
        can_buy = self.camp.spawn_ground and self.points_left >= cheapest
        if not self.outcome and not ours and not self.awaiting and not can_buy:
            self.outcome = "lost"
            self._event("campaign_lost", points_left=self.points_left)
            log.info("The campaign is lost. Claude reviews stop here.")

    # ----- reviews -----
    def _maybe_review(self, units: dict[int, Unit]) -> None:
        if self.replanner is None:
            return
        if self._review is not None:
            if not self._review.done():
                return
            future, self._review = self._review, None
            try:
                self._apply_review(future.result())
            except Exception:
                log.exception("Review failed; the current orders stand")
        if self.outcome:
            return  # won or lost: nothing left for Claude to decide, so no more reviews to pay for
        now = time.time()
        due = now - self.last_review >= self.camp.replan_minutes * 60
        triggered = self.review_reasons and now - self.last_review >= self.camp.min_replan_gap_minutes * 60
        if not (due or triggered):
            return
        report = self.situation(units, reasons=list(dict.fromkeys(self.review_reasons)) or ["scheduled"])
        self.review_reasons.clear()
        self.recent_events = []
        self.last_review = now
        log.info("Asking Claude for a review (%s)", ", ".join(report["why_now"]))
        if not self.review_async:
            try:
                self._apply_review(self.replanner(report))
            except Exception:
                log.exception("Review failed; the current orders stand")
            return
        if self._executor is None:
            self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._review = self._executor.submit(self.replanner, report)

    def _apply_review(self, plan: CampaignPlan) -> None:
        cost = plan.cost(self.catalog)
        if self.camp.spawn_ground and cost > self.points_left:
            log.warning("Review buys %d points with %g left; ignoring its reinforcements", cost, self.points_left)
            plan.convoys, plan.artillery, plan.air = [], [], []
            cost = 0
        if not self.camp.spawn_ground and (plan.convoys or plan.artillery):
            log.warning("Review buys ground units, which this mission does not allow; ignoring them")
            plan.convoys, plan.artillery = [], []
        flown = Counter()
        for a in plan.air:
            flown[(a.airbase, a.type)] += a.count
        if self.airframes_left and any(n > self.airframes_left.get(b, {}).get(t, 0) for (b, t), n in flown.items()):
            log.warning("Review flies more airframes than the warehouses have left; ignoring its air taskings")
            plan.air = []
        self.points_left -= cost
        self.assessments = (self.assessments + [plan.summary])[-3:]
        self._event("review", assessment=plan.summary, orders=len(plan.orders), reinforcements=plan.names() + [a.type for a in plan.air],
                    cost=cost, points_left=self.points_left)
        self._carry_out(plan)

    def situation(self, units: dict[int, Unit], reasons: list[str] | None = None) -> dict:
        """What Red knows, for Claude: its own forces, and the enemy units its sensors detect."""
        o = self.scenario.objective
        where = lambda u: {"bearing_deg": round(geo.bearing_deg(o.lat, o.lng, u.lat, u.lng)),
                           "distance_km": round(geo.distance_m(o.lat, o.lng, u.lat, u.lng) / 1000, 1)}
        ground, air = [], []
        for g in self.groups.values():
            members = [u for u in units.values() if u.group_name == g.name and u.alive]
            leader = self._leader(units, g.name)
            if not leader:
                continue
            entry = {"name": g.label or g.name, "role": g.role, "units": dict(Counter(u.name for u in members)),
                     "position": where(leader), "health_pct": round(sum(u.health for u in members) / len(members))}
            if g.role in GROUND_ROLES:
                ground.append({**entry, "status": g.status})
            else:
                air.append({**entry, "target_id": g.target_id})
        leaders = {g.label: self._leader(units, g.name) for g in self.groups.values() if g.role in GROUND_ROLES}
        enemies = []
        for t in {**self.detected_enemy_air(units), **self.detected_enemy_ground(units)}.values():
            near = min(((n, geo.distance_m(l.lat, l.lng, t.lat, t.lng)) for n, l in leaders.items() if l), key=lambda x: x[1], default=None)
            enemies.append({"id": t.id, "type": t.name, "category": t.category, "group": t.group_name, "position": where(t),
                            **({"nearest_red_group": near[0], "km_from_it": round(near[1] / 1000, 1)} if near else {})})
        report = {
            "why_now": reasons or ["scheduled"],
            "elapsed_min": round((time.time() - self.started_at) / 60, 1),
            "outcome": self.outcome or "in progress",
            **({"points_left": self.points_left} if self.camp.spawn_ground else {}),
            "hold": {"running": self.hold_since is not None,
                     "held_min": round((time.time() - self.hold_since) / 60, 1) if self.hold_since else 0,
                     "needed_min": self.camp.hold_minutes},
            "red_ground_groups": ground,
            "red_aircraft": air,
            "on_alert": [{"type": a.type, "role": a.role, "airbase": a.airbase, "remaining": a.remaining}
                         for a in self.alerts if a.remaining],
            "detected_enemy": enemies,
            "events_since_last_review": self.recent_events,
            "your_previous_assessments": self.assessments,
        }
        if self.camp.defend:
            red_ground = [u for g in self.groups.values() if g.role in GROUND_ROLES
                          for u in units.values() if u.group_name == g.name and u.alive]
            count_in = lambda us, z: sum(geo.distance_m(z.lat, z.lng, u.lat, u.lng) <= z.radius_m for u in us)
            report["defend_zones"] = [{"name": z.name, "enemy_detected_inside": z.name in self.threatened,
                                       "your_ground_units_inside": count_in(red_ground, z)} for z in self.camp.defend]
        if self.scenario.warehouses:
            report["airframes_left"] = {b: {t: n for t, n in types.items() if n > 0} for b, types in self.airframes_left.items()}
        if self.spawn_failures:
            report["spawn_failures"] = [{"airbase": b, "type": t, "loadout": l} for b, t, l in sorted(self.spawn_failures)]
        if self.awaiting:
            report["groups_not_in_mission_yet"] = self.awaiting
        if self.batteries:
            report["missile_batteries"] = []
            for name, b in self.batteries.items():
                entry = {"group": name, "status": b["status"],
                         "missiles_left": b["missiles_left"] if b["missiles_left"] is not None else "unknown",
                         "launched_so_far": b["launched"]}
                leader = self._leader(units, name)
                if leader:
                    entry.update(position=where(leader), range_km=round(self._range_km(name, units)),
                                 launchers=dict(Counter(u.name for u in units.values() if u.group_name == name and u.alive)))
                report["missile_batteries"].append(entry)
        return report
