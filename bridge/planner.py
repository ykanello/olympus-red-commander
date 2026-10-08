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
        brief["menu"] = catalog.cards()
        brief["instruction"] = "Buy from the menu within budget_points (price is per unit or per airframe) and place what you buy."
    return brief


class Planner:
    def __init__(self, config: PlannerConfig | None = None, client: anthropic.Anthropic | None = None):
        self.config = config or PlannerConfig()
        self.client = client or anthropic.Anthropic()

    def _ask(self, messages: list[dict], schema: dict):
        kwargs = dict(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            system=SYSTEM_PROMPT,
            messages=messages,
            output_config={"effort": self.config.effort, "format": {"type": "json_schema", "schema": schema}},
        )
        if self.config.refusal_fallback:
            response = self.client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
            )
        else:
            response = self.client.messages.create(**kwargs)

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
        messages: list[dict] = [{"role": "user", "content": json.dumps(brief, indent=1)}]

        for attempt in range(self.config.repair_rounds + 1):
            response, data = self._ask(messages, schema)
            plan = Plan.from_json(data)
            errors, warnings = validate(plan, scenario, catalog, red_airbases)
            if not errors:
                return plan, warnings
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
