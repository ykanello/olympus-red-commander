"""Unit catalog: the Olympus databases narrowed to what Red may buy, with a price per type.

Olympus has a per-unit `cost` field, but almost no unit sets it (everything else costs a flat
10 points), so prices come from the class table below, overridable per scenario.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# Default prices in points. Starting values to tune, not researched figures.
DEFAULT_CLASS_PRICES = {
    "sam_long": 150,
    "sam_medium": 80,
    "sam_short": 30,
    "aaa": 10,
    "ewr": 20,
    "tank_modern": 15,
    "tank_old": 8,
    "apc": 6,
    "artillery": 10,  # tube artillery and mortars
    "mlrs": 25,  # rocket artillery reaching beyond 30 km (Uragan, Smerch)
    "fighter": 40,  # per airframe
    "attack": 35,  # per airframe: ground-attack aircraft with no air-to-air loadout (Su-25)
}

AIR_DEFENCE_CLASSES = {"sam_long", "sam_medium", "sam_short", "aaa"}
MANOEUVRE_CLASSES = {"tank_modern", "tank_old", "apc"}  # can be held as a reserve that drives to a contact
ARTILLERY_CLASSES = {"artillery", "mlrs"}  # fire on detected enemy ground units within range
GROUND_CLASSES = AIR_DEFENCE_CLASSES | MANOEUVRE_CLASSES | ARTILLERY_CLASSES | {"ewr"}
MLRS_MIN_RANGE_M = 30_000
GROUND_ATTACK_ROLES = ("CAS", "Strike")

# Rough reach of air-to-air missiles in km, only to rank loadouts: longest reach first, then how many of that missile.
MISSILE_REACH_KM = {
    "AIM-120C": 90, "AIM-120B": 75, "R-77": 80, "PL-12": 80, "SD-10": 80, "R-27ER": 65, "R-27ET": 55, "AIM-54": 100,
    "R-33": 120, "R-40R": 50, "R-40RD": 50, "AIM-7": 50, "Super 530D": 45, "R-27R": 40, "R-24R": 35, "R-27T": 30, "R-40T": 30, "R-40TD": 30,
    "R-24T": 20, "AIM-9": 15, "R-73": 15, "PL-8": 15, "Magic": 12, "PL-5": 10, "R-3R": 10, "R-60": 8, "R-60M": 8, "R-3S": 7,
    "R-13M": 7, "R-13M1": 7, "R-55": 6,
}
MISSILE_RE = re.compile(r"\b(AIM-\d+[A-Z]?|R-\d+[A-Z]*\d?|PL-\d+|SD-10|Super 530D|Magic)")


def missile_reach_km(item_name: str) -> float:
    m = MISSILE_RE.search(item_name)
    if not m:
        return 0.0
    name = m.group(1)
    return MISSILE_REACH_KM.get(name) or MISSILE_REACH_KM.get(re.sub(r"[A-Z]+$", "", name), 0.0)


def rack_size(item_name: str) -> int:
    """Missiles per pylon: "SD-10A AAM x 2" and "APU-60-2M with 2 x R-60" carry two."""
    m = re.search(r"\bx\s*(\d+)\b|\b(\d+)\s*x\s", item_name)
    return int(m.group(1) or m.group(2)) if m else 1


def a2a_rank(loadout: dict) -> tuple:
    """Sort key for an air-to-air loadout: the longest-reaching missile, how many of it, then everything it carries."""
    missiles = [(missile_reach_km(i["name"]), i.get("quantity", 1) * rack_size(i["name"])) for i in loadout.get("items", [])]
    best = max((r for r, _ in missiles), default=0.0)
    return (best, sum(q for r, q in missiles if r == best), sum(r * q for r, q in missiles))


@dataclass
class CatalogItem:
    name: str  # exact DCS / Olympus type name used to spawn
    label: str
    cls: str
    category: str  # "groundunit" or "aircraft"
    era: str
    price: int
    engagement_range_m: float = 0.0
    acquisition_range_m: float = 0.0
    is_template: bool = False
    a2a_loadouts: list[str] = field(default_factory=list)
    ground_attack_loadouts: list[str] = field(default_factory=list)
    description: str = ""

    def card(self) -> dict:
        """What Claude sees for this item."""
        card = {
            "type": self.name,
            "label": self.label,
            "class": self.cls,
            "era": self.era,
            "price": self.price,
        }
        if self.category == "groundunit":
            card["engagement_range_km"] = round(self.engagement_range_m / 1000, 1)
            card["detection_range_km"] = round(self.acquisition_range_m / 1000, 1)
            card["spawns_as_full_battery"] = self.is_template
        else:
            if self.a2a_loadouts:
                card["air_to_air_loadouts"] = self.a2a_loadouts[:4]
            if self.ground_attack_loadouts:
                card["ground_attack_loadouts"] = self.ground_attack_loadouts[:4]
        if self.description:
            card["description"] = self.description
        return card


def load_template_names(templates_lua: Path) -> set[str]:
    """Names of the battery templates Olympus can spawn (keys of the `templates` table)."""
    text = templates_lua.read_text(encoding="utf-8", errors="ignore")
    return set(re.findall(r'^\s{4}\["([^"]+)"\]\s*=\s*$', text, flags=re.MULTILINE))


def _ground_class(entry: dict) -> str | None:
    kind = entry.get("type")
    era = entry.get("era", "")
    if kind == "SAM Site":
        return {"Long": "sam_long", "Medium": "sam_medium", "Short": "sam_short"}.get(entry.get("range") or "", "sam_short")
    if kind == "AAA":
        return "aaa"
    if kind == "Radar (EWR)":
        return "ewr"
    if kind == "Tank":
        return "tank_modern" if era in ("Modern", "Late Cold War") else "tank_old"
    if kind == "APC":
        return "apc"
    if kind == "Artillery":
        return "mlrs" if float(entry.get("engagementRange") or 0) > MLRS_MIN_RANGE_M else "artillery"
    return None


class Catalog:
    def __init__(self, items: dict[str, CatalogItem], ground_db: dict | None = None):
        self.items = items
        self.ground_db = ground_db or {}  # the whole Olympus ground database, for units Red already has in the mission

    def describe(self, name: str) -> CatalogItem:
        """Any ground type, even one Red may not buy: class and ranges of a unit already in the mission."""
        if name in self.items:
            return self.items[name]
        entry = self.ground_db.get(name, {})
        return CatalogItem(name=name, label=entry.get("label", name), cls=_ground_class(entry) or "other", category="groundunit",
                           era=entry.get("era", ""), price=0, engagement_range_m=float(entry.get("engagementRange") or 0),
                           acquisition_range_m=float(entry.get("acquisitionRange") or 0))

    def __contains__(self, name: str) -> bool:
        return name in self.items

    def __getitem__(self, name: str) -> CatalogItem:
        return self.items[name]

    def names(self, category: str | None = None) -> list[str]:
        return sorted(n for n, i in self.items.items() if category is None or i.category == category)

    def cards(self) -> list[dict]:
        return [self.items[n].card() for n in sorted(self.items, key=lambda n: (self.items[n].cls, self.items[n].price, n))]

    @classmethod
    def load(
        cls,
        olympus_dir: Path,
        coalition: str = "red",
        eras: list[str] | None = None,
        classes: list[str] | None = None,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        price_overrides: dict[str, int] | None = None,
        class_prices: dict[str, int] | None = None,
        allow_mods: bool = False,
    ) -> "Catalog":
        """olympus_dir is Saved Games/DCS/Mods/Services/Olympus (holds databases/ and Scripts/)."""
        olympus_dir = Path(olympus_dir)
        ground = json.loads((olympus_dir / "databases/units/groundunitdatabase.json").read_text(encoding="utf-8"))
        aircraft = json.loads((olympus_dir / "databases/units/aircraftdatabase.json").read_text(encoding="utf-8"))
        templates_path = olympus_dir / "Scripts/templates.lua"
        templates = load_template_names(templates_path) if templates_path.exists() else set()

        prices = {**DEFAULT_CLASS_PRICES, **(class_prices or {})}
        overrides = price_overrides or {}
        include_set = set(include or [])
        exclude_set = set(exclude or [])
        items: dict[str, CatalogItem] = {}

        def wanted(name: str, entry: dict, klass: str) -> bool:
            if name in exclude_set:
                return False
            if name in include_set:
                return True
            if not entry.get("enabled", True) or entry.get("coalition") != coalition:
                return False
            if eras and entry.get("era") not in eras:
                return False
            if classes and klass not in classes:
                return False
            if not allow_mods and name.startswith(("CHAP_", "SON_")):
                return False
            return True

        for name, entry in ground.items():
            klass = _ground_class(entry)
            if klass is None or not wanted(name, entry, klass):
                continue
            # "... SAM Battery" entries only spawn if Olympus has a template for them.
            if entry.get("type") == "SAM Site" and name.endswith(("Battery", "site")) and name not in templates:
                continue
            items[name] = CatalogItem(
                name=name,
                label=entry.get("label", name),
                cls=klass,
                category="groundunit",
                era=entry.get("era", ""),
                price=int(overrides.get(name, prices[klass])),
                engagement_range_m=float(entry.get("engagementRange") or 0),
                acquisition_range_m=float(entry.get("acquisitionRange") or 0),
                is_template=name in templates,
                description=entry.get("description", ""),
            )

        for name, entry in aircraft.items():
            armed = [l for l in entry.get("loadouts", []) if l.get("items")]
            # Best first: Claude sees only the first few, and alert pairs fly the first one.
            loadouts = [l["name"] for l in sorted((l for l in armed if "CAP" in l.get("roles", [])), key=a2a_rank, reverse=True)]
            attack = [l["name"] for l in armed if set(GROUND_ATTACK_ROLES) & set(l.get("roles", []))]
            klass = "fighter" if loadouts else "attack" if attack else None
            if klass is None or not wanted(name, entry, klass):
                continue
            items[name] = CatalogItem(
                name=name,
                label=entry.get("label", name),
                cls=klass,
                category="aircraft",
                era=entry.get("era", ""),
                price=int(overrides.get(name, prices[klass])),
                a2a_loadouts=loadouts,
                ground_attack_loadouts=attack,
                description=entry.get("description", ""),
            )

        return cls(items, ground)
