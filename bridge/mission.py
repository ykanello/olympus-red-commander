"""Reads a DCS mission file (.miz): the airframes each airbase's warehouse holds.

A .miz is a zip. Its `warehouses` entry is a Lua table written by the mission editor:
warehouses = { ["airports"] = { [26] = { ["coalition"] = "RED", ["aircrafts"] = { ["planes"] = {
    ["MiG-29S"] = { ["initialAmount"] = 4, ... }, ... }, ["helicopters"] = {...} }, ... }, ... }, ["warehouses"] = {...} }
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

UNLIMITED = 999  # airframes counted for a warehouse set to unlimited aircraft

_TOKEN = re.compile(r"""
    \s+ | --[^\n]* |                                  # whitespace, comments ("-- end of [...]")
    (?P<str>"(?:\\.|[^"\\])*") |
    (?P<num>-?(?:\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+)) |
    (?P<name>[A-Za-z_]\w*) |
    (?P<sym>[{}\[\]=,;])
""", re.VERBOSE)


def _tokens(text: str):
    pos = 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise ValueError(f"Cannot read the mission's Lua at character {pos}: {text[pos:pos + 30]!r}")
        pos = m.end()
        if m.group("str") is not None:
            yield "str", bytes(m.group("str")[1:-1], "utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8")
        elif m.group("num") is not None:
            n = m.group("num")
            yield "val", float(n) if any(c in n for c in ".eE") else int(n)
        elif m.group("name") is not None:
            word = m.group("name")
            yield ("val", {"true": True, "false": False, "nil": None}[word]) if word in ("true", "false", "nil") else ("name", word)
        elif m.group("sym") is not None:
            yield "sym", m.group("sym")


def parse_lua_assignment(text: str) -> tuple[str, object]:
    """Parse `name = <table>` as written by DCS. Tables come back as dicts; keys keep their Lua type."""
    tokens = list(_tokens(text))
    pos = 0

    def take(kind=None, value=None):
        nonlocal pos
        k, v = tokens[pos]
        if (kind and k != kind) or (value is not None and v != value):
            raise ValueError(f"Unexpected {v!r} in the mission's Lua")
        pos += 1
        return v

    def value():
        k, v = tokens[pos]
        if k == "sym" and v == "{":
            return table()
        if k in ("str", "val"):
            return take()
        raise ValueError(f"Unexpected {v!r} in the mission's Lua")

    def table():
        take("sym", "{")
        result, index = {}, 1
        while tokens[pos] != ("sym", "}"):
            if tokens[pos] == ("sym", "["):
                take()
                key = value()
                take("sym", "]")
                take("sym", "=")
                result[key] = value()
            elif tokens[pos][0] == "name" and tokens[pos + 1] == ("sym", "="):
                key = take()
                take()
                result[key] = value()
            else:
                result[index] = value()
                index += 1
            if tokens[pos][0] == "sym" and tokens[pos][1] in ",;":
                take()
        take("sym", "}")
        return result

    name = take("name")
    take("sym", "=")
    return name, value()


def read_warehouses(miz: str | Path) -> dict[int, dict]:
    """Airport warehouses of a mission: {airport ID: {"coalition", "unlimited", "airframes": {type: count}}}."""
    with zipfile.ZipFile(miz) as z:
        text = z.read("warehouses").decode("utf-8")
    _, data = parse_lua_assignment(text)
    result = {}
    for airport_id, w in (data.get("airports") or {}).items():
        airframes = {}
        for kind in ("planes", "helicopters"):
            for type_name, entry in ((w.get("aircrafts") or {}).get(kind) or {}).items():
                count = entry.get("initialAmount", 0) if isinstance(entry, dict) else 0
                if count > 0:
                    airframes[type_name] = int(count)
        result[int(airport_id)] = {"coalition": str(w.get("coalition", "")).lower(),
                                   "unlimited": bool(w.get("unlimitedAircrafts")), "airframes": airframes}
    return result


def airframe_stock(miz: str | Path, airbases: dict[str, int], ignore: list[str] | None = None,
                   spawn_as: dict[str, str] | None = None) -> dict[str, dict[str, int]]:
    """Airframes per airbase, {Olympus airbase name: {type: count}}, from the warehouses of the named airport IDs.

    ignore: warehouse types to leave out. spawn_as: warehouse type -> the Olympus type that is spawned for it.
    A warehouse set to unlimited aircraft counts every type it lists as UNLIMITED.
    """
    warehouses = read_warehouses(miz)
    ignore_set, spawn_as = set(ignore or []), spawn_as or {}
    stock: dict[str, dict[str, int]] = {}
    for name, airport_id in airbases.items():
        if airport_id not in warehouses:
            raise ValueError(f"Airbase '{name}': the mission has no warehouse for airport ID {airport_id}")
        w = warehouses[airport_id]
        stock[name] = {}
        for type_name, count in w["airframes"].items():
            if type_name in ignore_set:
                continue
            spawned = spawn_as.get(type_name, type_name)
            stock[name][spawned] = stock[name].get(spawned, 0) + (UNLIMITED if w["unlimited"] else count)
    return stock
