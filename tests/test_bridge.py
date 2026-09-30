import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bridge import geo
from bridge.catalog import Catalog
from bridge.commander import Commander
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
    assert alarm[sa11.id] == 1 and alarm[ewr.id] == 2  # SA-11 dark (green), EWR on (red)
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
