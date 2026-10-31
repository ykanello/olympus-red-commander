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

## The window

`start-red-commander-gui.bat` (or `python -m bridge.gui`) opens a small window:

- **Status lights**, checked every 5 seconds: DCS running on this machine, the Olympus backend answering (it only does
  while a mission runs), the Olympus web interface, the Anthropic API key, and the commander itself.
- **Scenario**: pick a file from `scenarios/` and edit its values: objective, budget, campaign settings and rules.
  Rules the file leaves at their default are shown in grey; they are only written if you change them. The inventory
  list is edited in the file (Open in editor). Save checks the scenario the same way the bridge does.
- **Settings**: the `config.yaml` values (Saved Games folder, Olympus address, model, effort...). You can also paste
  an API key there for this window only; it is never saved.
- **Start** runs `python -m bridge run` for the chosen scenario, **Plan only** runs `plan`, **Stop** stops it the way
  Ctrl+C does (units stay in the mission). The log shows underneath, with the Claude cost so far this mission.

Saving keeps the comments in your YAML files when `ruamel.yaml` is installed (it is in `requirements.txt`).

## Claude cost

Each Claude call is logged with its tokens and an estimated cost, plus the running total for the mission:

```
Claude review: 1840 input tokens (+21310 read from cache, 0 written to cache), 2210 output tokens, about $0.055. This mission: 4 calls, $0.31
```

In a campaign, the part of the brief that does not change during the mission (rules, map, menu) is cached for an hour,
so reviews pay a fraction of its price after the first one. Reviews stop once the target is taken or the campaign is lost.

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

## Offensive campaign

`scenarios/take-kutaisi.yaml` turns things around: Red runs the war from its HQ airbase (Sochi-Adler) and must take
the target zone (Kutaisi) with ground forces, while you fly and drive Blue.

```
python -m bridge plan scenarios/take-kutaisi.yaml   # the opening plan, printed; spawns nothing
python -m bridge run  scenarios/take-kutaisi.yaml   # spawn it, fight it, and let Claude review as it goes
python -m bridge run  scenarios/take-kutaisi.yaml --no-reviews   # opening plan only, no Claude calls after the start
```

- **Where things start.** `campaign.hq` is the Red airbase all Red aircraft fly from. `campaign.staging` is where ground
  forces spawn and form up; leave it out to use the HQ. Sochi to Kutaisi is a long drive for a DCS convoy, so the
  example stages at Gali.
- **The opening plan.** Claude buys from the menu within `budget_points` (leave `catalog.classes` empty for everything
  Red has; fixed SAM batteries are left out because they cannot move) and returns:
  - convoys: up to `max_group_units` vehicles of mixed types each, with a road route and a departure time. A convoy
    with no route waits at the staging area as a reserve;
  - artillery batteries, each driving to a firing position and then firing on detected Blue ground units in range;
  - aircraft from the HQ: `sweep` (flies its route at the start), `intercept` (alert pairs against detected aircraft) and
    `cas` (alert pairs of ground-attack aircraft, such as the Su-25, against detected ground units). Alert pairs launch
    against threats within `protect_radius_km` of Red ground forces, or within `scramble_when_contact_within_km` of the target.
- **Reviews.** Every `replan_minutes`, and after big events (a group lost, a group arriving, the hold starting or
  breaking), Claude gets a situation report: its own groups, the Blue units Red detects, recent events, points left.
  It answers with orders for existing ground groups (`move` with a new route, `hold`, `return` to staging) and any
  reinforcements bought with the points left. The review runs in the background, so the fight carries on meanwhile.
  Reviews are never closer together than `min_replan_gap_minutes`.
- **Winning.** Red takes the target when its ground units have held the zone (`objective.radius_m`) for `hold_minutes`
  with no Blue ground units inside. Red loses when its ground force is gone and the points left cannot buy a vehicle.
  The referee that decides this sees everything; Red's own decisions only ever use what Red detects.

Every step goes to the events log: `spawned`, `depart`, `arrived`, `scramble` (with `role: cas` for ground attack),
`fire_mission`, `review` (Claude's assessment), `order`, `hold_started`, `hold_broken`, `target_taken`, `campaign_lost`,
`aircraft_lost` (one Red aircraft of a pair, with how many still fly), `enemy_aircraft_down` (a detected Blue aircraft
shot down; DCS does not say by whom), `break_off`, `cas_held`.

## Commanding the mission's own forces

`scenarios/sinai-st-catherine.yaml` runs Red in a mission built in the editor (the Sinai Base Mission): Red spawns no ground
units, commands ground groups already in the mission, and flies only the aircraft its airbases' warehouses hold.

```
python -m bridge plan scenarios/sinai-st-catherine.yaml   # the opening orders, printed; changes nothing in the mission
python -m bridge run  scenarios/sinai-st-catherine.yaml
```

Before the first run, set `warehouses.mission_file` to the .miz you run (on the DCS machine).

- **`campaign.spawn_ground: false`** turns off buying and spawning ground units; no `budget_points` needed.
- **`campaign.command_groups`** lists the mission editor names of the Red ground groups Claude commands. Each keeps its
  mission route (or stays put) until Claude orders it to `move`, `hold` or `return` (to where it started). A group that is
  not in the mission yet, such as a late activation, is taken under command when it appears. Every other Red group (SAMs,
  radars, missile launchers, ships) is left to the mission and its scripts. Groups with artillery fire on detected Blue
  ground units within range, as in the other modes.
- **`campaign.defend`** lists zones Red must hold while it attacks. A detected Blue ground unit inside one triggers a
  review (`defence_threatened` in the events log), and alert aircraft also launch against threats within
  `scramble_when_contact_within_km` of a defend zone.
- **`warehouses`** limits aircraft to the airframes in the mission's warehouses. `airbases` maps each Red airbase (its name
  in Olympus) to its airport ID in the mission. Each air tasking names its airbase and spends airframes from it for good;
  aircraft that land are not counted back. Aircraft then cost no points. `ignore_types` leaves warehouse types out, and
  `spawn_as` spawns an Olympus type for a warehouse type Olympus does not have (the warehouse's count still applies). The
  bridge warns at start about warehouse types it cannot fly.
- **`campaign.notes`** is the commander's intelligence and intent in plain words (in the Sinai mission: how Blue has to
  win, and what Red must stop). Claude gets it in its brief and plans by it.
- **`rules.priority_air_targets`** lists enemy aircraft types that alert pairs go after before anything else (the
  C-130 in the Sinai mission), in every mode.
- **`rules.cas_waits_for_enemy_fighters`** (on by default) keeps CAS pairs on the ground while a detected enemy jet is
  in the response zone (`cas_held` in the events log); a priority target such as a transport does not count. They launch
  once the jets are gone.
- **`rules.break_off_beyond_km`** sends an alert pair home when its target goes further than this from the target and
  every defend zone, or Red loses sight of it (`break_off`). A returning pair is retasked if a new threat comes in.
  Unset, pairs chase a target Red still detects however far it goes.
- Red loses when every commanded ground group is gone. It wins by holding the target as in the offensive campaign.
- **`missiles`** hands the mission's ballistic missile groups to Claude. With `wait_for_mission_strike`, the mission fires
  first (in the Sinai mission, its triggers fire on Ovda); a group becomes Claude's once it has not launched for
  `quiet_minutes` (the bridge follows each launcher's ammunition through Olympus), or after `release_after_minutes`.
  Claude then orders `strikes` at one of the named `targets` or at a detected Blue ground unit, with a number of rounds.
  The bridge gives the group a DCS FireAtPoint task through a Lua file run by Olympus (as the terrain check does), and
  refuses a strike out of range (`range_km` per launcher type, where Olympus has none) or within
  `no_fire_near_friendly_m` of Red ground units. `intent` tells Claude what the strikes are for. Events: `missile_launch`,
  `missiles_ready`, `missile_strike`, `strike_refused`. Not yet verified in DCS: that Olympus reports the launchers'
  missiles, and how DCS counts `expendQty` for a battery of several launchers.

## Scenario files

- `scenarios/defend-kutaisi.yaml`: an exact inventory. Claude decides only where things go and what the fighters do.
- `scenarios/defend-kutaisi-budget.yaml`: a priced menu and `budget_points`. Claude buys and places.
- `scenarios/defend-kutaisi-combined.yaml`: the same, with tanks, APCs and artillery on the menu as well.
  Claude decides how much of the budget goes to the ground threat.
- `scenarios/take-kutaisi.yaml`: the offensive campaign described above.
- `scenarios/sinai-st-catherine.yaml`: the Sinai Base Mission, commanding the mission's own forces (above).

Without an `inventory`, the menu is built from the Olympus unit databases on your server: every enabled Red unit of the
listed `era`, narrowed by `catalog.classes` if you give them.

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

- The campaign has only run against the fake Olympus. Worth checking in DCS: that convoys sent with `setPath` and
  `setFollowRoads` find the roads from the staging area, how long the drive takes, and that the `cas` pairs
  (ramp start, then `attackUnit`) find their ground targets.

- Ground forces: reserves (`setPath`) and fire missions (`fireAtArea`) have only run against the fake Olympus.
  Worth checking in DCS: how far Red ground units actually detect Blue vehicles, whether a reserve sent with
  `setPath` engages on arrival, and how many rounds a battery fires per mission.

- Aircraft air-spawn altitude units (the bridge sends metres; only ramp starts were tested).
- The terrain check (Lua run through Olympus `executeFile`) has only run against a stand-in for DCS so far.
  If Olympus gives no answer, sites stay where Claude put them and the log says so.
