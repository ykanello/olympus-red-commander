"""Asks Claude for a defence plan and repairs it until it passes validation."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable

import anthropic

from . import geo
from .catalog import Catalog
from .plan import Plan, json_schema, validate
from .scenario import Scenario

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the Red commander defending an objective in a DCS World mission. You plan; the DCS AI fights.

You receive an objective to protect, the Red airbases nearby, the rules, and either an exact inventory to place or a priced menu and a points budget. Return one defence plan.

How your plan is used:
- Each ground group spawns at a bearing and distance from the objective centre. You cannot see terrain, so prefer positions a few km from the objective rather than exact spots, and never rely on a single site.
- Ranges on the cards are the Olympus database values; use them to overlap engagement rings over the objective and toward the threat axis.
- Surface-to-air sites are set weapons free. If the rules keep SAMs dark, they stay radar-silent until a detected enemy aircraft is inside that distance. Early-warning radars always radiate, so they are what lets dark SAMs and interceptors react.
- Fighters with role "intercept" do not launch at start. They wait at their airbase and are scrambled in pairs against enemy aircraft that Red sensors detect inside the scramble distance.
- Fighters with role "sweep" launch at start, fly the sweep route with weapons free, then orbit at the last waypoint.
- Red only knows what its own sensors detect. Do not assume enemy positions beyond the threat axis you are given.

Ground forces, when the menu or inventory has them:
- Tanks and APCs with role "position" hold their spot and fight whatever comes into range. Use them to block the approaches along the threat axis and to keep enemy ground forces off SAM sites and the objective.
- Tanks and APCs with role "reserve" wait at their spot. When a Red unit detects an enemy ground group within reserve_react_within_km of the objective, the nearest free reserve drives to it and engages, then returns when the contact is gone. Place reserves central, behind the blocking positions, so they reach any approach.
- Artillery (classes artillery and mlrs) fires on detected enemy ground units within its engagement range, but never on a target close to Red ground units. Place it behind the front so its range covers the approaches.
- Early-warning radars do not see vehicles. Enemy ground units are only detected by Red ground units nearby, so reserves and artillery depend on forward positions along the threat axis to act as their eyes.

Choosing units: when two types do the same job, take the cheaper one unless the threat justifies more, and say why in that entry's reason. In a budget scenario you do not have to spend everything; unspent points are fine. Layer the defence: long or medium range coverage, short-range systems protecting those sites and the objective, early warning, then fighters. If ground forces are offered, decide from the threat axis and the objective how much of the budget the ground threat deserves.

Keep every reason to one line. Pick loadouts only from the air_to_air_loadouts listed for that type."""


# USD per million tokens: (input, output, cache read). Cache writes cost 1.25x input (5-minute) or 2x (1-hour).
# Output includes Claude's thinking. Unknown models are priced as Opus 5.5, so the totals are an estimate.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}


@dataclass
class Usage:
    """Tokens and estimated cost of the Claude calls in one mission."""
    calls: int = 0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, response, model: str, what: str) -> None:
        u = getattr(response, "usage", None)
        if u is None:
            return
        count = lambda name: getattr(u, name, None) or 0
        fresh, read, write, out = (count("input_tokens"), count("cache_read_input_tokens"),
                                   count("cache_creation_input_tokens"), count("output_tokens"))
        write_1h = getattr(getattr(u, "cache_creation", None), "ephemeral_1h_input_tokens", None) or 0
        model = getattr(response, "model", None) or model
        price_in, price_out, price_read = next((p for name, p in PRICES.items() if model.startswith(name)), PRICES["claude-opus-5-5"])
        cost = (fresh * price_in + read * price_read + write_1h * 2 * price_in + (write - write_1h) * 1.25 * price_in
                + out * price_out) / 1e6
        self.calls += 1
        self.input_tokens += fresh
        self.cache_read_tokens += read
        self.cache_write_tokens += write
        self.output_tokens += out
        self.cost_usd += cost
        log.info("Claude %s: %d input tokens (+%d read from cache, %d written to cache), %d output tokens, about $%.3f. "
                 "This mission: %d calls, $%.2f", what, fresh, read, write, out, cost, self.calls, self.cost_usd)


@dataclass
class PlannerConfig:
    model: str = "claude-opus-5-5"
    effort: str = "high"
    max_tokens: int = 16000
    refusal_fallback: bool = True
    repair_rounds: int = 1


def build_brief(scenario: Scenario, catalog: Catalog, red_airbases: dict[str, tuple[float, float]]) -> dict:
    o = scenario.objective
    bases = []
    for name, (lat, lng) in red_airbases.items():
        d = geo.distance_m(o.lat, o.lng, lat, lng) / 1000
        if d <= 250:
            bases.append({"name": name, "bearing_deg": round(geo.bearing_deg(o.lat, o.lng, lat, lng)), "distance_km": round(d, 1)})
    bases.sort(key=lambda b: b["distance_km"])

    brief = {
        "objective": {
            "name": o.name,
            "radius_m": o.radius_m,
            "threat_axis_deg": o.threat_axis_deg,
            "note": "All positions are bearing/distance from this objective's centre.",
        },
        "rules": {
            "max_distance_from_objective_km": scenario.rules.max_distance_from_objective_km,
            "keep_sams_dark_until_km": scenario.rules.keep_sams_dark_until_km,
            "scramble_when_contact_within_km": scenario.rules.scramble_when_contact_within_km,
            "sweep_altitude_ft": scenario.rules.sweep_altitude_ft,
            "reserve_react_within_km": scenario.rules.reserve_react_within_km,
            "artillery_fires_on_detected_ground_units": scenario.rules.artillery_fire,
        },
        "red_airbases_within_250km": bases,
        "mode": scenario.mode,
    }
    if scenario.mode == "inventory":
        brief["inventory"] = [
            {"count": e.count, "base": e.base, "allowed_roles": e.roles if catalog[e.type].category == "aircraft" else None, **catalog[e.type].card()}
            for e in scenario.inventory
        ]
        brief["instruction"] = "Place every item of the inventory. Fighters listed with a base must use that base."
    else:
        brief["budget_points"] = scenario.budget_points
        brief["menu"] = [c for c in catalog.cards() if c["class"] != "attack"]
        brief["instruction"] = "Buy from the menu within budget_points (price is per unit or per airframe) and place what you buy."
    return brief


class Planner:
    def __init__(self, config: PlannerConfig | None = None, client: anthropic.Anthropic | None = None, usage: Usage | None = None):
        self.config = config or PlannerConfig()
        self.client = client or anthropic.Anthropic()
        self.usage = usage or Usage()

    def _ask(self, messages: list[dict], schema: dict, system: str = SYSTEM_PROMPT, what: str = "plan"):
        kwargs = dict(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            system=system,
            messages=messages,
            output_config={"effort": self.config.effort, "format": {"type": "json_schema", "schema": schema}},
        )
        if self.config.refusal_fallback:
            response = self.client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
            )
        else:
            response = self.client.messages.create(**kwargs)
        self.usage.add(response, self.config.model, what)

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise RuntimeError(f"Claude declined to plan ({getattr(details, 'category', None)})")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("Plan was cut off at max_tokens; raise planner.max_tokens")
        text = next(b.text for b in response.content if b.type == "text")
        return response, json.loads(text)

    def plan(self, scenario: Scenario, catalog: Catalog, red_airbases: dict[str, tuple[float, float]]) -> tuple[Plan, list[str]]:
        schema = json_schema(scenario, catalog, sorted(red_airbases))
        brief = build_brief(scenario, catalog, red_airbases)
        return self.solve(brief, schema, lambda data: self._check(Plan.from_json(data), scenario, catalog, red_airbases))

    @staticmethod
    def _check(plan: Plan, scenario: Scenario, catalog: Catalog, red_airbases) -> tuple[Plan, list[str], list[str]]:
        errors, warnings = validate(plan, scenario, catalog, red_airbases)
        return plan, errors, warnings

    def solve(self, brief: dict, schema: dict, check: Callable[[dict], tuple], system: str = SYSTEM_PROMPT,
              extra: dict | None = None, what: str = "plan"):
        """Ask, then send validation errors back for repair. check(data) returns (result, errors, warnings).

        With extra, brief is the part that stays the same all mission and is cached (1-hour TTL, since reviews can
        be 10 minutes or more apart); extra is the part that changes with every call.
        """
        if extra is None:
            content = json.dumps(brief, indent=1)
        else:
            content = [
                {"type": "text", "text": json.dumps(brief, indent=1), "cache_control": {"type": "ephemeral", "ttl": "1h"}},
                {"type": "text", "text": json.dumps(extra, indent=1)},
            ]
        messages: list[dict] = [{"role": "user", "content": content}]
        for attempt in range(self.config.repair_rounds + 1):
            response, data = self._ask(messages, schema, system, what if attempt == 0 else f"{what} repair")
            result, errors, warnings = check(data)
            if not errors:
                return result, warnings
            log.warning("Plan attempt %d failed validation: %s", attempt + 1, errors)
            messages.append({"role": "assistant", "content": response.content})
            messages.append({
                "role": "user",
                "content": "The plan failed these checks. Return the full corrected plan.\n- " + "\n- ".join(errors),
            })
        raise RuntimeError("Plan still invalid after repair: " + "; ".join(errors))


class StaticPlanner:
    """Stands in for Claude in tests or offline runs: returns a fixed plan dict."""

    def __init__(self, plan_json: dict | Callable[..., dict]):
        self.plan_json = plan_json

    def plan(self, scenario: Scenario, catalog: Catalog, red_airbases: dict[str, tuple[float, float]]) -> tuple[Plan, list[str]]:
        data = self.plan_json(scenario, catalog, red_airbases) if callable(self.plan_json) else self.plan_json
        plan = Plan.from_json(json.loads(json.dumps(data)))
        errors, warnings = validate(plan, scenario, catalog, red_airbases)
        if errors:
            raise RuntimeError("Static plan invalid: " + "; ".join(errors))
        return plan, warnings
