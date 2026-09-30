"""Command line entry point.

  python -m bridge catalog scenarios/defend-kutaisi.yaml   # show what Claude may buy, with prices
  python -m bridge plan    scenarios/defend-kutaisi.yaml   # ask Claude for a plan, print it, spawn nothing
  python -m bridge run     scenarios/defend-kutaisi.yaml   # plan, spawn, then watch and scramble
  python -m bridge run     scenarios/defend-kutaisi.yaml --plan logs/plan-....json   # reuse a saved plan
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
import time
from pathlib import Path

import requests
import yaml

from .catalog import Catalog
from .commander import Commander
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
        print(f"  {g.name:<18} {g.count}x {g.type:<22} {g.bearing_deg:>5.0f}° {g.distance_km:>5.1f} km  {g.reason}")
    for f in plan.fighters:
        print(f"  {f.role:<18} {f.count}x {f.type:<22} at {f.airbase}  {f.reason}")
    print(f"\n  Cost: {plan.cost(catalog)} points")
    for w in warnings:
        print(f"  Warning: {w}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="bridge", description="Claude as Red commander through DCS Olympus")
    parser.add_argument("command", choices=["catalog", "plan", "run"])
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--plan", type=Path, help="Use a saved plan JSON instead of asking Claude")
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
    try:
        bases = red_airbases(client, scenario.coalition)
    except requests.ConnectionError:
        sys.exit(f"Cannot reach Olympus at {client.base_url}. Olympus only answers while a DCS mission is running "
                 "with the Olympus mod enabled: start the mission (unpaused), then run this again.")
    if not bases:
        logging.warning("Olympus reports no %s airbases in this mission; fighters cannot be used.", scenario.coalition)
    log_dir = Path(cfg.get("log_dir", "logs"))
    log_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    if args.plan:
        plan = Plan.from_json(json.loads(args.plan.read_text(encoding="utf-8")))
        errors, warnings = validate(plan, scenario, catalog, bases)
        if errors:
            sys.exit("Saved plan is invalid: " + "; ".join(errors))
    else:
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            sys.exit("ANTHROPIC_API_KEY is not set in this terminal. In PowerShell: $env:ANTHROPIC_API_KEY = \"sk-ant-...\" "
                     "(or setx it, then open a new window). Or reuse a saved plan with --plan.")
        planner = Planner(PlannerConfig(**cfg.get("planner", {})))
        plan, warnings = planner.plan(scenario, catalog, bases)
        plan_path = log_dir / f"plan-{scenario.name}-{stamp}.json"
        plan_path.write_text(json.dumps(dataclasses.asdict(plan), indent=1), encoding="utf-8")
        logging.info("Plan saved to %s", plan_path)

    print_plan(plan, catalog, warnings)
    if args.command == "plan":
        return

    commander = Commander(client, scenario, catalog, bases, event_log=log_dir / f"events-{scenario.name}-{stamp}.jsonl")
    commander.execute(plan)
    logging.info("Plan executed. Watching every %.0fs; Ctrl+C to stop.", scenario.rules.poll_seconds)
    try:
        commander.run()
    except KeyboardInterrupt:
        logging.info("Stopped.")


if __name__ == "__main__":
    main()
