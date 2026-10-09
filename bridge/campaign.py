"""Offensive campaign: Red buys a combined-arms force, drives it from a staging area to the target and takes it.

Claude plans the opening, then reviews the Red-only picture every few minutes and after big events and gives
new orders or buys reinforcements. Between reviews the deterministic loop fights, as in the defence mode:
alert fighters against detected aircraft, alert ground-attack aircraft against detected ground units, artillery
fire missions. A referee, which unlike Red sees everything, decides when the target is taken.
"""

from __future__ import annotations

import concurrent.futures
import logging
import math
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from . import geo
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
REVIEW_EVENTS = {"group_lost", "arrived", "hold_started", "hold_broken", "target_taken", "campaign_lost"}
AIR_ROLES = ("sweep", "intercept", "cas")

CAMPAIGN_PROMPT = """You are the Red commander running an offensive in a DCS World mission. You plan and give orders; the DCS AI fights.

Goal: take the target zone. Red takes it when Red ground units hold the zone for hold_minutes with no enemy ground units inside. Aircraft cannot take it; only ground forces can.

How your plan is used:
- Every position is a bearing and distance from the target centre. The brief gives the staging area and the HQ airbase the same way, and that is your map.
- Convoys: each is one DCS group of up to max_group_units vehicles, types may be mixed. A convoy spawns at the staging area, sets off depart_min minutes after the start and drives its route, keeping to roads where it can. Where the route ends is where it stops. A convoy with an empty route waits at the staging area for your orders: a reserve.
- Give convoys their own short-range air defence (classes sam_short and aaa) so they are not defenceless from the air. Fixed SAM batteries cannot move and are not offered.
- Artillery batteries drive to their firing position, then fire on detected enemy ground units within range, never on a target close to Red ground units. Place them so their range reaches where your convoys will fight.
- All aircraft fly from the HQ airbase. sweep: launches at the start and flies its route weapons free. intercept: waits on alert and is scrambled in pairs against detected enemy aircraft near Red ground forces or the target. cas: waits on alert and is scrambled in pairs against detected enemy ground units near Red ground forces or the target. Loadouts only from the lists on the card: air_to_air_loadouts for sweep and intercept, ground_attack_loadouts for cas.
- Red only knows what its own units detect. Early-warning radars see aircraft, not vehicles; ground units see a few km around them. Do not assume enemy positions you have not been told.
- Points you do not spend stay available. At later reviews you can buy reinforcements, which spawn at the staging area or the HQ.

Events in the situation report are about your own Red groups unless they say otherwise (group_lost means one of your groups was destroyed). Enemy units appear only under detected_enemy.

Reviews: every replan_minutes, and after big events (a group lost, a group arriving, the hold starting or breaking), you get a situation report and return the same structure. summary is your assessment in two or three sentences. orders change what existing ground groups do: move (with a new route), hold (stop where it is) or return (drive back to the staging area). convoys, artillery and air are reinforcements bought from points_left; leave them empty when none are needed. Return empty lists when nothing should change.

Choose types for the job and the budget, and say why in each reason. Keep every reason to one line."""


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
class CampaignPlan:
    summary: str
    convoys: list[Convoy] = field(default_factory=list)
    artillery: list[Battery] = field(default_factory=list)
    air: list[FighterTasking] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict, hq: str) -> "CampaignPlan":
        return cls(
            summary=clean_text(data.get("summary", "")),
            convoys=[Convoy(**{**cleaned(c), "elements": [Element(**e) for e in c.get("elements", [])]}) for c in data.get("convoys", [])],
            artillery=[Battery(**cleaned(b)) for b in data.get("artillery", [])],
            air=[FighterTasking(**{"sweep_route": [], **{k: v for k, v in cleaned(a).items() if k != "airbase"}, "airbase": hq})
                 for a in data.get("air", [])],
            orders=[Order(**{"route": [], "reason": "", **cleaned(o)}) for o in data.get("orders", [])],
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


def campaign_schema(catalog: Catalog, review: bool = False) -> dict:
    """Structured-output schema for the opening plan, or for a review.

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
            "loadout": {"type": "string"},
            "sweep_route": {"type": "array", "items": point, "description": "Waypoints for a sweep; empty otherwise"},
            "reason": {"type": "string"},
        },
        "required": ["type", "count", "role", "loadout", "sweep_route", "reason"],
        "additionalProperties": False,
    }
    properties = {
        "summary": {"type": "string", "description": "Two or three sentences: the concept, or at a review your assessment"},
        "convoys": {"type": "array", "items": convoy},
        "artillery": {"type": "array", "items": battery},
        "air": {"type": "array", "items": air},
    }
    if review:
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
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def validate_campaign(plan: CampaignPlan, scenario: Scenario, catalog: Catalog, points: float,
                      existing: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Returns (errors, warnings). existing: labels of groups already in the field (reviews only)."""
    errors: list[str] = []
    warnings: list[str] = []
    camp = scenario.campaign
    existing = existing or []

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

    for o in plan.orders:
        if o.group not in existing:
            errors.append(f"Order for '{o.group}': no such group in the field")
        if o.action not in ("move", "hold", "return"):
            errors.append(f"Order for '{o.group}': action must be move, hold or return")
        if o.action == "move":
            if not o.route:
                errors.append(f"Order for '{o.group}': move needs a route")
            check_points(f"Order for '{o.group}'", o.route)

    cost = plan.cost(catalog)
    if cost > points:
        errors.append(f"This costs {cost} points, only {points:g} are available")

    if not existing:
        r_km = scenario.objective.radius_m / 1000
        if not any(c.route and c.route[-1]["distance_km"] <= r_km for c in plan.convoys):
            warnings.append("No convoy's route ends inside the target zone")
    return errors, warnings


# ---------- Claude ----------

class CampaignPlanner(Planner):
    def brief(self, scenario: Scenario, catalog: Catalog, red_airbases: dict, staging: tuple[float, float]) -> dict:
        o, camp = scenario.objective, scenario.campaign
        where = lambda lat, lng: {"bearing_deg": round(geo.bearing_deg(o.lat, o.lng, lat, lng)),
                                  "distance_km": round(geo.distance_m(o.lat, o.lng, lat, lng) / 1000, 1)}
        return {
            "target": {"name": o.name, "radius_m": o.radius_m, "threat_axis_deg": o.threat_axis_deg,
                       "note": "All positions are bearing/distance from this target's centre."},
            "hq_airbase": {"name": camp.hq, **where(*red_airbases[camp.hq])},
            "staging_area": {"name": camp.staging_name or camp.hq, **where(*staging)},
            "other_red_airbases": [{"name": n, **where(*ll)} for n, ll in sorted(red_airbases.items()) if n != camp.hq],
            "rules": {
                "hold_minutes": camp.hold_minutes, "replan_minutes": camp.replan_minutes,
                "max_group_units": camp.max_group_units, "protect_radius_km": camp.protect_radius_km,
                "artillery_fire": scenario.rules.artillery_fire, "no_fire_near_friendly_m": scenario.rules.no_fire_near_friendly_m,
                "scramble_pair_size": scenario.rules.scramble_pair_size, "sweep_altitude_ft": scenario.rules.sweep_altitude_ft,
            },
            "budget_points": scenario.budget_points,
            "menu": [c for c in catalog.cards() if catalog[c["type"]].category == "aircraft" or c["type"] in convoy_types(catalog)
                     or catalog[c["type"]].cls in ARTILLERY_CLASSES],
        }

    def plan_campaign(self, scenario: Scenario, catalog: Catalog, red_airbases: dict,
                      staging: tuple[float, float]) -> tuple[CampaignPlan, list[str]]:
        brief = self.brief(scenario, catalog, red_airbases, staging)
        extra = {"instruction": "Buy your opening force from the menu within budget_points (price per vehicle or airframe) and plan the opening of the offensive."}

        def check(data):
            plan = CampaignPlan.from_json(data, scenario.campaign.hq)
            return (plan, *validate_campaign(plan, scenario, catalog, scenario.budget_points))
        return self.solve(brief, campaign_schema(catalog), check, CAMPAIGN_PROMPT, extra=extra, what="opening plan")

    def review(self, scenario: Scenario, catalog: Catalog, red_airbases: dict, staging: tuple[float, float],
               report: dict) -> CampaignPlan:
        brief = self.brief(scenario, catalog, red_airbases, staging)
        extra = {"situation": report,
                 "instruction": "Review the situation and give your orders. Reinforcements must fit within situation.points_left."}
        labels = [g["name"] for g in report["red_ground_groups"]]

        def check(data):
            plan = CampaignPlan.from_json(data, scenario.campaign.hq)
            return (plan, *validate_campaign(plan, scenario, catalog, report["points_left"], labels))
        plan, _ = self.solve(brief, campaign_schema(catalog, review=True), check, CAMPAIGN_PROMPT, extra=extra, what="review")
        return plan


# ---------- execution ----------

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
            "spawn_slot": self.spawn_slot}}

    def restore(self, state: dict) -> None:
        super().restore(state)
        for k, v in state.get("campaign", {}).items():
            setattr(self, k, v)

    def _labels(self) -> dict[str, SpawnedGroup]:
        return {g.label: g for g in self.groups.values() if g.role in GROUND_ROLES}

    # ----- spawning -----
    def execute(self, plan: CampaignPlan) -> None:
        self.started_at = self.last_review = time.time()
        self.points_left = self.scenario.budget_points - plan.cost(self.catalog)
        o = self.scenario.objective
        self._event("plan", summary=plan.summary, cost=plan.cost(self.catalog), points_left=self.points_left)
        self._marker(o.lat, o.lng, f"Red target: {o.name}. {plan.summary}")
        self._marker(*self.staging, f"Red staging area: {self.camp.staging_name or self.camp.hq}")
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
            if a.role == "sweep":
                self._launch_sweep(a)
            else:
                self.alerts.append(AlertSlot(a.type, a.airbase, a.loadout, a.count, role=a.role))
                self._event("alert", type=a.type, airbase=a.airbase, count=a.count, role=a.role, reason=a.reason)
        for order in plan.orders:
            self._order(order)

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
        elif order.action == "return":
            self._set_off(g, leader, [self.staging])
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
        reach = self.camp.protect_radius_km * 1000
        return any(geo.distance_m(l.lat, l.lng, threat.lat, threat.lng) <= reach for l in self._ground_leaders(units))

    def tick(self, units: dict[int, Unit] | None = None) -> None:
        units = self.client.get_units() if units is None else units
        self._move_groups(units)
        super().tick(units)
        self._air_response(units, "cas", self.detected_enemy_ground(units))
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
        if not self.outcome and not ours and self.points_left < cheapest:
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
        if cost > self.points_left:
            log.warning("Review buys %d points with %g left; ignoring its reinforcements", cost, self.points_left)
            plan.convoys, plan.artillery, plan.air = [], [], []
            cost = 0
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
        return {
            "why_now": reasons or ["scheduled"],
            "elapsed_min": round((time.time() - self.started_at) / 60, 1),
            "outcome": self.outcome or "in progress",
            "points_left": self.points_left,
            "hold": {"running": self.hold_since is not None,
                     "held_min": round((time.time() - self.hold_since) / 60, 1) if self.hold_since else 0,
                     "needed_min": self.camp.hold_minutes},
            "red_ground_groups": ground,
            "red_aircraft": air,
            "on_alert_at_hq": [{"type": a.type, "role": a.role, "remaining": a.remaining} for a in self.alerts if a.remaining],
            "detected_enemy": enemies,
            "events_since_last_review": self.recent_events,
            "your_previous_assessments": self.assessments,
        }
