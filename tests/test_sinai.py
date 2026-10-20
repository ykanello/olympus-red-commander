"""Commanding the mission's own forces: warehouse airframes, mission ground groups, defend zones (Sinai scenario)."""

import json
import zipfile

import pytest
import yaml

from bridge import geo
from bridge.campaign import CampaignCommander, CampaignPlan, CampaignPlanner, campaign_schema, validate_campaign
from bridge.mission import airframe_stock, parse_lua_assignment, read_warehouses
from bridge.olympus import OlympusClient
from bridge.planner import PlannerConfig
from bridge.scenario import load_scenario

from .fake_olympus import PASSWORD, FakeOlympus
from .test_bridge import OLYMPUS_DIR, ROOT, FakeMessages

ST_CATHERINE = (28.6852, 34.0626)
ABU_RUDEIS = (28.8995, 33.2023)
BASES = {"Abu Rudeis": ABU_RUDEIS, "Wadi Abu Rish": (28.971, 31.6987), "Melez": (30.4099, 33.1497)}

# As the mission editor writes it, comments and all.
WAREHOUSES = '''warehouses =
{
    ["airports"] =
    {
        [26] =
        {
            ["coalition"] = "RED",
            ["unlimitedAircrafts"] = false,
            ["aircrafts"] =
            {
                ["planes"] =
                {
                    ["Su-25T"] =
                    {
                        ["initialAmount"] = 2,
                        ["wsType"] = { [1] = 1, [2] = 1, [3] = 1, [4] = 54, }, -- end of ["wsType"]
                    }, -- end of ["Su-25T"]
                    ["MiG-29 Fulcrum"] = { ["initialAmount"] = 2, },
                    ["MiG-29S"] = { ["initialAmount"] = 1, },
                    ["F-14BU"] = { ["initialAmount"] = 100, },
                    ["MiG-31"] = { ["initialAmount"] = 0, },
                }, -- end of ["planes"]
                ["helicopters"] = {},
            }, -- end of ["aircrafts"]
            ["dynamicSpawn"] = false,
            ["speed"] = 16.666666,
            ["name"] = "Abu \\"Rudeis\\"",
        }, -- end of [26]
        [43] =
        {
            ["coalition"] = "RED",
            ["unlimitedAircrafts"] = false,
            ["aircrafts"] = { ["planes"] = { ["MiG-29S"] = { ["initialAmount"] = 4, }, }, },
        },
        [5] =
        {
            ["coalition"] = "RED",
            ["unlimitedAircrafts"] = true,
            ["aircrafts"] = { ["planes"] = { ["Su-27"] = { ["initialAmount"] = 1, }, }, },
        },
    }, -- end of ["airports"]
    ["warehouses"] = {},
} -- end of warehouses
'''


@pytest.fixture
def miz(tmp_path):
    path = tmp_path / "Sinai Base Mission.miz"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mission", "mission = {}")
        z.writestr("warehouses", WAREHOUSES)
    return path


@pytest.fixture
def scenario(miz, tmp_path):
    raw = yaml.safe_load((ROOT / "scenarios/sinai-st-catherine.yaml").read_text(encoding="utf-8"))
    raw["warehouses"].update(mission_file=str(miz), airbases={"Abu Rudeis": 26, "Wadi Abu Rish": 43},
                             spawn_as={"MiG-29 Fulcrum": "MiG-29S"})
    raw["campaign"]["command_groups"] = ["Ground-3", "Red RT", "Infantry-1", "Late-Group"]
    path = tmp_path / "sinai.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_scenario(path)


@pytest.fixture
def catalog(scenario):
    from bridge.__main__ import build_catalog
    return build_catalog(scenario, OLYMPUS_DIR)


@pytest.fixture
def olympus():
    fake = FakeOlympus()
    fake.airbases = {str(i): {"callsign": n, "coalition": "red", "latitude": ll[0], "longitude": ll[1]}
                     for i, (n, ll) in enumerate(BASES.items(), 1)}
    fake.airbases["9"] = {"callsign": "St Catherine", "coalition": "blue", "latitude": ST_CATHERINE[0], "longitude": ST_CATHERINE[1]}
    west = geo.project(*ST_CATHERINE, 270, 8000)
    for i, t in enumerate(["T-55", "T-55", "BMP-2", "BMP-2"]):
        fake.add(category="GroundUnit", coalition=1, name=t, group_name="Ground-3", lat=west[0], lng=west[1] - i * 0.0005, is_leader=i == 0)
    guns = geo.project(*ABU_RUDEIS, 90, 28000)
    for i, t in enumerate(["SAU Msta", "SAU Msta", "GAZ-66"]):
        fake.add(category="GroundUnit", coalition=1, name=t, group_name="Red RT", lat=guns[0], lng=guns[1] + i * 0.0005, is_leader=i == 0)
    for i in range(3):
        fake.add(category="GroundUnit", coalition=1, name="Soldier AK", group_name="Infantry-1", lat=ABU_RUDEIS[0], lng=ABU_RUDEIS[1],
                 is_leader=i == 0)
    # Red's air defence and missiles: left to the mission and Skynet.
    fake.add(category="GroundUnit", coalition=1, name="SA-11 Buk LN 9A310M1", group_name="SAM-6", lat=28.68, lng=32.83)
    fake.add(category="GroundUnit", coalition=1, name="Scud_B", group_name="Scud", lat=28.33, lng=33.08)
    server, url = fake.serve()
    yield fake, OlympusClient(url, PASSWORD)
    server.shutdown()


def opening():
    return {
        "summary": "Ground-3 pushes into St Catherine under the guns; Infantry-1 holds Abu Rudeis; MiG-29s on alert.",
        "orders": [
            {"group": "Ground-3", "action": "move", "route": [{"bearing_deg": 0, "distance_km": 0.5}], "reason": "Take the airport"},
            {"group": "Red RT", "action": "move", "route": [{"bearing_deg": 265, "distance_km": 15}], "reason": "In range of the target"},
        ],
        "air": [
            {"type": "MiG-29S", "count": 4, "role": "intercept", "airbase": "Wadi Abu Rish", "loadout": "R-73*2,R-60M*2,R-27R*2",
             "sweep_route": [], "reason": "Cover"},
            {"type": "Su-25T", "count": 2, "role": "cas", "airbase": "Abu Rudeis", "loadout": "RBK-500AO*4,UB-32*2,R-60M*2,Fuel*2",
             "sweep_route": [], "reason": "On call for Abu Rudeis"},
        ],
    }


def test_lua_tables_as_the_mission_editor_writes_them():
    name, data = parse_lua_assignment('x = { ["a"] = "say \\"hi\\"", [2] = -1.5e2, [3] = { true, false, nil, }, b = 4; } -- end')
    assert name == "x" and data == {"a": 'say "hi"', 2: -150.0, 3: {1: True, 2: False, 3: None}, "b": 4}


def test_warehouses_give_the_airframes_per_airbase(miz):
    w = read_warehouses(miz)
    assert w[26] == {"coalition": "red", "unlimited": False, "airframes": {"Su-25T": 2, "MiG-29 Fulcrum": 2, "MiG-29S": 1, "F-14BU": 100}}
    stock = airframe_stock(miz, {"Abu Rudeis": 26, "Wadi Abu Rish": 43, "Melez": 5}, ignore=["F-14BU"],
                           spawn_as={"MiG-29 Fulcrum": "MiG-29S"})
    assert stock == {"Abu Rudeis": {"Su-25T": 2, "MiG-29S": 3}, "Wadi Abu Rish": {"MiG-29S": 4}, "Melez": {"Su-27": 999}}
    with pytest.raises(ValueError, match="airport ID 7"):
        airframe_stock(miz, {"Nowhere": 7})


def test_sinai_scenario_and_menu(scenario, catalog):
    camp = scenario.campaign
    assert not camp.spawn_ground and camp.hq == "Abu Rudeis" and scenario.budget_points is None
    assert [z.name for z in camp.defend] == ["Abu Rudeis airport", "Abu Rudeis seaport", "Missile base (El Tor)"]
    assert scenario.warehouses.stock == {"Abu Rudeis": {"Su-25T": 2, "MiG-29S": 3}, "Wadi Abu Rish": {"MiG-29S": 4}}
    # Only what the warehouses hold, at no cost; the stock is the limit.
    assert catalog.names("aircraft") == ["MiG-29S", "Su-25T"] and catalog["MiG-29S"].price == 0
    schema = campaign_schema(catalog, ground=False, orders=True, airbases=sorted(scenario.warehouses.stock))
    assert "convoys" not in schema["properties"] and "artillery" not in schema["properties"] and "orders" in schema["properties"]
    assert schema["properties"]["air"]["items"]["properties"]["airbase"]["enum"] == ["Abu Rudeis", "Wadi Abu Rish"]


def test_validation_keeps_to_the_warehouses_and_spawns_no_ground_units(scenario, catalog):
    labels = ["Ground-3", "Red RT", "Infantry-1"]
    stock = scenario.warehouses.stock
    plan = CampaignPlan.from_json(opening(), "Abu Rudeis")
    assert validate_campaign(plan, scenario, catalog, None, labels, stock, opening=True) == ([], [])

    data = opening()
    data["air"][0]["count"] = 6
    data["air"].append({**data["air"][1], "airbase": "Melez"})
    data["convoys"] = [{"name": "New", "elements": [{"type": "T-55", "count": 2}], "depart_min": 0, "route": [], "reason": ""}]
    errors, _ = validate_campaign(CampaignPlan.from_json(data, "Abu Rudeis"), scenario, catalog, None, labels, stock, opening=True)
    text = " | ".join(errors)
    for expected in ["No ground units can be bought", "Wadi Abu Rish has 4 MiG-29S left, the plan flies 6", "'Melez' is not one of"]:
        assert expected in text


def test_opening_brief_shows_forces_airframes_and_defend_zones(scenario, catalog, olympus):
    fake, client = olympus
    situation = CampaignCommander(client, scenario, catalog, BASES).opening_report(client.get_units())
    groups = {g["name"]: g for g in situation["red_ground_groups"]}
    assert set(groups) == {"Ground-3", "Red RT", "Infantry-1"}  # not the SAMs or the Scuds, which the mission runs
    assert groups["Red RT"]["role"] == "artillery" and groups["Ground-3"]["status"] == "mission orders"
    assert situation["groups_not_in_mission_yet"] == ["Late-Group"] and "points_left" not in situation
    assert situation["airframes_left"] == {"Abu Rudeis": {"Su-25T": 2, "MiG-29S": 3}, "Wadi Abu Rish": {"MiG-29S": 4}}

    from types import SimpleNamespace
    fake_api = FakeMessages([opening()])
    planner = CampaignPlanner(PlannerConfig(), client=SimpleNamespace(beta=SimpleNamespace(messages=fake_api), messages=fake_api))
    plan, _ = planner.plan_campaign(scenario, catalog, BASES, BASES["Abu Rudeis"], situation)
    assert plan.air[0].airbase == "Wadi Abu Rish" and plan.orders[0].group == "Ground-3"
    call = fake_api.calls[0]
    assert "already in the mission" in call["system"] and "Buy" not in call["system"]
    static, extra = call["messages"][0]["content"]
    brief = json.loads(static["text"])
    assert "staging_area" not in brief and "budget_points" not in brief
    assert {b["name"]: b["airframes"] for b in brief["airbases"]} == scenario.warehouses.stock
    assert [z["name"] for z in brief["defend_zones"]] == ["Abu Rudeis airport", "Abu Rudeis seaport", "Missile base (El Tor)"]
    assert {c["type"] for c in brief["menu"]} == {"MiG-29S", "Su-25T"}  # no ground units to buy
    assert "Missiles:" in call["system"] and "Ovda" in brief["missile_intent"]
    assert "C-130" in brief["commander_notes"] and "commander_notes" in call["system"]
    assert brief["rules"]["alert_pairs_go_first_after"] == ["C-130J-30", "C-130"]
    assert "Ovda storage 1" in [t["name"] for t in brief["strike_targets"]]
    assert call["output_config"]["format"]["schema"]["properties"]["strikes"]["items"]["properties"]["group"]["enum"] == [
        "Iskander-first", "Iskander-second", "Iskander-5", "Scud"]
    assert json.loads(extra["text"])["situation"]["red_ground_groups"]


def test_command_campaign_runs_on_the_missions_own_forces(scenario, catalog, olympus, tmp_path):
    fake, client = olympus
    reports = []

    def replanner(report):
        reports.append(report)
        return CampaignPlan.from_json({
            "summary": "Blue is at Abu Rudeis: Infantry-1 holds, two more MiG-29S from Wadi Abu Rish, but that is all there are.",
            "orders": [{"group": "Infantry-1", "action": "hold", "route": [], "reason": "Hold the airport"}],
            "air": [{"type": "MiG-29S", "count": 2, "role": "intercept", "airbase": "Abu Rudeis", "loadout": "R-73*2,R-60M*2,R-27R*2",
                     "sweep_route": [], "reason": "More cover"}],
        }, "Abu Rudeis")

    cmd = CampaignCommander(client, scenario, catalog, BASES, run_id="T", event_log=tmp_path / "e.jsonl",
                            replanner=replanner, review_async=False)
    fake.commands.clear()
    cmd.execute(CampaignPlan.from_json(opening(), "Abu Rudeis"))
    sent = fake.names_sent()
    assert "spawnGroundUnits" not in sent and "spawnAircrafts" not in sent  # nothing spawned: both air taskings wait on alert
    leader = lambda group: next(u for u in fake.units.values() if u.group_name == group and u.is_leader)
    moved = {b["ID"] for n, b in fake.commands if n == "setPath"}
    assert moved == {leader("Ground-3").id, leader("Red RT").id}
    sam_ids = {u.id for u in fake.units.values() if u.group_name in ("SAM-6", "Scud")}
    assert not any(b.get("ID") in sam_ids for _, b in fake.commands)  # the mission's SAMs and missiles are never touched
    assert cmd.airframes_left == {"Abu Rudeis": {"Su-25T": 0, "MiG-29S": 3}, "Wadi Abu Rish": {"MiG-29S": 0}}

    # A Blue vehicle at Abu Rudeis, seen by Infantry-1: the zone is threatened, Su-25s scramble, Claude reviews.
    blue = fake.add(category="GroundUnit", coalition=2, name="M-1 Abrams", group_name="Blue-1",
                    lat=ABU_RUDEIS[0] + 0.005, lng=ABU_RUDEIS[1])
    leader("Infantry-1").contacts = [blue.id]
    cmd.last_review -= 300
    fake.commands.clear()
    cmd.tick()
    spawned = [b for n, b in fake.commands if n == "spawnAircrafts"]
    assert len(spawned) == 1 and spawned[0]["units"][0]["unitType"] == "Su-25T" and spawned[0]["airbaseName"] == "Abu Rudeis"
    assert len(reports) == 1 and "defence_threatened" in reports[0]["why_now"]
    zones = {z["name"]: z for z in reports[0]["defend_zones"]}
    assert zones["Abu Rudeis airport"]["enemy_detected_inside"] and zones["Abu Rudeis airport"]["your_ground_units_inside"] == 3
    assert not zones["Abu Rudeis seaport"]["enemy_detected_inside"]
    # The review's air tasking is paid from the warehouse.
    assert cmd.airframes_left["Abu Rudeis"]["MiG-29S"] == 1
    assert [a.airbase for a in cmd.alerts if a.role == "intercept"] == ["Wadi Abu Rish", "Abu Rudeis"]

    # A review that flies more than is left gets its air taskings dropped.
    cmd._apply_review(CampaignPlan.from_json({"summary": "More!", "orders": [], "air": [
        {"type": "MiG-29S", "count": 2, "role": "intercept", "airbase": "Abu Rudeis", "loadout": "R-73*2,R-60M*2,R-27R*2",
         "sweep_route": [], "reason": ""}]}, "Abu Rudeis"))
    assert cmd.airframes_left["Abu Rudeis"]["MiG-29S"] == 1 and len(cmd.alerts) == 3

    # Blue gone; Ground-3 reaches St Catherine and holds it.
    blue.alive = False
    for u in fake.units.values():
        if u.group_name == "Ground-3":
            u.lat, u.lng = geo.project(*ST_CATHERINE, 0, 400)
    cmd.tick()
    assert cmd.threatened == [] and cmd.hold_since is not None
    cmd.hold_since -= 11 * 60
    cmd.tick()
    assert cmd.outcome == "won"

    # A group that appears later (a late activation) is taken under command then.
    fake.add(category="GroundUnit", coalition=1, name="T-55", group_name="Late-Group", lat=ABU_RUDEIS[0], lng=ABU_RUDEIS[1])
    cmd.tick()
    assert "Late-Group" in cmd.groups and cmd.awaiting == []

    events = [json.loads(l)["event"] for l in (tmp_path / "e.jsonl").read_text().splitlines()]
    for kind in ["command_taken", "order", "defence_threatened", "scramble", "review", "defence_clear", "target_taken"]:
        assert kind in events


def test_artillery_of_the_mission_fires_with_its_own_range(scenario, catalog, olympus):
    fake, client = olympus
    cmd = CampaignCommander(client, scenario, catalog, BASES, run_id="T")
    cmd.execute(CampaignPlan.from_json({"summary": "Wait.", "orders": [], "air": []}, "Abu Rudeis"))
    guns = next(u for u in fake.units.values() if u.group_name == "Red RT" and u.is_leader)
    reach_km = catalog.describe("SAU Msta").engagement_range_m / 1000
    lat, lng = geo.project(guns.lat, guns.lng, 0, reach_km * 1000 * 0.8)
    blue = fake.add(category="GroundUnit", coalition=2, name="M-1 Abrams", group_name="Blue-2", lat=lat, lng=lng)
    guns.contacts = [blue.id]
    fake.commands.clear()
    cmd.tick()
    assert ("fireAtArea", guns.id) in [(n, b["ID"]) for n, b in fake.commands if n == "fireAtArea"]


def test_lost_only_when_every_commanded_group_is_gone(scenario, catalog, olympus):
    fake, client = olympus
    scenario.campaign.command_groups = ["Ground-3", "Infantry-1"]
    cmd = CampaignCommander(client, scenario, catalog, BASES, run_id="T")
    cmd.execute(CampaignPlan.from_json({"summary": "Hold.", "orders": [], "air": []}, "Abu Rudeis"))
    for u in fake.units.values():
        if u.group_name == "Ground-3":
            u.alive = False
    cmd.tick()
    assert cmd.outcome == ""
    for u in fake.units.values():
        if u.group_name == "Infantry-1":
            u.alive = False
    cmd.tick()
    assert cmd.outcome == "lost"



def test_missiles_wait_for_the_mission_strike_then_take_claudes_orders(scenario, catalog, olympus, tmp_path):
    fake, client = olympus
    for u in fake.units.values():
        if u.group_name == "Scud":
            u.ammo = {"Scud_B": 1}
    second = fake.add(category="GroundUnit", coalition=1, name="Scud_B", group_name="Scud", lat=28.331, lng=33.081, is_leader=False,
                      ammo={"Scud_B": 1})
    reports = []
    strike = {"group": "Scud", "target": "Ovda storage 1", "rounds": 1, "reason": "Finish the ammunition storage"}

    def replanner(report):
        reports.append(report)
        return CampaignPlan.from_json({"summary": "Ovda still flies: strike it again.", "orders": [], "air": [], "strikes": [strike]},
                                      "Abu Rudeis")

    cmd = CampaignCommander(client, scenario, catalog, BASES, run_id="T", event_log=tmp_path / "e.jsonl", work_dir=tmp_path,
                            replanner=replanner, review_async=False)
    cmd.execute(CampaignPlan.from_json({"summary": "Wait.", "orders": [], "air": []}, "Abu Rudeis"))
    assert cmd.batteries["Scud"]["status"] == "waiting for the mission's strike" and cmd.batteries["Scud"]["missiles_left"] == 2
    # Before the mission's strike, Claude cannot use the battery.
    report = cmd.situation(client.get_units())
    plan = CampaignPlan.from_json({"summary": "", "strikes": [strike]}, "Abu Rudeis")
    errors, _ = validate_campaign(plan, scenario, catalog, None, ["Ground-3"], None, situation=report)
    assert errors == ["Strike by Scud: the battery is waiting for the mission's strike, not ready"]

    # The mission's strike: one Scud launches; three quiet minutes later the battery is Claude's, and that calls a review.
    second.ammo = {"Scud_B": 0}
    cmd.tick()
    assert cmd.batteries["Scud"]["status"] == "firing" and not reports
    cmd.batteries["Scud"]["last_launch"] -= 200
    cmd.last_review -= 300
    fake.commands.clear()
    cmd.tick()
    assert len(reports) == 1 and "missiles_ready" in reports[0]["why_now"]
    battery = next(b for b in reports[0]["missile_batteries"] if b["group"] == "Scud")
    assert battery["status"] == "ready" and battery["missiles_left"] == 1 and battery["range_km"] == 300
    # The review's strike goes out as a FireAtPoint task for that group, with its rounds.
    files = [b["filePath"] for n, b in fake.commands if n == "executeFile"]
    assert len(files) == 1
    lua = open(files[0], encoding="utf-8").read()
    assert 'Group.getByName("Scud")' in lua and "FireAtPoint" in lua and "expendQty = 1" in lua and "29.968100, 34.962000" in lua
    assert cmd.batteries["Scud"]["status"] == "firing"

    # Too far, unknown target, too many rounds: back to Claude for repair.
    report = cmd.situation(client.get_units())
    next(b for b in report["missile_batteries"] if b["group"] == "Scud")["status"] = "ready"
    bad = CampaignPlan.from_json({"summary": "", "strikes": [{**strike, "rounds": 3}, {**strike, "target": "Cairo"}]}, "Abu Rudeis")
    text = " | ".join(validate_campaign(bad, scenario, catalog, None, ["Ground-3"], None, situation=report)[0])
    for expected in ["3 rounds, it has 1 missiles left", "'Cairo' is neither", "only one strike order at a time"]:
        assert expected in text

    # No strike close to Red ground units.
    cmd.batteries["Scud"]["status"] = "ready"
    for u in fake.units.values():
        if u.group_name == "Ground-3":
            u.lat, u.lng = 28.6470, 33.9950
    fake.commands.clear()
    cmd._strike(CampaignPlan.from_json({"summary": "", "strikes": [{**strike, "target": "Blue firebase west of St Catherine"}]},
                                       "Abu Rudeis").strikes[0])
    assert "executeFile" not in fake.names_sent()
    events = [json.loads(l) for l in (tmp_path / "e.jsonl").read_text().splitlines()]
    assert events[-1]["event"] == "strike_refused" and "Red ground units within 2000 m" in events[-1]["why"]
    assert {"missile_launch", "missiles_ready", "missile_strike"} <= {e["event"] for e in events}


def test_alert_pairs_go_after_the_transport_first(scenario, catalog, olympus):
    fake, client = olympus
    cmd = CampaignCommander(client, scenario, catalog, BASES, run_id="T")
    cmd.execute(CampaignPlan.from_json({"summary": "Cover Abu Rudeis.", "orders": [], "air": [
        {"type": "MiG-29S", "count": 2, "role": "intercept", "airbase": "Abu Rudeis", "loadout": "R-73*2,R-60M*2,R-27R*2",
         "sweep_route": [], "reason": "One pair"}]}, "Abu Rudeis"))
    near = geo.project(*ABU_RUDEIS, 90, 10000)
    far = geo.project(*ABU_RUDEIS, 90, 40000)
    fighter = fake.add(category="Aircraft", coalition=2, name="F-16C_50", group_name="Viper-1", lat=near[0], lng=near[1], alt=6000)
    transport = fake.add(category="Aircraft", coalition=2, name="C-130J-30", group_name="Trash", lat=far[0], lng=far[1], alt=3000)
    next(u for u in fake.units.values() if u.group_name == "Infantry-1").contacts = [fighter.id, transport.id]
    fake.commands.clear()
    cmd.tick()
    assert [b["targetID"] for n, b in fake.commands if n == "attackUnit"] == [transport.id]


def test_a_spawn_dcs_refuses_is_not_a_loss(scenario, catalog, olympus, tmp_path):
    fake, client = olympus
    reports = []
    cmd = CampaignCommander(client, scenario, catalog, BASES, run_id="T", event_log=tmp_path / "e.jsonl",
                            replanner=lambda r: reports.append(r) or CampaignPlan.from_json(
                                {"summary": "Hold.", "orders": [], "air": []}, "Abu Rudeis"),
                            review_async=False, spawn_timeout_s=0.5)
    cmd.execute(CampaignPlan.from_json({"summary": "Cover Abu Rudeis.", "orders": [], "air": [
        {"type": "MiG-29S", "count": 4, "role": "intercept", "airbase": "Wadi Abu Rish", "loadout": "R-73*2,R-60M*2,R-27R*2",
         "sweep_route": [], "reason": "Two pairs"}]}, "Abu Rudeis"))
    assert cmd.airframes_left["Wadi Abu Rish"]["MiG-29S"] == 0
    near = geo.project(*ABU_RUDEIS, 90, 10000)
    heli = fake.add(category="Helicopter", coalition=2, name="AH-64D_BLK_II", group_name="Apache", lat=near[0], lng=near[1], alt=100)
    next(u for u in fake.units.values() if u.group_name == "Infantry-1").contacts = [heli.id]

    # DCS refuses the ramp spawn but takes the air spawn above the base: the pair flies.
    fake.refuse_spawn = lambda body: body["airbaseName"] != ""
    cmd.last_review -= 300
    fake.commands.clear()
    cmd.tick()
    spawns = [b for n, b in fake.commands if n == "spawnAircrafts"]
    assert [b["airbaseName"] for b in spawns] == ["Wadi Abu Rish", ""]
    assert [b["targetID"] for n, b in fake.commands if n == "attackUnit"] == [heli.id]
    assert cmd.alerts[0].remaining == 2 and not cmd.spawn_failures

    # Now DCS refuses both: nothing is lost, the slot stands down, its airframes go back, and Claude hears why.
    for u in fake.units.values():
        if u.category == "Aircraft" and u.coalition == 1:
            u.alive = False
    fake.refuse_spawn = lambda body: True
    cmd.last_review -= 300
    cmd.tick()
    events = [json.loads(l) for l in (tmp_path / "e.jsonl").read_text().splitlines()]
    failed = [e for e in events if e["event"] == "spawn_failed"]
    assert len(failed) == 1 and failed[0]["airframes_returned"] == 2
    assert cmd.alerts[0].remaining == 0 and cmd.airframes_left["Wadi Abu Rish"]["MiG-29S"] == 2
    assert ("Wadi Abu Rish", "MiG-29S", "R-73*2,R-60M*2,R-27R*2") in cmd.spawn_failures
    assert reports and "spawn_failed" in reports[-1]["why_now"]
    assert reports[-1]["spawn_failures"] == [{"airbase": "Wadi Abu Rish", "type": "MiG-29S", "loadout": "R-73*2,R-60M*2,R-27R*2"}]
    assert not any(e["event"] == "group_lost" and e.get("type") == "MiG-29S" and e["group"].startswith("INT2") for e in events)

    # The same airbase, type and loadout cannot be tasked again.
    plan = CampaignPlan.from_json({"summary": "Again.", "orders": [], "air": [
        {"type": "MiG-29S", "count": 2, "role": "intercept", "airbase": "Wadi Abu Rish", "loadout": "R-73*2,R-60M*2,R-27R*2",
         "sweep_route": [], "reason": ""}]}, "Abu Rudeis")
    errors = validate_campaign(plan, scenario, catalog, None, ["Ground-3"], reports[-1]["airframes_left"], opening=False,
                               situation=reports[-1])[0]
    assert any("did not spawn" in e for e in errors)
