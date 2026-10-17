"""Scenario file: what to defend, with what, under which rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .mission import airframe_stock


@dataclass
class Objective:
    name: str
    lat: float
    lng: float
    radius_m: float = 3000
    threat_axis_deg: float | None = None
    airbase: str | None = None


@dataclass
class Zone:
    name: str
    lat: float
    lng: float
    radius_m: float = 3000


@dataclass
class Warehouses:
    """Red aircraft fly only from these airbases, and only as many of each type as the mission's warehouses hold."""
    airbases: dict[str, int]  # Olympus airbase name -> airport ID of its warehouse in the mission
    mission_file: str = ""  # the .miz the warehouses are read from
    ignore_types: list[str] = field(default_factory=list)  # warehouse types never flown
    spawn_as: dict[str, str] = field(default_factory=dict)  # warehouse type -> Olympus type spawned for it
    stock: dict[str, dict[str, int]] = field(default_factory=dict)  # filled at load: {airbase: {type: count}}


@dataclass
class Missiles:
    """Ballistic missile groups in the mission that Claude may fire, once the mission's own strike is over."""
    groups: list[str]  # mission editor group names
    targets: list[Zone] = field(default_factory=list)  # named aim points Claude can choose from
    intent: str = ""  # what the strikes are for, in a sentence or two, for Claude
    wait_for_mission_strike: bool = True  # the mission fires first; Claude only gets each group after it has launched
    quiet_minutes: float = 3  # no launch for this long: a group's strike is over
    release_after_minutes: float | None = None  # hand the groups to Claude anyway after this long, launched or not
    no_fire_near_friendly_m: float = 2000  # no strike on a point this close to a Red ground unit
    range_km: dict[str, float] = field(default_factory=dict)  # per launcher type, where Olympus has no range for it


@dataclass
class InventoryEntry:
    type: str
    count: int
    base: str | None = None  # aircraft only: home airbase
    roles: list[str] = field(default_factory=lambda: ["intercept", "sweep"])  # aircraft only


@dataclass
class CatalogOptions:
    classes: list[str] | None = None
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    price_overrides: dict[str, int] = field(default_factory=dict)
    class_prices: dict[str, int] = field(default_factory=dict)
    allow_mods: bool = False


@dataclass
class Rules:
    skill: str = "High"
    keep_sams_dark_until_km: float | None = 40
    max_distance_from_objective_km: float = 60
    scramble_when_contact_within_km: float = 80
    scramble_pair_size: int = 2
    fighter_spawn: str = "ramp"  # "ramp" (hot start at the airbase) or "air" (airborne above it)
    air_spawn_altitude_ft: float = 10000
    sweep_altitude_ft: float = 25000
    poll_seconds: float = 5
    # Ground war: enemy ground units count only once a Red unit detects them (EWRs do not see vehicles).
    reserve_react_within_km: float = 30  # a detected enemy ground group this close to the objective draws a reserve
    artillery_fire: bool = True  # artillery fires on detected enemy ground units within its range
    fire_mission_every_s: float = 90  # minimum time between fire missions of one battery
    no_fire_near_friendly_m: float = 500  # no fire mission on a target this close to a Red ground unit


@dataclass
class Campaign:
    """Offensive mode: Red starts at its HQ and must take the objective (the target zone) with ground forces."""
    hq: str  # Red airbase that all Red aircraft fly from
    staging_name: str = ""
    staging_lat: float | None = None  # where ground forces spawn and form up; empty: next to the HQ airbase
    staging_lng: float | None = None
    hold_minutes: float = 10  # Red takes the target by holding it this long with no Blue ground units inside
    replan_minutes: float = 10  # Claude reviews the situation this often, and after big events
    min_replan_gap_minutes: float = 2  # but never more often than this
    protect_radius_km: float = 25  # alert aircraft launch against threats this close to Red ground forces
    max_group_units: int = 12  # vehicles per convoy or battery
    spawn_ground: bool = True  # False: no ground units are bought or spawned; only command_groups fight on the ground
    command_groups: list[str] = field(default_factory=list)  # Red ground groups already in the mission that Claude commands
    defend: list[Zone] = field(default_factory=list)  # places Red must hold while it attacks


@dataclass
class Scenario:
    name: str
    objective: Objective
    coalition: str = "red"
    country: str = ""  # empty: Olympus picks a country of the coalition
    eras: list[str] | None = None
    budget_points: int | None = None
    inventory: list[InventoryEntry] = field(default_factory=list)
    catalog: CatalogOptions | None = None
    rules: Rules = field(default_factory=Rules)
    campaign: Campaign | None = None
    warehouses: Warehouses | None = None
    missiles: Missiles | None = None

    @property
    def mode(self) -> str:
        """'inventory': place exactly these units. 'catalog': buy from a menu within the budget."""
        return "inventory" if self.inventory else "catalog"


def load_scenario(path: str | Path) -> Scenario:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    obj = raw.get("objective") or {}
    loc = obj.get("location") or {}
    if "lat" not in loc or "lng" not in loc:
        raise ValueError("objective.location needs lat and lng")

    era = raw.get("era")
    eras = [era] if isinstance(era, str) else era

    inventory = [InventoryEntry(**e) for e in raw.get("inventory") or []]
    catalog_raw = raw.get("catalog")
    if not inventory and catalog_raw is None:
        catalog_raw = {}
    campaign = None
    if raw.get("campaign"):
        c = dict(raw["campaign"])
        staging = c.pop("staging", None) or {}
        defend = [Zone(name=z.get("name", "zone"), lat=float(z["lat"]), lng=float(z["lng"]), radius_m=float(z.get("radius_m", 3000)))
                  for z in c.pop("defend", None) or []]
        if not c.get("hq"):
            raise ValueError("campaign.hq must name the Red airbase the campaign is run from")
        campaign = Campaign(staging_name=staging.get("name", ""), staging_lat=staging.get("lat"), staging_lng=staging.get("lng"),
                            defend=defend, **c)
        if inventory:
            raise ValueError("A campaign scenario has no inventory: it buys its forces or commands the mission's own")
        if campaign.spawn_ground and raw.get("budget_points") is None:
            raise ValueError("A campaign that buys ground forces needs budget_points (or set campaign.spawn_ground: false)")
        if not campaign.spawn_ground and not campaign.command_groups:
            raise ValueError("campaign.spawn_ground is false, so list the mission's ground groups Red commands in campaign.command_groups")
    elif not inventory and raw.get("budget_points") is None:
        raise ValueError("A catalog scenario needs budget_points")

    warehouses = None
    if raw.get("warehouses"):
        w = dict(raw["warehouses"])
        if not w.get("airbases") or not w.get("mission_file"):
            raise ValueError("warehouses needs mission_file and airbases (airbase name: airport ID)")
        miz = Path(w["mission_file"])
        if not miz.is_absolute():
            miz = Path(path).parent / miz if (Path(path).parent / miz).exists() else miz
        if not miz.exists():
            raise ValueError(f"warehouses.mission_file not found: {miz}")
        warehouses = Warehouses(**{**w, "mission_file": str(miz), "airbases": {k: int(v) for k, v in w["airbases"].items()}})
        warehouses.stock = airframe_stock(miz, warehouses.airbases, warehouses.ignore_types, warehouses.spawn_as)

    missiles = None
    if raw.get("missiles"):
        m = dict(raw["missiles"])
        if not m.get("groups"):
            raise ValueError("missiles.groups must list the mission's missile groups")
        targets = [Zone(name=t["name"], lat=float(t["lat"]), lng=float(t["lng"]), radius_m=float(t.get("radius_m", 0)))
                   for t in m.pop("targets", None) or []]
        missiles = Missiles(targets=targets, **m)
        if not campaign:
            raise ValueError("missiles need a campaign: strikes are ordered at the campaign's reviews")

    scenario = Scenario(
        name=raw.get("scenario", Path(path).stem),
        coalition=raw.get("coalition", "red"),
        country=raw.get("country", ""),
        eras=eras,
        objective=Objective(
            name=obj.get("name", "objective"),
            lat=float(loc["lat"]),
            lng=float(loc["lng"]),
            radius_m=float(obj.get("radius_m", 3000)),
            threat_axis_deg=obj.get("threat_axis_deg"),
            airbase=obj.get("airbase"),
        ),
        budget_points=raw.get("budget_points"),
        inventory=inventory,
        catalog=CatalogOptions(**catalog_raw) if catalog_raw is not None else None,
        rules=Rules(**(raw.get("rules") or {})),
        campaign=campaign,
        warehouses=warehouses,
        missiles=missiles,
    )
    if scenario.rules.fighter_spawn not in ("ramp", "air"):
        raise ValueError("rules.fighter_spawn must be 'ramp' or 'air'")
    return scenario
