# Olympus Red Commander bridge

Claude plans a Red defence around an objective (air defence, fighters, and optionally armour and
artillery) and places it in a live DCS mission through DCS Olympus. A deterministic watch loop then
wakes SAMs and scrambles interceptors against enemy aircraft that Red actually detects, sends armour
reserves against detected enemy ground units, and gives artillery fire missions.

Design: see the "Claude as Red Commander: static defense design" doc in the project.

## Setup (on the machine running the mission)

1. Python 3.10 or newer, then `pip install -r requirements.txt`.
2. Set an Anthropic API key in the terminal you run the bridge from. PowerShell: `$env:ANTHROPIC_API_KEY = "sk-ant-..."` (this window only), or `setx ANTHROPIC_API_KEY "sk-ant-..."` and then open a new window. Linux: `export ANTHROPIC_API_KEY=...`.
3. `copy config.example.yaml config.yaml` and set `saved_games_dcs` to your DCS Saved Games folder.
   The bridge reads `Config/olympus.json` (port and password hash) and
   `Mods/Services/Olympus` (unit databases and SAM templates) from there.
4. In `olympus.json`, set a Red Commander password in Olympus (or a Game Master one). The bridge uses
   the Red one if present.
5. Start the mission with Olympus running.

## Use

```
python -m bridge catalog scenarios/defend-kutaisi-budget.yaml   # menu and prices, no DCS needed
python -m bridge plan    scenarios/defend-kutaisi.yaml          # Claude plans; prints it; spawns nothing
python -m bridge run     scenarios/defend-kutaisi.yaml          # plan, spawn, then watch and scramble
python -m bridge run     scenarios/defend-kutaisi.yaml --plan logs/plan-....json   # rerun a saved plan
```

Each run writes `logs/plan-*.json` (the plan) and `logs/events-*.jsonl` (spawns, scrambles, SAMs going active).
The plan is also drawn as F10 map markers.

## Running it unattended

`start-red-commander.bat` runs `python -m bridge run` for the scenario named at its top. Double-click it, or
start it at logon so it is always ready:

```
schtasks /create /tn "Red Commander" /sc onlogon /tr "\"C:\Users\Hawk\olympus-red-commander\start-red-commander.bat\""
```

`run` then:

- waits until a DCS mission with Olympus is running, instead of stopping with an error;
- asks Claude for a plan once (or uses `--plan`), then spawns it;
- keeps watching if Olympus drops out for a while, and carries on when it is back;
- spawns the same plan again when the mission is restarted (`--replan` asks Claude for a new plan each time);
- if the bridge itself is restarted during the same mission, takes back control of the units it already
  spawned (from `logs/state-<scenario>.json`) instead of spawning a second set. Delete that file to force a fresh spawn.

Everything the console shows also goes to `logs/bridge-<date>.log`. For a task started at logon, set the API key with
`setx` (not `$env:`), so it is there for every new window.

## Scenario files

- `scenarios/defend-kutaisi.yaml`: an exact inventory. Claude decides only where things go and what the fighters do.
- `scenarios/defend-kutaisi-budget.yaml`: a priced menu and `budget_points`. Claude buys and places.
- `scenarios/defend-kutaisi-combined.yaml`: the same, with tanks, APCs and artillery on the menu as well.
  Claude decides how much of the budget goes to the ground threat.

Unit types use Olympus database names (`python -m bridge catalog ...` lists them). Prices come from
`DEFAULT_CLASS_PRICES` in `bridge/catalog.py`; override per class (`class_prices`) or per unit
(`price_overrides`) in the scenario. Mod units (CHAP_ ...) are left out unless `allow_mods: true`.

## What happens at run time

1. Claude gets the objective, rules, nearby Red airbases and the inventory or menu as cards
   (type, class, price, engagement and detection ranges). It never gets Blue positions.
2. Claude returns a plan as structured JSON. The bridge checks types, counts, inventory, budget,
   distances, airbases and loadouts, and sends any errors back once for repair.
3. Terrain check: DCS reports, for a ring of spots around each planned site, whether the ground is dry,
   flat and free of buildings and trees. Each site moves to the nearest good spot, at most 800 m away
   (`site_moved` in the events log). Turn this off with `check_terrain: false` in config.yaml.
   Ground groups then spawn there. SAMs are weapons free.
   Long and medium SAMs start dark (alarm state green) if `keep_sams_dark_until_km` is set. Point-defence SAMs
   (engagement range 15 km or less, such as the Tor) and EWRs radiate from the start.
4. Sweep flights spawn, climb to `sweep_altitude_ft` and fly their route. Intercept flights stay on alert.
5. Every `poll_seconds`: enemy aircraft that any Red unit detects are the only threats considered.
   A dark SAM goes active when one comes within `keep_sams_dark_until_km` of it. A threat inside
   `scramble_when_contact_within_km` of the objective gets a pair scrambled from the nearest alert base
   and ordered to attack it. Interceptors whose target is gone are retasked to the nearest unassigned threat.
6. Ground forces, in the same loop. Enemy ground units count only once a Red unit detects them; EWRs do not
   see vehicles, so forward ground positions are the eyes.
   - Tanks and APCs with role `position` hold their spot and fight what comes into range.
   - Tanks and APCs with role `reserve` wait. A detected enemy ground group within `reserve_react_within_km`
     of the objective gets the nearest free reserve, which drives to it (re-routed as it moves) and returns
     to its spot when the contact is destroyed, lost, or more than 1.5 times that distance from the objective.
     One reserve per enemy group.
   - Artillery (classes `artillery` and `mlrs`, rocket launchers reaching beyond 30 km) fires on detected enemy
     ground units within its range, at most once per `fire_mission_every_s` per battery, closest to the objective
     first, and never on a target within `no_fire_near_friendly_m` of Red ground units. `artillery_fire: false`
     turns this off.

## Tests

`python -m pytest` runs everything against a fake Olympus server that speaks the same HTTP and binary
formats. No DCS and no Anthropic API key needed.

## Verified in DCS (2026-09-30, Kutaisi scenario, one Blue flight)

- Ground groups and SAM batteries spawn where planned.
- A hot ramp start followed by `setPath`/`attackUnit` gets the MiGs airborne.
- Contact IDs in `/units` match unit IDs: the EWR's contact triggered the scramble and the SAM wake-ups.
- Some Olympus builds ignore `groupName` and call groups `Olympus-<n>`. The bridge then finds each spawn as the new Red units near the spawn point.

## Not yet verified

- Ground forces: reserves (`setPath`) and fire missions (`fireAtArea`) have only run against the fake Olympus.
  Worth checking in DCS: how far Red ground units actually detect Blue vehicles, whether a reserve sent with
  `setPath` engages on arrival, and how many rounds a battery fires per mission.

- Aircraft air-spawn altitude units (the bridge sends metres; only ramp starts were tested).
- The terrain check (Lua run through Olympus `executeFile`) has only run against a stand-in for DCS so far.
  If Olympus gives no answer, sites stay where Claude put them and the log says so.
