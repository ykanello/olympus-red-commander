import json
import time

import pytest

from bridge import geo
from bridge.campaign import CampaignCommander, CampaignPlan, campaign_schema, validate_campaign
from bridge.catalog import Catalog
from bridge.olympus import OlympusClient
from bridge.planner import PlannerConfig
from bridge.campaign import CampaignPlanner
from bridge.scenario import load_scenario

from .fake_olympus import PASSWORD, FakeOlympus
from .test_bridge import OLYMPUS_DIR, ROOT, FakeMessages

KUTAISI = (42.176, 42.482)
BASES = {"Sochi-Adler": (43.44, 39.94), "Kutaisi": KUTAISI}


@pytest.fixture
def scenario():
    return load_scenario(ROOT / "scenarios/take-kutaisi.yaml")


@pytest.fixture
def catalog(scenario):
    return Catalog.load(OLYMPUS_DIR, eras=scenario.eras)


@pytest.fixture
def olympus():
    fake = FakeOlympus()
    fake.airbases["4"] = {"callsign": "Sochi-Adler", "coalition": "red", "latitude": 43.44, "longitude": 39.94}
    server, url = fake.serve()
    yield fake, OlympusClient(url, PASSWORD)
    server.shutdown()


def opening():
    return {
        "summary": "Armour drives down the main road with its own Tor, Msta covers it, Su-25s on call, MiGs guard the column.",
        "convoys": [
            {"name": "Armour-1", "elements": [{"type": "T-72B", "count": 4}, {"type": "Tor 9A331", "count": 1}], "depart_min": 0,
             "route": [{"bearing_deg": 280, "distance_km": 30}, {"bearing_deg": 0, "distance_km": 0.5}], "reason": "Main thrust"},
            {"name": "Reserve", "elements": [{"type": "BMP-2", "count": 3}], "depart_min": 0, "route": [], "reason": "Held back"},
        ],
        "artillery": [
            {"name": "Msta", "type": "SAU Msta", "count": 2, "depart_min": 0, "firing_position": {"bearing_deg": 280, "distance_km": 15},
             "reason": "Reaches the target"},
        ],
        "air": [
            {"type": "Su-25T", "count": 2, "role": "cas", "loadout": "RBK-500AO*4,UB-32*2,R-60M*2,Fuel*2", "sweep_route": [], "reason": "On call"},
            {"type": "MiG-29S", "count": 2, "role": "intercept", "loadout": "R-73*2,R-60M*2,R-27R*2", "sweep_route": [], "reason": "Cover"},
        ],
    }


def test_campaign_scenario_and_menu(scenario, catalog):
    assert scenario.campaign.hq == "Sochi-Adler" and scenario.campaign.staging_lat == 42.627
    schema = campaign_schema(catalog)
    convoy_types = schema["properties"]["convoys"]["items"]["properties"]["elements"]["items"]["properties"]["type"]["enum"]
    assert "T-72B" in convoy_types and "Tor 9A331" in convoy_types
    assert "SA-11 SAM Battery" not in convoy_types and "SAU Msta" not in convoy_types  # fixed battery; artillery has its own list
    assert "orders" not in schema["properties"]
    review = campaign_schema(catalog, review=True)
    assert "enum" not in review["properties"]["orders"]["items"]["properties"]["group"]  # same schema every review


def test_campaign_validation_catches_mistakes(scenario, catalog):
    plan = CampaignPlan.from_json(opening(), "Sochi-Adler")
    assert validate_campaign(plan, scenario, catalog, 800) == ([], [])
    assert plan.cost(catalog) == 4 * 15 + catalog["Tor 9A331"].price + 3 * 6 + 2 * 10 + 2 * 35 + 2 * 40

    data = opening()
    data["convoys"][0]["elements"].append({"type": "SA-11 SAM Battery", "count": 1})
    data["convoys"][1]["elements"] = [{"type": "BMP-2", "count": 13}]
    data["convoys"][1]["route"] = [{"bearing_deg": 0, "distance_km": 0}]
    data["convoys"].append({"name": "Guns", "elements": [{"type": "SAU Msta", "count": 1}], "depart_min": 0, "route": [], "reason": ""})
    data["air"][0]["loadout"] = "R-73*2,R-60M*2,R-27R*2"
    data["orders"] = [{"group": "Ghost", "action": "move", "route": [], "reason": ""}]
    errors, _ = validate_campaign(CampaignPlan.from_json(data, "Sochi-Adler"), scenario, catalog, 100)
    text = " | ".join(errors)
    for expected in ["fixed battery", "13 vehicles", "in the artillery list", "ground_attack_loadouts", "no such group",
                     "move needs a route", "only 100 are available"]:
        assert expected in text

    data = opening()
    data["convoys"][0]["route"] = [{"bearing_deg": 280, "distance_km": 30}]
    _, warnings = validate_campaign(CampaignPlan.from_json(data, "Sochi-Adler"), scenario, catalog, 800)
    assert warnings == ["No convoy's route ends inside the target zone"]


def test_campaign_planner_asks_with_the_campaign_brief(scenario, catalog):
    from types import SimpleNamespace
    fake = FakeMessages([opening()])
    client = SimpleNamespace(beta=SimpleNamespace(messages=fake), messages=fake)
    plan, _ = CampaignPlanner(PlannerConfig(), client=client).plan_campaign(scenario, catalog, BASES, (42.627, 41.735))
    assert [c.name for c in plan.convoys] == ["Armour-1", "Reserve"] and plan.air[0].airbase == "Sochi-Adler"
    call = fake.calls[0]
    assert "offensive" in call["system"]
    static, extra = call["messages"][0]["content"]
    assert static["cache_control"] == {"type": "ephemeral", "ttl": "1h"} and "cache_control" not in extra
    assert "opening" in json.loads(extra["text"])["instruction"]
    brief = json.loads(static["text"])
    assert brief["hq_airbase"]["name"] == "Sochi-Adler" and brief["staging_area"]["name"] == "Gali"
    assert 75 < brief["staging_area"]["distance_km"] < 85
    types = {c["type"] for c in brief["menu"]}
    assert {"T-72B", "SAU Msta", "Su-25T", "MiG-29S"} <= types and "SA-11 SAM Battery" not in types


def test_campaign_runs_convoys_cas_reviews_and_referee(scenario, catalog, olympus, tmp_path):
    fake, client = olympus
    reports = []

    def replanner(report):
        reports.append(report)
        return CampaignPlan.from_json({
            "summary": "Armour is on the objective; send the reserve to reinforce it and buy a second wave.",
            "orders": [{"group": "Reserve", "action": "move", "route": [{"bearing_deg": 90, "distance_km": 1}], "reason": "Reinforce"}],
            "convoys": [{"name": "Wave-2", "elements": [{"type": "T-90", "count": 2}], "depart_min": 5,
                         "route": [{"bearing_deg": 0, "distance_km": 0}], "reason": "Second wave"}],
            "artillery": [], "air": [],
        }, "Sochi-Adler")

    plan = CampaignPlan.from_json(opening(), "Sochi-Adler")
    cmd = CampaignCommander(client, scenario, catalog, BASES, run_id="T", event_log=tmp_path / "e.jsonl",
                            replanner=replanner, review_async=False)
    cmd.execute(plan)
    assert cmd.points_left == 800 - plan.cost(catalog)
    roles = {g.label: (g.role, g.status) for g in cmd.groups.values()}
    assert roles == {"Armour-1": ("convoy", "waiting"), "Reserve": ("convoy", "waiting"), "Msta": ("artillery", "waiting")}
    staging = (42.627, 41.735)
    armour = [u for u in fake.units.values() if u.group_name == "RED-T-Armour-1"]
    assert sorted(u.name for u in armour) == ["T-72B"] * 4 + ["Tor 9A331"]
    assert all(geo.distance_m(*staging, u.lat, u.lng) < 1500 for u in armour)
    assert fake.names_sent().count("spawnAircrafts") == 0  # cas and intercept wait on alert

    # First tick: the armour and the battery set off on roads; the reserve stays.
    fake.commands.clear()
    cmd.tick()
    moved = {body["ID"] for name, body in fake.commands if name == "setPath"}
    leader = lambda label: next(u for u in fake.units.values() if u.group_name == f"RED-T-{label}" and u.is_leader)
    assert moved == {leader("Armour-1").id, leader("Msta").id}
    assert {body["ID"] for name, body in fake.commands if name == "setFollowRoads"} == moved
    assert not reports  # no review yet

    # A Blue tank near the column, seen by the Tor: Su-25s scramble against it; no fire from the moving battery.
    lat, lng = geo.project(armour[0].lat, armour[0].lng, 90, 3000)
    blue = fake.add(category="GroundUnit", coalition=2, name="M-1 Abrams", group_name="Blue-1", lat=lat, lng=lng)
    next(u for u in armour if u.name == "Tor 9A331").contacts = [blue.id]
    fake.commands.clear()
    cmd.tick()
    spawned = [body for name, body in fake.commands if name == "spawnAircrafts"]
    assert len(spawned) == 1 and spawned[0]["units"][0]["unitType"] == "Su-25T" and spawned[0]["airbaseName"] == "Sochi-Adler"
    assert ("attackUnit", blue.id) in [(n, b["targetID"]) for n, b in fake.commands if n == "attackUnit"]
    assert "fireAtArea" not in fake.names_sent()

    # Blue is destroyed; the armour reaches the target: it has arrived and the hold starts. That triggers a review.
    blue.alive = False
    for u in armour:
        u.lat, u.lng = geo.project(*KUTAISI, 0, 400)
    cmd.last_review -= 300
    fake.commands.clear()
    cmd.tick()
    assert len(reports) == 1 and {"arrived", "hold_started"} <= set(reports[0]["why_now"])
    report = reports[0]
    assert {g["name"] for g in report["red_ground_groups"]} == {"Armour-1", "Reserve", "Msta"}
    assert report["points_left"] == 800 - plan.cost(catalog)
    assert all(e["type"] != "M-1 Abrams" for e in report["detected_enemy"])  # dead units are gone from the picture
    # The review's orders: the reserve moves, a second wave spawns and its cost comes off the points.
    reserve_path = [b for n, b in fake.commands if n == "setPath" and b["ID"] == leader("Reserve").id]
    assert len(reserve_path) == 1
    assert "RED-T-Wave-2" in cmd.groups and cmd.groups["RED-T-Wave-2"].status == "waiting"
    assert cmd.points_left == 800 - plan.cost(catalog) - 2 * 15

    # Held for the full ten minutes: Red has taken the target. That ends the reviews, scheduled or not.
    cmd.hold_since -= 11 * 60
    cmd.tick()
    assert cmd.outcome == "won"
    cmd.last_review -= 3600
    cmd.tick()
    assert len(reports) == 1

    # A Blue unit drives into the zone: the hold is broken, but the win stands.
    fake.add(category="GroundUnit", coalition=2, name="M-1 Abrams", group_name="Blue-2", lat=KUTAISI[0], lng=KUTAISI[1])
    cmd.tick()
    assert cmd.hold_since is None and cmd.outcome == "won"

    events = [json.loads(l)["event"] for l in (tmp_path / "e.jsonl").read_text().splitlines()]
    for kind in ["depart", "scramble", "arrived", "hold_started", "review", "order", "target_taken", "hold_broken"]:
        assert kind in events


def test_campaign_state_survives_a_bridge_restart(scenario, catalog, olympus, tmp_path):
    fake, client = olympus
    first = CampaignCommander(client, scenario, catalog, BASES, run_id="T", state_path=tmp_path / "s.json")
    first.execute(CampaignPlan.from_json(opening(), "Sochi-Adler"))
    first.hold_since = time.time() - 60
    first.save_state()
    second = CampaignCommander(client, scenario, catalog, BASES)
    second.restore(json.loads((tmp_path / "s.json").read_text()))
    assert second.groups == first.groups and second.points_left == first.points_left
    assert second.hold_since == first.hold_since and second.spawn_slot == 3
    assert [a.role for a in second.alerts] == ["cas", "intercept"]


def test_campaign_is_lost_when_the_ground_force_is_gone_and_points_are_spent(scenario, catalog, olympus):
    fake, client = olympus
    data = opening()
    data["convoys"], data["artillery"], data["air"] = data["convoys"][:1], [], []
    cmd = CampaignCommander(client, scenario, catalog, BASES, run_id="T")
    scenario.budget_points = 4 * 15 + catalog["Tor 9A331"].price  # nothing left after the opening
    cmd.execute(CampaignPlan.from_json(data, "Sochi-Adler"))
    for u in fake.units.values():
        u.alive = False
    cmd.tick()
    assert cmd.outcome == "lost" and not cmd.groups


def test_reviews_reuse_the_cached_brief_and_count_tokens(scenario, catalog):
    from types import SimpleNamespace
    from bridge.planner import Usage
    review = {"summary": "Hold.", "convoys": [], "artillery": [], "air": [], "orders": []}
    fake = FakeMessages([review, review])
    usage_reply = SimpleNamespace(input_tokens=1000, output_tokens=2000, cache_read_input_tokens=20000,
                                  cache_creation_input_tokens=0, cache_creation=None)
    create = fake.create
    fake.create = lambda **kw: SimpleNamespace(**vars(create(**kw)), usage=usage_reply, model="claude-opus-5-5")
    client = SimpleNamespace(beta=SimpleNamespace(messages=fake), messages=fake)
    usage = Usage()
    planner = CampaignPlanner(PlannerConfig(), client=client, usage=usage)
    report = lambda n: {"why_now": ["scheduled"], "points_left": 100, "red_ground_groups": [{"name": "Armour-1"}], "elapsed_min": n}
    planner.review(scenario, catalog, BASES, (42.627, 41.735), report(10))
    planner.review(scenario, catalog, BASES, (42.627, 41.735), report(20))
    first, second = fake.calls
    # Everything up to the cache breakpoint is identical, so the second review reads it from the cache.
    assert first["system"] == second["system"] and first["output_config"] == second["output_config"]
    assert first["messages"][0]["content"][0] == second["messages"][0]["content"][0]
    assert first["messages"][0]["content"][1] != second["messages"][0]["content"][1]
    # 1000 x $4 + 20000 x $0.20 + 2000 x $20 per million = $0.048 a call.
    assert usage.calls == 2 and usage.cache_read_tokens == 40000
    assert abs(usage.cost_usd - 0.096) < 1e-9


def test_a_lost_red_group_is_reported_as_ours(scenario, catalog, olympus, tmp_path):
    fake, client = olympus
    cmd = CampaignCommander(client, scenario, catalog, BASES, run_id="T", event_log=tmp_path / "e.jsonl")
    cmd.execute(CampaignPlan.from_json(opening(), "Sochi-Adler"))
    for u in fake.units.values():
        if u.group_name == "RED-T-Reserve":
            u.alive = False
    cmd.tick()
    lost = [e for e in cmd.recent_events if e["event"] == "group_lost"]
    assert lost == [{"event": "group_lost", "group": "RED-T-Reserve", "name": "Reserve", "side": "red (yours)",
                     "role": "convoy", "type": lost[0]["type"]}]


def test_stray_json_is_cut_from_reasons():
    data = {"summary": "Push on.", "orders": [], "artillery": [], "air": [], "convoys": [{
        "name": "AD-Escort", "elements": [{"type": "Tor 9A331", "count": 1}], "depart_min": 0, "route": [],
        "reason": 'Tor and Tunguska escort the armour into the zone."}],"artillery":[],"air":[],"orders":[],"reason":""}]'}]}
    plan = CampaignPlan.from_json(data, "Sochi-Adler")
    assert plan.convoys[0].reason == "Tor and Tunguska escort the armour into the zone."
    assert CampaignPlan.from_json({**data, "summary": 'He said "hold" and we did.'}, "x").summary == 'He said "hold" and we did.'
