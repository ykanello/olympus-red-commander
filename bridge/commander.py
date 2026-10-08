"""Executes a validated plan through Olympus, then runs the watch-and-scramble loop.

The loop is deterministic and makes no Claude calls:
- SAM sites kept dark (alarm state green) go active when a detected enemy aircraft comes close.
- A detected enemy aircraft inside the scramble distance gets an alert pair launched against it.
- A detected enemy ground group near the objective draws the nearest free ground reserve, which drives to it.
- Artillery fires on detected enemy ground units within its range, unless Red ground units are close to them.
Red only acts on enemy units that at least one Red unit currently detects.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from . import geo, terrain
from .catalog import ARTILLERY_CLASSES, Catalog
from .olympus import OlympusClient, Unit
from .plan import FighterTasking, Plan
from .scenario import Scenario

log = logging.getLogger(__name__)

FT = 0.3048
AIR_CATEGORIES = ("Aircraft", "Helicopter")
GROUND_CATEGORIES = ("GroundUnit",)
DARK_CLASSES = {"sam_long", "sam_medium"}  # radar SAMs that emission control applies to
POINT_DEFENCE_MAX_RANGE_M = 15_000  # SAMs reaching no further than this (Tor) stay on, to shoot down ARMs
UNIT_SPACING_M = 80
REPATH_M = 500  # a reserve gets a new path when its target has moved this far
RESERVE_LEASH = 1.5  # a reserve breaks off when its target is this many reaction radii from the objective


@dataclass
class SpawnedGroup:
    name: str
    type: str
    cls: str
    lat: float
    lng: float
    role: str = "ground"  # ground | reserve | artillery | sweep | intercept
    target_id: int | None = None
    dark: bool = False
    target_group: str | None = None  # reserve: the enemy group it is sent against
    target_lat: float | None = None  # reserve: where its current path leads
    target_lng: float | None = None
    last_fire: float = 0.0  # artillery: wall-clock time of its last fire mission
    label: str = ""  # the name Claude gave the group in its plan
    route: list = field(default_factory=list)  # campaign: [lat, lng] points it drives along
    depart_at: float = 0.0  # campaign: wall-clock time it sets off
    status: str = ""  # campaign: waiting | moving | arrived


@dataclass
class AlertSlot:
    type: str
    airbase: str
    loadout: str
    remaining: int
    role: str = "intercept"  # intercept (against aircraft) or cas (against ground units)


class MissionRestarted(Exception):
    """Olympus came back with a new session: the mission was restarted and our units are gone."""


@dataclass
class Commander:
    client: OlympusClient
    scenario: Scenario
    catalog: Catalog
    red_airbases: dict[str, tuple[float, float]]
    run_id: str = field(default_factory=lambda: time.strftime("%H%M%S"))
    event_log: Path | None = None
    groups: dict[str, SpawnedGroup] = field(default_factory=dict)
    alerts: list[AlertSlot] = field(default_factory=list)
    scramble_counter: int = 0
    marker_counter: int = 9000
    session_hash: str | None = None
    state_path: Path | None = None  # saved after every change so a restarted bridge can take over its units
    work_dir: Path = Path("logs")  # where generated Lua files go (must be on the DCS machine)
    check_terrain: bool = True

    # ---------- helpers ----------
    def _event(self, kind: str, **data) -> None:
        log.info("%s %s", kind, data)
        if self.event_log:
            with self.event_log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": time.time(), "event": kind, **data}) + "\n")
        self.save_state()

    # ---------- state, so a restarted bridge resumes instead of spawning twice ----------
    def state(self) -> dict:
        return {
            "session_hash": self.session_hash, "run_id": self.run_id, "scramble_counter": self.scramble_counter,
            "marker_counter": self.marker_counter,
            "groups": [dataclasses.asdict(g) for g in self.groups.values()],
            "alerts": [dataclasses.asdict(a) for a in self.alerts],
        }

    def save_state(self) -> None:
        if not self.state_path:
            return
        state = self.state()
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        tmp.replace(self.state_path)

    def restore(self, state: dict) -> None:
        self.run_id = state["run_id"]
        self.scramble_counter = state["scramble_counter"]
        self.marker_counter = state["marker_counter"]
        self.groups = {g["name"]: SpawnedGroup(**g) for g in state["groups"]}
        self.alerts = [AlertSlot(**a) for a in state["alerts"]]

    # ---------- terrain ----------
    def _footprint_m(self, g) -> float:
        if self.catalog[g.type].is_template:
            return 150.0
        return UNIT_SPACING_M + 20.0 if g.count > 1 else 30.0

    def _fix_sites(self, plan: Plan) -> None:
        """Move each ground site to dry, flat, open ground near where Claude put it."""
        requests_ = [terrain.SiteRequest(i, g.lat, g.lng, self._footprint_m(g)) for i, g in enumerate(plan.ground_groups)]
        samples = terrain.probe(self.client, requests_, self.work_dir)
        if samples is None:
            return
        for i, g in enumerate(plan.ground_groups):
            if i not in samples:
                continue
            best = terrain.choose(samples[i])
            moved_m = geo.distance_m(g.lat, g.lng, best.lat, best.lng)
            if not best.good:
                log.warning("No clear ground found near %s; using the least bad spot (water points %d, slope %.0f%%, obstacles %d).",
                            g.name, best.wet_points, best.slope * 100, best.obstacles)
            if moved_m > 1:
                self._event("site_moved", name=g.name, moved_m=round(moved_m), good=best.good,
                            planned=samples[i][0].__dict__, chosen=best.__dict__)
                g.lat, g.lng = best.lat, best.lng

    def _group_name(self, name: str) -> str:
        return f"RED-{self.run_id}-{name}"

    def _leader(self, units: dict[int, Unit], group_name: str) -> Unit | None:
        members = [u for u in units.values() if u.group_name == group_name and u.alive]
        if not members:
            return None
        leaders = [u for u in members if u.is_leader]
        return (leaders or members)[0]

    def _wait_for_group(self, group_name: str, timeout: float = 20.0) -> Unit | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            leader = self._leader(self.client.get_units(), group_name)
            if leader:
                return leader
            time.sleep(1.0)
        log.warning("Group %s did not appear in /units within %.0fs", group_name, timeout)
        return None

    def _spawn_and_find(self, requested_name: str, lat: float, lng: float, send, radius_m: float = 5000.0,
                        timeout: float = 20.0) -> str:
        """Send a spawn and return the DCS group name it produced.

        Newer Olympus versions honour groupName; older ones (e.g. v2.0.x builds) ignore it and name the group
        "Olympus-<n>". So the new group is found by name, or else as the new units of our coalition near the spawn point.
        """
        # spawn_points=0 at the call sites: the bridge enforces its own budget, so Olympus's spawn restriction is not charged.
        before = set(self.client.get_units())
        self.client.wait_for(send(), want_result=False)
        deadline = time.monotonic() + timeout
        while True:
            units = self.client.get_units()
            if self._leader(units, requested_name):
                return requested_name
            new = [u for uid, u in units.items() if uid not in before and u.alive and u.group_name
                   and u.coalition == self.scenario.coalition and geo.distance_m(lat, lng, u.lat, u.lng) <= radius_m]
            if new:
                return min(new, key=lambda u: geo.distance_m(lat, lng, u.lat, u.lng)).group_name
            if time.monotonic() >= deadline:
                log.warning("Spawned %s but could not find its units in Olympus within %.0fs", requested_name, timeout)
                return requested_name
            time.sleep(1.0)

    def _marker(self, lat: float, lng: float, text: str) -> None:
        self.marker_counter += 1
        try:
            self.client.create_marker(self.marker_counter, lat, lng, text[:200])
        except Exception as exc:  # markers are cosmetic
            log.debug("Marker failed: %s", exc)

    def _spawn_fighters(self, name: str, tasking_type: str, loadout: str, airbase: str, count: int, heading_deg: float = 0.0) -> str:
        rules = self.scenario.rules
        base_lat, base_lng = self.red_airbases[airbase]
        air = rules.fighter_spawn == "air"
        alt_m = rules.air_spawn_altitude_ft * FT if air else 0
        units = []
        for i in range(count):
            lat, lng = (geo.project(base_lat, base_lng, heading_deg + 90, i * 200) if air else (base_lat, base_lng))
            units.append({
                "unitType": tasking_type, "location": {"lat": lat, "lng": lng, "alt": alt_m},
                "altitude": alt_m, "loadout": loadout, "liveryID": "", "skill": rules.skill,
                "heading": math.radians(heading_deg),
            })
        requested = self._group_name(name)
        return self._spawn_and_find(requested, units[0]["location"]["lat"], units[0]["location"]["lng"], lambda: self.client.spawn_aircraft(
            requested, units, airbase="" if air else airbase, coalition=self.scenario.coalition,
            country=self.scenario.country, spawn_points=0))

    # ---------- plan execution ----------
    def execute(self, plan: Plan) -> None:
        rules = self.scenario.rules
        self._event("plan", summary=plan.summary, cost=plan.cost(self.catalog))
        o = self.scenario.objective
        self._marker(o.lat, o.lng, f"Red objective: {o.name}. {plan.summary}")
        if self.check_terrain and plan.ground_groups:
            self._fix_sites(plan)

        for g in plan.ground_groups:
            item = self.catalog[g.type]
            units = []
            for i in range(g.count):
                if g.count == 1:
                    lat, lng = g.lat, g.lng
                else:
                    lat, lng = geo.project(g.lat, g.lng, i * 360 / g.count, UNIT_SPACING_M)
                units.append({"unitType": g.type, "location": {"lat": lat, "lng": lng}, "heading": math.radians(g.heading_deg),
                              "liveryID": "", "skill": rules.skill})
            requested = self._group_name(g.name)
            group_name = self._spawn_and_find(requested, g.lat, g.lng, lambda: self.client.spawn_ground(
                requested, units, coalition=self.scenario.coalition, country=self.scenario.country,
                spawn_points=0))
            dark = (item.cls in DARK_CLASSES and item.engagement_range_m > POINT_DEFENCE_MAX_RANGE_M
                    and rules.keep_sams_dark_until_km is not None)
            role = "reserve" if g.role == "reserve" else "artillery" if item.cls in ARTILLERY_CLASSES else "ground"
            spawned = SpawnedGroup(group_name, g.type, item.cls, g.lat, g.lng, role=role, dark=dark)
            self.groups[group_name] = spawned
            self._event("spawned", group=group_name, name=g.name, type=g.type, count=g.count, role=role, reason=g.reason)
            tag = f" ({role})" if role != "ground" else ""
            self._marker(g.lat, g.lng, f"{g.name}{tag}: {g.count}x {item.label}. {g.reason}")

        # Group orders need the units to exist in Olympus first.
        units = self.client.get_units()
        for spawned in self.groups.values():
            leader = self._leader(units, spawned.name) or self._wait_for_group(spawned.name)
            if not leader:
                continue
            self.client.set_roe(leader.id, "free")
            self.client.set_alarm_state(leader.id, "green" if spawned.dark else "red")

        for f in plan.fighters:
            if f.role == "intercept":
                self.alerts.append(AlertSlot(f.type, f.airbase, f.loadout, f.count))
                self._event("alert", type=f.type, airbase=f.airbase, count=f.count, reason=f.reason)
            else:
                self._launch_sweep(f)

    def _launch_sweep(self, f: FighterTasking) -> None:
        route = f.route_points(self.scenario)
        heading = geo.bearing_deg(*self.red_airbases[f.airbase], *route[0])
        group_name = self._spawn_fighters(f"SWEEP-{f.airbase}-{len(self.groups)}", f.type, f.loadout, f.airbase, f.count, heading)
        self.groups[group_name] = SpawnedGroup(group_name, f.type, "fighter", *self.red_airbases[f.airbase], role="sweep")
        leader = self._wait_for_group(group_name)
        if leader:
            self.client.set_roe(leader.id, "free")
            self.client.set_altitude(leader.id, self.scenario.rules.sweep_altitude_ft * FT)
            self.client.set_path(leader.id, route)
        self._event("sweep", group=group_name, type=f.type, count=f.count, route=route, reason=f.reason)

    # ---------- watch loop ----------
    def _detected_enemy(self, units: dict[int, Unit], categories: tuple[str, ...]) -> dict[int, Unit]:
        red = [u for u in units.values() if u.alive and u.coalition == self.scenario.coalition]
        seen = {c.id for u in red for c in u.contacts}
        return {
            uid: u for uid, u in units.items()
            if uid in seen and u.alive and u.coalition not in (self.scenario.coalition, "neutral") and u.category in categories
        }

    def detected_enemy_air(self, units: dict[int, Unit]) -> dict[int, Unit]:
        """Enemy aircraft that at least one live Red unit currently detects."""
        return self._detected_enemy(units, AIR_CATEGORIES)

    def detected_enemy_ground(self, units: dict[int, Unit]) -> dict[int, Unit]:
        """Enemy ground units that at least one live Red unit currently detects."""
        return self._detected_enemy(units, GROUND_CATEGORIES)

    def _live_leader(self, units: dict[int, Unit], g: SpawnedGroup) -> Unit | None:
        """The group's leader, or None after dropping a group that has been wiped out."""
        leader = self._leader(units, g.name)
        if leader is None:
            self._event("group_lost", group=g.name)
            del self.groups[g.name]
        return leader

    def tick(self, units: dict[int, Unit] | None = None) -> None:
        units = self.client.get_units() if units is None else units
        air = self.detected_enemy_air(units)
        ground = self.detected_enemy_ground(units)
        self._emission_control(units, air)
        self._air_response(units, "intercept", air)
        self._direct_reserves(units, ground)
        if self.scenario.rules.artillery_fire:
            self._fire_missions(units, ground)

    def _emission_control(self, units: dict[int, Unit], threats: dict[int, Unit]) -> None:
        """Wake dark SAMs when a detected aircraft closes in."""
        rules = self.scenario.rules
        if rules.keep_sams_dark_until_km is None:
            return
        for g in self.groups.values():
            if not g.dark:
                continue
            close = [t for t in threats.values() if geo.distance_m(g.lat, g.lng, t.lat, t.lng) <= rules.keep_sams_dark_until_km * 1000]
            leader = self._leader(units, g.name)
            if close and leader:
                self.client.set_alarm_state(leader.id, "red")
                g.dark = False
                self._event("sam_active", group=g.name, trigger=close[0].id)

    def _in_response_zone(self, threat: Unit, units: dict[int, Unit]) -> bool:
        """Whether a detected threat is close enough to launch alert aircraft against it."""
        o = self.scenario.objective
        return geo.distance_m(o.lat, o.lng, threat.lat, threat.lng) <= self.scenario.rules.scramble_when_contact_within_km * 1000

    def _air_response(self, units: dict[int, Unit], role: str, threats: dict[int, Unit]) -> None:
        """Alert aircraft of one role: drop dead groups, retarget those whose target is gone, scramble against the rest."""
        assigned: set[int] = set()
        for g in [g for g in self.groups.values() if g.role == role]:
            leader = self._live_leader(units, g)
            if leader is None:
                continue
            if g.target_id in threats:
                assigned.add(g.target_id)
                continue
            free = [t for t in threats.values() if t.id not in assigned]
            if free:
                target = min(free, key=lambda t: geo.distance_m(leader.lat, leader.lng, t.lat, t.lng))
                self.client.attack_unit(leader.id, target.id)
                g.target_id = target.id
                assigned.add(target.id)
                self._event("retask", group=g.name, target=target.id)

        o = self.scenario.objective
        for t in sorted(threats.values(), key=lambda t: geo.distance_m(o.lat, o.lng, t.lat, t.lng)):
            if t.id in assigned or not self._in_response_zone(t, units):
                continue
            slot = self._pick_alert(t, role)
            if slot is None:
                break
            self._scramble(slot, t)
            assigned.add(t.id)

    def _drive(self, leader: Unit, g: SpawnedGroup, target: Unit) -> None:
        self.client.set_path(leader.id, [(target.lat, target.lng)])
        g.target_id, g.target_lat, g.target_lng = target.id, target.lat, target.lng

    def _direct_reserves(self, units: dict[int, Unit], threats: dict[int, Unit]) -> None:
        o = self.scenario.objective
        ring_m = self.scenario.rules.reserve_react_within_km * 1000
        from_objective = lambda u: geo.distance_m(o.lat, o.lng, u.lat, u.lng)
        enemy_groups: dict[str, list[Unit]] = {}
        for t in threats.values():
            enemy_groups.setdefault(t.group_name or str(t.id), []).append(t)

        engaged: set[str] = set()
        free: list[tuple[SpawnedGroup, Unit]] = []
        for g in [g for g in self.groups.values() if g.role == "reserve"]:
            leader = self._live_leader(units, g)
            if leader is None:
                continue
            if g.target_group is not None:
                target = threats.get(g.target_id) or min(
                    enemy_groups.get(g.target_group, []), key=lambda t: geo.distance_m(leader.lat, leader.lng, t.lat, t.lng), default=None)
                if target is not None and from_objective(target) <= ring_m * RESERVE_LEASH:
                    engaged.add(g.target_group)
                    if g.target_lat is None or geo.distance_m(g.target_lat, g.target_lng, target.lat, target.lng) > REPATH_M:
                        self._drive(leader, g, target)
                    continue
                # Contact destroyed, lost or drawn too far away: back to the reserve position.
                self.client.set_path(leader.id, [(g.lat, g.lng)])
                self._event("reserve_return", group=g.name, target_group=g.target_group)
                g.target_id = g.target_group = g.target_lat = g.target_lng = None
            free.append((g, leader))

        for key, members in sorted(enemy_groups.items(), key=lambda kv: min(map(from_objective, kv[1]))):
            if not free:
                break
            nearest = min(members, key=from_objective)
            if key in engaged or from_objective(nearest) > ring_m:
                continue
            g, leader = min(free, key=lambda gl: geo.distance_m(gl[1].lat, gl[1].lng, nearest.lat, nearest.lng))
            free.remove((g, leader))
            self._drive(leader, g, nearest)
            g.target_group = key
            self._event("reserve_sent", group=g.name, target=nearest.id, target_group=key,
                        target_km_from_objective=round(from_objective(nearest) / 1000, 1))

    def _fire_missions(self, units: dict[int, Unit], threats: dict[int, Unit]) -> None:
        rules = self.scenario.rules
        o = self.scenario.objective
        now = time.time()
        red_ground = [u for u in units.values() if u.alive and u.coalition == self.scenario.coalition and u.category in GROUND_CATEGORIES]
        safe = [t for t in threats.values()
                if not any(geo.distance_m(r.lat, r.lng, t.lat, t.lng) <= rules.no_fire_near_friendly_m for r in red_ground)]
        fired_at: set[int] = set()
        for g in [g for g in self.groups.values() if g.role == "artillery"]:
            leader = self._live_leader(units, g)
            if leader is None or g.status == "moving" or now - g.last_fire < rules.fire_mission_every_s:
                continue
            reach_m = self.catalog[g.type].engagement_range_m if g.type in self.catalog else 0
            in_range = [t for t in safe if geo.distance_m(leader.lat, leader.lng, t.lat, t.lng) <= reach_m]
            if not in_range:
                continue
            # Spread batteries over different targets, closest to the objective first.
            target = min(in_range, key=lambda t: (t.id in fired_at, geo.distance_m(o.lat, o.lng, t.lat, t.lng)))
            self.client.fire_at_area(leader.id, target.lat, target.lng)
            g.last_fire, g.target_id = now, target.id
            fired_at.add(target.id)
            self._event("fire_mission", group=g.name, target=target.id, target_type=target.name,
                        range_km=round(geo.distance_m(leader.lat, leader.lng, target.lat, target.lng) / 1000, 1))

    def _pick_alert(self, target: Unit, role: str = "intercept") -> AlertSlot | None:
        ready = [s for s in self.alerts if s.remaining > 0 and s.role == role]
        if not ready:
            return None
        return min(ready, key=lambda s: geo.distance_m(*self.red_airbases[s.airbase], target.lat, target.lng))

    def _scramble(self, slot: AlertSlot, target: Unit) -> None:
        count = min(self.scenario.rules.scramble_pair_size, slot.remaining)
        slot.remaining -= count
        self.scramble_counter += 1
        heading = geo.bearing_deg(*self.red_airbases[slot.airbase], target.lat, target.lng)
        prefix = "CAS" if slot.role == "cas" else "INT"
        group_name = self._spawn_fighters(f"{prefix}{self.scramble_counter}-{slot.airbase}", slot.type, slot.loadout, slot.airbase, count, heading)
        cls = self.catalog[slot.type].cls if slot.type in self.catalog else "fighter"
        self.groups[group_name] = SpawnedGroup(group_name, slot.type, cls, *self.red_airbases[slot.airbase], role=slot.role, target_id=target.id)
        leader = self._wait_for_group(group_name)
        if leader:
            self.client.set_roe(leader.id, "free")
            self.client.attack_unit(leader.id, target.id)
        self._event("scramble", group=group_name, role=slot.role, type=slot.type, count=count, airbase=slot.airbase, target=target.id,
                    left_on_alert=slot.remaining)

    def check_session(self) -> None:
        current = self.client.session_hash()
        if self.session_hash and current and current != self.session_hash:
            raise MissionRestarted(current)

    def run(self, stop_after_s: float | None = None, session_check_every: int = 6) -> None:
        """Watch loop. Survives Olympus dropping out; raises MissionRestarted when the mission was restarted."""
        start = time.monotonic()
        ticks, offline_since = 0, None
        while stop_after_s is None or time.monotonic() - start < stop_after_s:
            try:
                if offline_since is not None or ticks % session_check_every == 0:
                    self.check_session()
                if offline_since is not None:
                    log.info("Olympus is back after %.0fs, same mission; carrying on.", time.monotonic() - offline_since)
                    offline_since = None
                self.tick()
            except MissionRestarted:
                raise
            except requests.RequestException as exc:
                if offline_since is None:
                    offline_since = time.monotonic()
                    log.warning("Lost contact with Olympus (%s). Waiting for it to come back...", type(exc).__name__)
            except Exception:
                log.exception("Watch loop tick failed; retrying next tick")
            ticks += 1
            time.sleep(self.scenario.rules.poll_seconds)
