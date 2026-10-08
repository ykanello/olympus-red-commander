"""Command line entry point.

  python -m bridge catalog scenarios/defend-kutaisi.yaml   # show what Claude may buy, with prices
  python -m bridge plan    scenarios/defend-kutaisi.yaml   # ask Claude for a plan, print it, spawn nothing
  python -m bridge run     scenarios/defend-kutaisi.yaml   # plan, spawn, then watch and scramble
  python -m bridge run     scenarios/defend-kutaisi.yaml --plan logs/plan-....json   # reuse a saved plan
  python -m bridge run     scenarios/take-kutaisi.yaml     # offensive campaign: Claude plans, then reviews as it goes

run waits for the mission, keeps going when Olympus drops out, takes back its units if restarted in the same
mission, and spawns the plan again when the mission restarts.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import json
import logging
import os
import sys
import time
from pathlib import Path

import requests
import yaml

from .campaign import CampaignCommander, CampaignPlan, CampaignPlanner, validate_campaign
from .catalog import Catalog
from .commander import Commander, MissionRestarted
from .olympus import OlympusClient
from .plan import Plan, validate
from .planner import Planner, PlannerConfig
from .scenario import Scenario, load_scenario


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}  # all defaults: auto-detect Olympus, default planner settings
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def olympus_paths(cfg: dict) -> tuple[Path, Path]:
    """Returns (olympus.json, Olympus install dir).

    Olympus lives in DCS's Saved Games folder, not the game install folder. If saved_games_dcs is not
    set, or has no Config/olympus.json, the usual Saved Games locations are tried.
    """
    o = cfg.get("olympus", {})
    if o.get("olympus_json") and o.get("olympus_dir"):
        return Path(o["olympus_json"]), Path(o["olympus_dir"])

    candidates = [Path(o["saved_games_dcs"])] if o.get("saved_games_dcs") else []
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    candidates += [home / "Saved Games" / name for name in ("DCS", "DCS.openbeta", "DCS.release_server")]
    for saved in candidates:
        if (saved / "Config/olympus.json").exists():
            return (Path(o.get("olympus_json") or saved / "Config/olympus.json"),
                    Path(o.get("olympus_dir") or saved / "Mods/Services/Olympus"))
    tried = "\n  ".join(str(c / "Config/olympus.json") for c in candidates)
    sys.exit(f"Could not find olympus.json. Tried:\n  {tried}\n"
             "Set olympus.saved_games_dcs in config.yaml to your DCS Saved Games folder.")


def build_catalog(scenario: Scenario, olympus_dir: Path) -> Catalog:
    if scenario.mode == "inventory":
        wanted = [e.type for e in scenario.inventory]
        catalog = Catalog.load(olympus_dir, scenario.coalition, include=wanted)
        missing = [t for t in wanted if t not in catalog]
        if missing:
            sys.exit(f"Inventory types not found in the Olympus database (or not spawnable): {missing}")
        catalog.items = {n: i for n, i in catalog.items.items() if n in wanted}
        return catalog
    c = scenario.catalog
    return Catalog.load(olympus_dir, scenario.coalition, eras=scenario.eras, classes=c.classes, include=c.include,
                        exclude=c.exclude, price_overrides=c.price_overrides, class_prices=c.class_prices, allow_mods=c.allow_mods)


def red_airbases(client: OlympusClient, coalition: str) -> dict[str, tuple[float, float]]:
    return {a["callsign"]: (a["latitude"], a["longitude"]) for a in client.get_airbases() if a.get("coalition") == coalition}


def print_plan(plan: Plan, catalog: Catalog, warnings: list[str]) -> None:
    print(f"\n{plan.summary}\n")
    for g in plan.ground_groups:
        tag = " [reserve]" if g.role == "reserve" else ""
        print(f"  {g.name:<18} {g.count}x {g.type:<22} {g.bearing_deg:>5.0f}° {g.distance_km:>5.1f} km  {g.reason}{tag}")
    for f in plan.fighters:
        print(f"  {f.role:<18} {f.count}x {f.type:<22} at {f.airbase}  {f.reason}")
    print(f"\n  Cost: {plan.cost(catalog)} points")
    for w in warnings:
        print(f"  Warning: {w}")


def print_campaign_plan(plan: CampaignPlan, catalog: Catalog, warnings: list[str]) -> None:
    print(f"\n{plan.summary}\n")
    for c in plan.convoys:
        load = ", ".join(f"{e.count}x {e.type}" for e in c.elements)
        when = f"sets off at +{c.depart_min:g} min, {len(c.route)} waypoints" if c.route else "waits at staging"
        print(f"  {c.name:<18} {load}  ({when})  {c.reason}")
    for b in plan.artillery:
        p = b.firing_position
        print(f"  {b.name:<18} {b.count}x {b.type}  fires from {p['bearing_deg']:.0f}° {p['distance_km']:.1f} km  {b.reason}")
    for a in plan.air:
        print(f"  {a.role:<18} {a.count}x {a.type} from {a.airbase}  {a.reason}")
    for o in plan.orders:
        print(f"  order: {o.group} {o.action}  {o.reason}")
    print(f"\n  Cost: {plan.cost(catalog)} points")
    for w in warnings:
        print(f"  Warning: {w}")


def staging_point(scenario: Scenario, bases: dict) -> tuple[float, float]:
    camp = scenario.campaign
    if camp.hq not in bases:
        sys.exit(f"campaign.hq '{camp.hq}' is not a {scenario.coalition} airbase in this mission. "
                 f"{scenario.coalition.capitalize()} airbases: {sorted(bases) or 'none'}")
    return (camp.staging_lat, camp.staging_lng) if camp.staging_lat is not None else bases[camp.hq]


def make_campaign_plan(args, cfg: dict, scenario: Scenario, catalog: Catalog, bases: dict, log_dir: Path) -> tuple[CampaignPlan, list[str]]:
    staging = staging_point(scenario, bases)
    if args.plan:
        plan = CampaignPlan.from_json(json.loads(args.plan.read_text(encoding="utf-8")), scenario.campaign.hq)
        errors, warnings = validate_campaign(plan, scenario, catalog, scenario.budget_points)
        if errors:
            sys.exit("Saved plan is invalid: " + "; ".join(errors))
        return plan, warnings
    require_api_key()
    plan, warnings = campaign_planner(cfg).plan_campaign(scenario, catalog, bases, staging)
    save_plan(plan, scenario, log_dir)
    return plan, warnings


def campaign_planner(cfg: dict) -> CampaignPlanner:
    return CampaignPlanner(PlannerConfig(**cfg.get("planner", {})))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="bridge", description="Claude as Red commander through DCS Olympus")
    parser.add_argument("command", choices=["catalog", "plan", "run"])
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--plan", type=Path, help="Use a saved plan JSON instead of asking Claude")
    parser.add_argument("--replan", action="store_true", help="Ask Claude for a fresh plan each time the mission restarts")
    parser.add_argument("--no-reviews", action="store_true", help="Campaign: run the opening plan without Claude reviewing it as it goes")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    scenario = load_scenario(args.scenario)
    olympus_json, olympus_dir = olympus_paths(cfg)
    catalog = build_catalog(scenario, olympus_dir)

    if args.command == "catalog":
        print(json.dumps(catalog.cards(), indent=1))
        return

    client = OlympusClient.from_olympus_json(olympus_json)
    log_dir = Path(cfg.get("log_dir", "logs"))
    log_dir.mkdir(exist_ok=True)
    add_file_log(log_dir)

    if args.command == "plan":
        bases = connect(client, scenario, wait=False)
        if scenario.campaign:
            plan, warnings = make_campaign_plan(args, cfg, scenario, catalog, bases, log_dir)
            print_campaign_plan(plan, catalog, warnings)
        else:
            plan, warnings = make_plan(args, cfg, scenario, catalog, bases, log_dir)
            print_plan(plan, catalog, warnings)
        return

    # run: keep going across mission restarts until Ctrl+C.
    state_path = log_dir / f"state-{scenario.name}.json"
    plan = None
    try:
        while True:
            bases = connect(client, scenario, wait=True)
            session = client.last_session_hash
            stamp = time.strftime("%Y%m%d-%H%M%S")
            common = dict(event_log=log_dir / f"events-{scenario.name}-{stamp}.jsonl", session_hash=session, work_dir=log_dir,
                          check_terrain=cfg.get("check_terrain", True))
            if scenario.campaign:
                staging = staging_point(scenario, bases)
                reviewer = campaign_planner(cfg) if not args.no_reviews else None
                commander = CampaignCommander(
                    client, scenario, catalog, bases, **common,
                    replanner=(lambda report, b=bases, st=staging: reviewer.review(scenario, catalog, b, st, report)) if reviewer else None)
            else:
                commander = Commander(client, scenario, catalog, bases, **common)
            state = load_state(state_path)
            if state and session and state.get("session_hash") == session:
                commander.restore(state)
                commander.state_path = state_path
                logging.info("Same mission as before (%d groups): taking back control instead of spawning again.", len(commander.groups))
            else:
                if plan is None or args.replan:
                    if scenario.campaign:
                        plan, warnings = make_campaign_plan(args, cfg, scenario, catalog, bases, log_dir)
                        print_campaign_plan(plan, catalog, warnings)
                    else:
                        plan, warnings = make_plan(args, cfg, scenario, catalog, bases, log_dir)
                        print_plan(plan, catalog, warnings)
                else:
                    logging.info("New mission: spawning the same plan again.")
                commander.state_path = state_path
                commander.execute(copy.deepcopy(plan))
            logging.info("Watching every %.0fs; Ctrl+C to stop.", scenario.rules.poll_seconds)
            try:
                commander.run()
            except MissionRestarted:
                logging.info("The mission was restarted.")
    except KeyboardInterrupt:
        logging.info("Stopped. Units stay in the mission; running again in the same mission takes them back.")


def add_file_log(log_dir: Path) -> None:
    handler = logging.FileHandler(log_dir / f"bridge-{time.strftime('%Y%m%d')}.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)


def connect(client: OlympusClient, scenario: Scenario, wait: bool) -> dict[str, tuple[float, float]]:
    """Read Red airbases. With wait, keep trying until a mission with Olympus is running."""
    waiting = False
    while True:
        try:
            bases = red_airbases(client, scenario.coalition)
            break
        except requests.RequestException:
            if not wait:
                sys.exit(f"Cannot reach Olympus at {client.base_url}. Olympus only answers while a DCS mission is running "
                         "with the Olympus mod enabled: start the mission (unpaused), then run this again.")
            if not waiting:
                logging.info("Waiting for a DCS mission with Olympus at %s ...", client.base_url)
                waiting = True
            time.sleep(10)
    if not bases:
        logging.warning("Olympus reports no %s airbases in this mission; fighters cannot be used.", scenario.coalition)
    return bases


def load_state(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def make_plan(args, cfg: dict, scenario: Scenario, catalog: Catalog, bases: dict, log_dir: Path) -> tuple[Plan, list[str]]:
    if args.plan:
        plan = Plan.from_json(json.loads(args.plan.read_text(encoding="utf-8")))
        errors, warnings = validate(plan, scenario, catalog, bases)
        if errors:
            sys.exit("Saved plan is invalid: " + "; ".join(errors))
        return plan, warnings
    require_api_key()
    planner = Planner(PlannerConfig(**cfg.get("planner", {})))
    plan, warnings = planner.plan(scenario, catalog, bases)
    save_plan(plan, scenario, log_dir)
    return plan, warnings


def require_api_key() -> None:
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("ANTHROPIC_API_KEY is not set in this terminal. In PowerShell: $env:ANTHROPIC_API_KEY = \"sk-ant-...\" "
                 "(or setx it, then open a new window). Or reuse a saved plan with --plan.")


def save_plan(plan, scenario: Scenario, log_dir: Path) -> None:
    plan_path = log_dir / f"plan-{scenario.name}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    plan_path.write_text(json.dumps(dataclasses.asdict(plan), indent=1), encoding="utf-8")
    logging.info("Plan saved to %s", plan_path)


if __name__ == "__main__":
    main()
