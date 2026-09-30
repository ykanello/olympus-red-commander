"""Executes a validated plan through Olympus, then runs the watch-and-scramble loop.

The loop is deterministic and makes no Claude calls:
- SAM sites kept dark (alarm state green) go active when a detected enemy aircraft comes close.
- A detected enemy aircraft inside the scramble distance gets an alert pair launched against it.
Red only acts on enemy aircraft that at least one Red unit currently detects.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import geo
from .catalog import Catalog
from .olympus import OlympusClient, Unit
from .plan import FighterTasking, Plan
from .scenario import Scenario

log = logging.getLogger(__name__)

FT = 0.3048
AIR_CATEGORIES = ("Aircraft", "Helicopter")
DARK_CLASSES = {"sam_long", "sam_medium"}  # radar SAMs that emission control applies to
UNIT_SPACING_M = 80


@dataclass
class SpawnedGroup:
    name: str
    type: str
    cls: str
    lat: float
    lng: float
    role: str = "ground"  # ground | sweep | intercept
    target_id: int | None = None
    dark: bool = False


@dataclass
class AlertSlot:
    type: str
    airbase: str
    loadout: str
    remaining: int


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

    # ---------- helpers ----------
    def _event(self, kind: str, **data) -> None:
        log.info("%s %s", kind, data)
        if self.event_log:
            with self.event_log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": time.time(), "event": kind, **data}) + "\n")

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
            dark = item.cls in DARK_CLASSES and rules.keep_sams_dark_until_km is not None
            spawned = SpawnedGroup(group_name, g.type, item.cls, g.lat, g.lng, dark=dark)
            self.groups[group_name] = spawned
            self._event("spawned", group=group_name, name=g.name, type=g.type, count=g.count, reason=g.reason)
            self._marker(g.lat, g.lng, f"{g.name}: {g.count}x {item.label}. {g.reason}")

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
    def detected_enemy_air(self, units: dict[int, Unit]) -> dict[int, Unit]:
        """Enemy aircraft that at least one live Red unit currently detects."""
        red = [u for u in units.values() if u.alive and u.coalition == self.scenario.coalition]
        seen = {c.id for u in red for c in u.contacts}
        return {
            uid: u for uid, u in units.items()
            if uid in seen and u.alive and u.coalition not in (self.scenario.coalition, "neutral") and u.category in AIR_CATEGORIES
        }

    def tick(self, units: dict[int, Unit] | None = None) -> None:
        units = self.client.get_units() if units is None else units
        rules = self.scenario.rules
        o = self.scenario.objective
        threats = self.detected_enemy_air(units)

        # 1. Emission control: wake dark SAMs when a detected aircraft closes in.
        if rules.keep_sams_dark_until_km is not None:
            for g in self.groups.values():
                if not g.dark:
                    continue
                close = [t for t in threats.values() if geo.distance_m(g.lat, g.lng, t.lat, t.lng) <= rules.keep_sams_dark_until_km * 1000]
                leader = self._leader(units, g.name)
                if close and leader:
                    self.client.set_alarm_state(leader.id, "red")
                    g.dark = False
                    self._event("sam_active", group=g.name, trigger=close[0].id)

        # 2. Interceptors: drop dead groups, retarget those whose target is gone.
        assigned: set[int] = set()
        for g in list(self.groups.values()):
            if g.role != "intercept":
                continue
            leader = self._leader(units, g.name)
            if leader is None:
                self._event("group_lost", group=g.name)
                del self.groups[g.name]
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

        # 3. Scramble against unassigned threats inside the scramble ring.
        ring_m = rules.scramble_when_contact_within_km * 1000
        for t in sorted(threats.values(), key=lambda t: geo.distance_m(o.lat, o.lng, t.lat, t.lng)):
            if t.id in assigned or geo.distance_m(o.lat, o.lng, t.lat, t.lng) > ring_m:
                continue
            slot = self._pick_alert(t)
            if slot is None:
                break
            self._scramble(slot, t)
            assigned.add(t.id)

    def _pick_alert(self, target: Unit) -> AlertSlot | None:
        ready = [s for s in self.alerts if s.remaining > 0]
        if not ready:
            return None
        return min(ready, key=lambda s: geo.distance_m(*self.red_airbases[s.airbase], target.lat, target.lng))

    def _scramble(self, slot: AlertSlot, target: Unit) -> None:
        count = min(self.scenario.rules.scramble_pair_size, slot.remaining)
        slot.remaining -= count
        self.scramble_counter += 1
        heading = geo.bearing_deg(*self.red_airbases[slot.airbase], target.lat, target.lng)
        group_name = self._spawn_fighters(f"INT{self.scramble_counter}-{slot.airbase}", slot.type, slot.loadout, slot.airbase, count, heading)
        self.groups[group_name] = SpawnedGroup(group_name, slot.type, "fighter", *self.red_airbases[slot.airbase], role="intercept", target_id=target.id)
        leader = self._wait_for_group(group_name)
        if leader:
            self.client.set_roe(leader.id, "free")
            self.client.attack_unit(leader.id, target.id)
        self._event("scramble", group=group_name, type=slot.type, count=count, airbase=slot.airbase, target=target.id,
                    left_on_alert=slot.remaining)

    def run(self, stop_after_s: float | None = None) -> None:
        start = time.monotonic()
        while stop_after_s is None or time.monotonic() - start < stop_after_s:
            try:
                self.tick()
            except Exception:
                log.exception("Watch loop tick failed; retrying next tick")
            time.sleep(self.scenario.rules.poll_seconds)
