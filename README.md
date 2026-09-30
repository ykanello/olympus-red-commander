# Olympus Red Commander bridge

Claude plans a Red air defence around an objective and places it in a live DCS mission through
DCS Olympus. A deterministic watch loop then wakes SAMs and scrambles interceptors against
enemy aircraft that Red actually detects.

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

## Scenario files

- `scenarios/defend-kutaisi.yaml`: an exact inventory. Claude decides only where things go and what the fighters do.
- `scenarios/defend-kutaisi-budget.yaml`: a priced menu and `budget_points`. Claude buys and places.

Unit types use Olympus database names (`python -m bridge catalog ...` lists them). Prices come from
`DEFAULT_CLASS_PRICES` in `bridge/catalog.py`; override per class (`class_prices`) or per unit
(`price_overrides`) in the scenario. Mod units (CHAP_ ...) are left out unless `allow_mods: true`.

## What happens at run time

1. Claude gets the objective, rules, nearby Red airbases and the inventory or menu as cards
   (type, class, price, engagement and detection ranges). It never gets Blue positions.
2. Claude returns a plan as structured JSON. The bridge checks types, counts, inventory, budget,
   distances, airbases and loadouts, and sends any errors back once for repair.
3. Ground groups spawn at their bearing and distance from the objective. SAMs are weapons free.
   Long and medium SAMs start dark (alarm state green) if `keep_sams_dark_until_km` is set; EWRs radiate.
4. Sweep flights spawn, climb to `sweep_altitude_ft` and fly their route. Intercept flights stay on alert.
5. Every `poll_seconds`: enemy aircraft that any Red unit detects are the only threats considered.
   A dark SAM goes active when one comes within `keep_sams_dark_until_km` of it. A threat inside
   `scramble_when_contact_within_km` of the objective gets a pair scrambled from the nearest alert base
   and ordered to attack it. Interceptors whose target is gone are retasked to the nearest unassigned threat.

## Tests

`python -m pytest` runs everything against a fake Olympus server that speaks the same HTTP and binary
formats. No DCS and no Anthropic API key needed.

## Not yet verified in DCS

These come from reading the Olympus v2.0.6 source, not from a live run:

- A hot ramp start followed at once by `attackUnit`/`setPath`: whether the AI taxis and takes off.
  If not, set `fighter_spawn: air` in the scenario.
- That contact IDs in `/units` match unit IDs (the watch loop assumes so).
- Some Olympus builds ignore `groupName` and call groups `Olympus-<n>`. The bridge then finds each spawn as the new Red units near the spawn point.
- Aircraft spawn altitude units (the bridge sends metres).
- Placement can put a site on water or in a valley; there is no terrain check yet.
