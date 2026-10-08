import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bridge import geo
from bridge.catalog import Catalog
from bridge.commander import Commander, MissionRestarted
from bridge.olympus import OlympusClient, decode_units
from bridge.plan import Plan, validate
from bridge.planner import Planner, PlannerConfig, StaticPlanner, build_brief
from bridge.scenario import load_scenario

from .fake_olympus import PASSWORD, FakeOlympus, FakeUnit, encode_unit

ROOT = Path(__file__).resolve().parents[1]
OLYMPUS_DIR = ROOT / "tests/fixtures/olympus"
KUTAISI = (42.176, 42.482)
RED_BASES = {"Kutaisi": KUTAISI, "Senaki-Kolkhi": (42.24, 42.06)}


@pytest.fixture
def scenario():
    return load_scenario(ROOT / "scenarios/defend-kutaisi.yaml")


@pytest.fixture
def budget_scenario():
    return load_scenario(ROOT / "scenarios/defend-kutaisi-budget.yaml")


@pytest.fixture
def inventory_catalog(scenario):
    wanted = [e.type for e in scenario.inventory]
    c = Catalog.load(OLYMPUS_DIR, include=wanted)
    c.items = {n: i for n, i in c.items.items() if n in wanted}
    return c


def good_plan():
    return {
        "summary": "SA-11 west of the field toward the threat, Tors and Shilkas close in, EWR behind, MiGs split sweep and alert.",
        "ground_groups": [
            {"name": "SA11-West", "type": "SA-11 SAM Battery", "count": 1, "bearing_deg": 240, "distance_km": 8, "heading_deg": 240, "reason": "Covers the threat axis"},
            {"name": "Tor-1", "type": "Tor 9A331", "count": 1, "bearing_deg": 250, "distance_km": 7, "heading_deg": 240, "reason": "Protects the SA-11"},
            {"name": "Tor-2", "type": "Tor 9A331", "count": 1, "bearing_deg": 0, "distance_km": 1, "heading_deg": 240, "reason": "Point defence"},
            {"name": "AAA", "type": "ZSU-23-4 Shilka", "count": 4, "bearing_deg": 0, "distance_km": 0.5, "heading_deg": 240, "reason": "Last ditch"},
            {"name": "EWR", "type": "1L13 EWR", "count": 1, "bearing_deg": 60, "distance_km": 10, "heading_deg": 240, "reason": "Early warning"},
        ],
        "fighters": [
            {"type": "MiG-29S", "count": 2, "airbase": "Kutaisi", "role": "sweep", "loadout": "R-73*2,R-60M*2,R-27R*2",
             "sweep_route": [{"bearing_deg": 240, "distance_km": 60}, {"bearing_deg": 220, "distance_km": 80}], "reason": "Push the threat axis"},
            {"type": "MiG-29S", "count": 2, "airbase": "Kutaisi", "role": "intercept", "loadout": "R-73*2,R-60M*2,R-27R*2",
             "sweep_route": [], "reason": "Alert pair"},
        ],
    }


# ---------- Olympus data format ----------

def test_decode_units_reads_fields_and_skips_the_rest():
    red = FakeUnit(1, "GroundUnit", 1, "1L13 EWR", "RED-EWR", 42.1, 42.4, contacts=[7, 8])
    blue = FakeUnit(7, "Aircraft", 2, "F-15C", "Blue-1", 41.9, 41.9, alt=8000)
    payload = b"\x01" + b"\0" * 7 + encode_unit(red) + encode_unit(blue)
    units = decode_units(payload)
    assert units[1].name == "1L13 EWR" and units[1].group_name == "RED-EWR" and units[1].coalition == "red"
    assert [c.id for c in units[1].contacts] == [7, 8]
    assert units[7].coalition == "blue" and units[7].airborne and units[7].lat == pytest.approx(41.9)


def test_geo_round_trip():
    lat, lng = geo.project(*KUTAISI, 240, 50_000)
    assert geo.distance_m(*KUTAISI, lat, lng) == pytest.approx(50_000, rel=1e-6)
    assert geo.bearing_deg(*KUTAISI, lat, lng) == pytest.approx(240, abs=0.01)


# ---------- catalog and pricing ----------

def test_catalog_filters_to_red_era_and_spawnable():
    c = Catalog.load(OLYMPUS_DIR, eras=["Late Cold War", "Modern"])
    tanks = sorted(n for n, i in c.items.items() if i.cls.startswith("tank"))
    assert tanks == ["T-72B", "T-72B3", "T-80UD", "T-90", "ZTZ96B"]  # T-90M and T-64BV come from the CHAP mod
    with_mods = Catalog.load(OLYMPUS_DIR, eras=["Late Cold War", "Modern"], allow_mods=True)
    assert "CHAP_T90M" in with_mods
    assert "SA-10 SAM Battery" in c and c["SA-10 SAM Battery"].is_template and c["SA-10 SAM Battery"].price == 150
    assert "SA-5 SAM Battery" not in c  # Mid Cold War, and Olympus has no template for it
    assert "CHAP_TorM2" not in c  # mod unit, excluded by default
    assert c["MiG-29S"].cls == "fighter" and c["MiG-29S"].a2a_loadouts
    assert "F-15C" not in c and "Su-25T" not in c  # blue, and no air-to-air loadout


def test_price_overrides():
    c = Catalog.load(OLYMPUS_DIR, price_overrides={"SA-10 SAM Battery": 200}, class_prices={"fighter": 55})
    assert c["SA-10 SAM Battery"].price == 200 and c["MiG-29S"].price == 55


# ---------- plan validation ----------

def test_good_plan_validates(scenario, inventory_catalog):
    plan = Plan.from_json(good_plan())
    errors, warnings = validate(plan, scenario, inventory_catalog, RED_BASES)
    assert errors == [] and warnings == []
    assert geo.distance_m(*KUTAISI, plan.ground_groups[0].lat, plan.ground_groups[0].lng) == pytest.approx(8000, rel=1e-3)


def test_validation_catches_mistakes(scenario, inventory_catalog):
    data = good_plan()
    data["ground_groups"][0]["count"] = 2  # template spawns a whole battery
    data["ground_groups"][3]["count"] = 5  # inventory only has 4 Shilkas
    data["ground_groups"][4]["distance_km"] = 90  # beyond the 60 km limit
    data["fighters"][0]["airbase"] = "Batumi"  # blue airbase
    data["fighters"][1]["loadout"] = "Kh-29L*2"
    errors, _ = validate(Plan.from_json(data), scenario, inventory_catalog, RED_BASES)
    text = " | ".join(errors)
    for expected in ["count must be 1", "plan uses 5, inventory has 4", "outside the allowed", "not a Red airbase", "loadout 'Kh-29L*2'"]:
        assert expected in text


def test_budget_is_enforced(budget_scenario):
    c = Catalog.load(OLYMPUS_DIR, eras=budget_scenario.eras, classes=budget_scenario.catalog.classes)
    data = {"summary": "", "fighters": [], "ground_groups": [
        {"name": f"SA10-{i}", "type": "SA-10 SAM Battery", "count": 1, "bearing_deg": 90 * i, "distance_km": 10, "heading_deg": 0, "reason": ""}
        for i in range(3)]}
    errors, _ = validate(Plan.from_json(data), budget_scenario, c, RED_BASES)
    assert any("costs 450 points, budget is 400" in e for e in errors)


def test_brief_shows_menu_without_blue_positions(budget_scenario):
    c = Catalog.load(OLYMPUS_DIR, eras=budget_scenario.eras, classes=budget_scenario.catalog.classes)
    brief = build_brief(budget_scenario, c, RED_BASES)
    assert brief["budget_points"] == 400 and brief["menu"]
    assert {b["name"] for b in brief["red_airbases_within_250km"]} == {"Kutaisi", "Senaki-Kolkhi"}
    assert all(card["class"] != "tank_modern" for card in brief["menu"])


# ---------- planner request and repair loop ----------

class FakeMessages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = json.dumps(self.replies.pop(0))
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])


def test_planner_repairs_an_invalid_plan(scenario, inventory_catalog):
    bad = good_plan()
    bad["ground_groups"][3]["count"] = 6
    fake = FakeMessages([bad, good_plan()])
    client = SimpleNamespace(beta=SimpleNamespace(messages=fake), messages=fake)
    plan, _ = Planner(PlannerConfig(), client=client).plan(scenario, inventory_catalog, RED_BASES)
    assert len(fake.calls) == 2
    first = fake.calls[0]
    assert first["model"] == "claude-opus-5-5" and first["fallbacks"] == "default"
    assert first["output_config"]["format"]["type"] == "json_schema"
    assert "inventory has 4" in fake.calls[1]["messages"][-1]["content"]
    assert plan.ground_groups[3].count == 4


# ---------- execution and watch loop against a fake Olympus ----------

@pytest.fixture
def olympus():
    fake = FakeOlympus()
    server, url = fake.serve()
    yield fake, OlympusClient(url, PASSWORD)
    server.shutdown()


def test_execute_then_scramble_and_wake_sams(scenario, inventory_catalog, olympus, tmp_path):
    fake, client = olympus
    plan, _ = StaticPlanner(good_plan()).plan(scenario, inventory_catalog, RED_BASES)
    cmd = Commander(client, scenario, inventory_catalog, RED_BASES, run_id="T", event_log=tmp_path / "events.jsonl")
    cmd.execute(plan)

    sent = fake.names_sent()
    assert sent.count("spawnGroundUnits") == 5 and sent.count("spawnAircrafts") == 1  # sweep only; alert pair waits
    alarm = {body["ID"]: body["alarmState"] for name, body in fake.commands if name == "setAlarmState"}
    sa11 = next(u for u in fake.units.values() if u.group_name == "RED-T-SA11-West")
    ewr = next(u for u in fake.units.values() if u.group_name == "RED-T-EWR")
    tor = next(u for u in fake.units.values() if u.group_name == "RED-T-Tor-1")
    assert alarm[sa11.id] == 1 and alarm[ewr.id] == 2 and alarm[tor.id] == 2  # SA-11 dark; EWR and Tor (point defence) on
    assert any(name == "setPath" and len(body["path"]) == 2 for name, body in fake.commands)
    assert sum(1 for u in fake.units.values() if u.group_name == "RED-T-AAA") == 4

    # A Blue fighter the EWR does not see: nothing happens.
    far_lat, far_lng = geo.project(*KUTAISI, 240, 60_000)
    bandit = fake.add(category="Aircraft", coalition=2, name="F-15C", group_name="Blue-1", lat=far_lat, lng=far_lng, alt=8000)
    fake.commands.clear()
    cmd.tick()
    assert "spawnAircrafts" not in fake.names_sent()

    # The EWR detects it 60 km out: one alert pair scrambles and attacks it.
    ewr.contacts = [bandit.id]
    cmd.tick()
    assert fake.names_sent().count("spawnAircrafts") == 1
    attack = [body for name, body in fake.commands if name == "attackUnit"]
    assert attack and attack[0]["targetID"] == bandit.id
    assert "setAlarmState" not in fake.names_sent()  # 60 km: SA-11 stays dark

    # Same contact next tick: no second scramble (and the alert pool is empty anyway).
    fake.commands.clear()
    cmd.tick()
    assert "spawnAircrafts" not in fake.names_sent()

    # It closes to 30 km of the SA-11: the SA-11 goes active.
    bandit.lat, bandit.lng = geo.project(sa11.lat, sa11.lng, 240, 30_000)
    cmd.tick()
    assert (("setAlarmState", {"ID": sa11.id, "alarmState": 2}) in fake.commands)

    events = [json.loads(l)["event"] for l in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert events.count("scramble") == 1 and "sam_active" in events


def test_groups_are_found_when_olympus_ignores_group_names(scenario, inventory_catalog, tmp_path):
    fake = FakeOlympus(honour_group_name=False)
    server, url = fake.serve()
    try:
        plan, _ = StaticPlanner(good_plan()).plan(scenario, inventory_catalog, RED_BASES)
        cmd = Commander(OlympusClient(url, PASSWORD), scenario, inventory_catalog, RED_BASES, run_id="T", event_log=tmp_path / "e.jsonl")
        cmd.execute(plan)
        assert all(name.startswith("Olympus-") for name in cmd.groups)
        sa11 = next(u for u in fake.units.values() if u.name == "SA-11 SAM Battery")
        ewr = next(u for u in fake.units.values() if u.name == "1L13 EWR")
        alarm = {body["ID"]: body["alarmState"] for name, body in fake.commands if name == "setAlarmState"}
        assert alarm[sa11.id] == 1 and alarm[ewr.id] == 2
        assert all(body["spawnPoints"] == 0 for name, body in fake.commands if name.startswith("spawn"))
    finally:
        server.shutdown()


def test_wrong_password_is_reported(olympus):
    fake, client = olympus
    bad = OlympusClient(client.base_url, "wrong")
    with pytest.raises(Exception, match="401"):
        bad.get_units()


# ---------- terrain, state and restarts ----------

def test_sites_on_water_are_moved_to_dry_ground(scenario, inventory_catalog, olympus, tmp_path):
    fake, client = olympus
    plan, _ = StaticPlanner(good_plan()).plan(scenario, inventory_catalog, RED_BASES)
    planned = (plan.ground_groups[0].lat, plan.ground_groups[0].lng)  # SA-11
    fake.terrain = lambda lat, lng: (5, 0.0, 0) if geo.distance_m(*planned, lat, lng) < 300 else (0, 0.02, 0)
    cmd = Commander(client, scenario, inventory_catalog, RED_BASES, run_id="T", event_log=tmp_path / "e.jsonl", work_dir=tmp_path)
    cmd.execute(plan)
    sa11 = next(u for u in fake.units.values() if u.group_name == "RED-T-SA11-West")
    assert 400 <= geo.distance_m(*planned, sa11.lat, sa11.lng) <= 600  # nearest dry ring is 500 m out
    moved = [json.loads(l) for l in (tmp_path / "e.jsonl").read_text().splitlines() if '"site_moved"' in l]
    assert [m["name"] for m in moved] == ["SA11-West"]


def test_state_lets_a_restarted_bridge_take_back_its_units(scenario, inventory_catalog, olympus, tmp_path):
    fake, client = olympus
    plan, _ = StaticPlanner(good_plan()).plan(scenario, inventory_catalog, RED_BASES)
    first = Commander(client, scenario, inventory_catalog, RED_BASES, run_id="T", session_hash="SESSION1",
                      state_path=tmp_path / "state.json", work_dir=tmp_path)
    first.execute(plan)
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["session_hash"] == "SESSION1" and len(state["groups"]) == 6 and state["alerts"][0]["remaining"] == 2

    second = Commander(client, scenario, inventory_catalog, RED_BASES, session_hash="SESSION1")
    second.restore(state)
    assert second.groups == first.groups and second.alerts == first.alerts


def test_watch_loop_survives_olympus_dropping_out_and_spots_a_mission_restart(scenario, inventory_catalog, olympus):
    fake, client = olympus
    scenario.rules.poll_seconds = 0.05
    offline = Commander(OlympusClient("http://127.0.0.1:9/olympus", PASSWORD), scenario, inventory_catalog, RED_BASES, session_hash="SESSION1")
    offline.run(stop_after_s=0.3)  # connection refused every tick: logs and keeps waiting, no exception

    cmd = Commander(client, scenario, inventory_catalog, RED_BASES, session_hash="SESSION1")
    fake.session_hash = "SESSION2"
    with pytest.raises(MissionRestarted):
        cmd.run(stop_after_s=2)


# ---------- ground forces: reserves and artillery ----------

@pytest.fixture
def combined_scenario():
    return load_scenario(ROOT / "scenarios/defend-kutaisi-combined.yaml")


@pytest.fixture
def combined_catalog(combined_scenario):
    return Catalog.load(OLYMPUS_DIR, eras=combined_scenario.eras, classes=combined_scenario.catalog.classes)


def ground_plan():
    group = lambda name, type_, count, bearing, dist, role, reason: {
        "name": name, "type": type_, "count": count, "bearing_deg": bearing, "distance_km": dist,
        "heading_deg": 240, "role": role, "reason": reason}
    return {
        "summary": "Forward BMPs watch the approach, T-72s wait in reserve, Msta and Uragan cover the axis.",
        "ground_groups": [
            group("Screen", "BMP-2", 2, 240, 20, "position", "Eyes on the approach"),
            group("Reserve", "T-72B", 4, 240, 5, "reserve", "Counter-attack force"),
            group("Msta", "SAU Msta", 2, 60, 3, "position", "Covers the axis to 20 km"),
            group("Uragan", "Uragan_BM-27", 1, 60, 10, "position", "Deep fires"),
        ],
        "fighters": [],
    }


def test_catalog_classes_ground_forces(combined_catalog):
    assert combined_catalog["BMP-2"].cls == "apc" and combined_catalog["BMP-2"].price == 6
    assert combined_catalog["SAU Msta"].cls == "artillery" and combined_catalog["SAU Msta"].price == 10
    assert combined_catalog["Uragan_BM-27"].cls == "mlrs" and combined_catalog["Uragan_BM-27"].price == 25


def test_only_armour_can_be_a_reserve(combined_scenario, combined_catalog):
    data = ground_plan()
    data["ground_groups"][2]["role"] = "reserve"
    errors, _ = validate(Plan.from_json(data), combined_scenario, combined_catalog, RED_BASES)
    assert any("Msta: only tanks and APCs can be a reserve" in e for e in errors)
    data["ground_groups"][2]["role"] = "position"
    assert validate(Plan.from_json(data), combined_scenario, combined_catalog, RED_BASES)[0] == []


def test_schema_and_brief_carry_ground_roles(combined_scenario, combined_catalog):
    from bridge.plan import json_schema
    schema = json_schema(combined_scenario, combined_catalog, sorted(RED_BASES))
    ground = schema["properties"]["ground_groups"]["items"]
    assert "role" in ground["required"] and ground["properties"]["role"]["enum"] == ["position", "reserve"]
    brief = build_brief(combined_scenario, combined_catalog, RED_BASES)
    assert brief["rules"]["reserve_react_within_km"] == 30
    assert {"apc", "artillery", "mlrs", "tank_modern"} <= {card["class"] for card in brief["menu"]}


def test_reserve_and_artillery_react_to_detected_ground_units(combined_scenario, combined_catalog, olympus, tmp_path):
    fake, client = olympus
    plan, _ = StaticPlanner(ground_plan()).plan(combined_scenario, combined_catalog, RED_BASES)
    cmd = Commander(client, combined_scenario, combined_catalog, RED_BASES, run_id="T", event_log=tmp_path / "e.jsonl")
    cmd.execute(plan)
    roles = {g.name: g.role for g in cmd.groups.values()}
    assert roles == {"RED-T-Screen": "ground", "RED-T-Reserve": "reserve", "RED-T-Msta": "artillery", "RED-T-Uragan": "artillery"}
    screen = next(u for u in fake.units.values() if u.group_name == "RED-T-Screen")
    reserve = next(u for u in fake.units.values() if u.group_name == "RED-T-Reserve" and u.is_leader)
    msta = next(u for u in fake.units.values() if u.group_name == "RED-T-Msta" and u.is_leader)
    uragan = next(u for u in fake.units.values() if u.group_name == "RED-T-Uragan")

    # Two Blue tanks 25 km out on the axis, 5 km beyond the screen. Not detected yet: nothing moves.
    t1 = fake.add(category="GroundUnit", coalition=2, name="M-1 Abrams", group_name="Blue-Armor", lat=0, lng=0)
    t2 = fake.add(category="GroundUnit", coalition=2, name="M-1 Abrams", group_name="Blue-Armor", lat=0, lng=0)
    t1.lat, t1.lng = geo.project(*KUTAISI, 240, 25_000)
    t2.lat, t2.lng = geo.project(t1.lat, t1.lng, 0, 100)
    fake.commands.clear()
    cmd.tick()
    assert "setPath" not in fake.names_sent() and "fireAtArea" not in fake.names_sent()

    # The screen sees them: the reserve drives at them, the Uragan fires (37 km); the Msta (23.5 km) is out of range.
    screen.contacts = [t1.id, t2.id]
    cmd.tick()
    paths = [body for name, body in fake.commands if name == "setPath"]
    assert [p["ID"] for p in paths] == [reserve.id]
    assert geo.distance_m(paths[0]["path"][0]["lat"], paths[0]["path"][0]["lng"], t1.lat, t1.lng) < 150
    fires = [body for name, body in fake.commands if name == "fireAtArea"]
    assert [f["ID"] for f in fires] == [uragan.id]

    # They push on to 18 km: the reserve is re-routed; the Uragan waits for its next mission; the Msta now fires.
    fake.commands.clear()
    for t in (t1, t2):
        t.lat, t.lng = geo.project(t.lat, t.lng, 60, 7_000)
    cmd.tick()
    assert [body["ID"] for name, body in fake.commands if name == "setPath"] == [reserve.id]
    assert [body["ID"] for name, body in fake.commands if name == "fireAtArea"] == [msta.id]

    # Blue reaches the screen: too close to friendly vehicles, no more fire on it even once batteries are ready.
    for g in cmd.groups.values():
        g.last_fire = 0.0
    t1.lat, t1.lng = geo.project(screen.lat, screen.lng, 240, 200)
    t2.alive = False
    fake.commands.clear()
    cmd.tick()
    assert "fireAtArea" not in fake.names_sent()

    # Blue group destroyed: the reserve goes back to its position.
    t1.alive = False
    fake.commands.clear()
    cmd.tick()
    back = [body for name, body in fake.commands if name == "setPath"]
    home = cmd.groups["RED-T-Reserve"]
    assert len(back) == 1 and geo.distance_m(back[0]["path"][0]["lat"], back[0]["path"][0]["lng"], home.lat, home.lng) < 1

    events = [json.loads(l)["event"] for l in (tmp_path / "e.jsonl").read_text().splitlines()]
    assert events.count("reserve_sent") == 1 and events.count("fire_mission") == 2 and events.count("reserve_return") == 1


def test_reserve_ignores_ground_contacts_outside_its_ring(combined_scenario, combined_catalog, olympus, tmp_path):
    fake, client = olympus
    data = ground_plan()
    data["ground_groups"] = data["ground_groups"][:2]
    plan, _ = StaticPlanner(data).plan(combined_scenario, combined_catalog, RED_BASES)
    cmd = Commander(client, combined_scenario, combined_catalog, RED_BASES, run_id="T")
    cmd.execute(plan)
    screen = next(u for u in fake.units.values() if u.group_name == "RED-T-Screen")
    lat, lng = geo.project(*KUTAISI, 240, 40_000)
    far = fake.add(category="GroundUnit", coalition=2, name="M-1 Abrams", group_name="Blue-Far", lat=lat, lng=lng)
    screen.contacts = [far.id]
    fake.commands.clear()
    cmd.tick()
    assert "setPath" not in fake.names_sent()
