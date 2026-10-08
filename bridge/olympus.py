"""Thin client for the DCS Olympus backend REST API (tested against Olympus v2.0.6 source).

The API lives at http://<address>:<port>/olympus. Every order is a PUT whose JSON body is
{"<commandName>": {...}}; reads are GETs on /units, /airbases, /commands, ...
Authentication is HTTP Basic with the SHA-256 hash stored in olympus.json as the password.

/units is a binary stream. The decoder below follows the layout written by
backend/core/src/unit.cpp (Unit::getData) and read by the official Python client
(scripts/python/API/unit/unit.py). It is reimplemented here so the bridge does not need the
official client's audio/ML dependencies.
"""

from __future__ import annotations

import base64
import json
import logging
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

log = logging.getLogger(__name__)

# Olympus enums, as in scripts/python/API/data/*.py
ROES = ["", "free", "designated", "return", "hold"]
ALARM_STATES = {"auto": 0, "green": 1, "red": 2}
EMISSIONS = ["silent", "attack", "defend", "free"]
REACTIONS = ["none", "manoeuvre", "passive", "evade"]
COALITIONS = {0: "neutral", 1: "red", 2: "blue"}

DETECTION_VISUAL, DETECTION_OPTIC, DETECTION_RADAR, DETECTION_IRST = 1, 2, 4, 8


@dataclass
class Contact:
    id: int
    detection_method: int


@dataclass
class Unit:
    id: int
    category: str = ""
    alive: bool = False
    human: bool = False
    coalition: str = "neutral"
    name: str = ""  # DCS type name, e.g. "MiG-29S"
    unit_name: str = ""
    group_name: str = ""
    group_id: int = 0
    state: int = 0
    lat: float = 0.0
    lng: float = 0.0
    alt: float = 0.0
    speed: float = 0.0
    heading: float = 0.0
    airborne: bool = False
    is_leader: bool = False
    health: int = 100
    alarm_state: str = "auto"
    roe: str = ""
    contacts: list[Contact] = field(default_factory=list)


class _Reader:
    def __init__(self, buf: bytes):
        self.buf = buf
        self.pos = 0

    def done(self) -> bool:
        return self.pos >= len(self.buf)

    def _unpack(self, fmt: str):
        value = struct.unpack_from(fmt, self.buf, self.pos)
        self.pos += struct.calcsize(fmt)
        return value

    def u8(self) -> int:
        return self._unpack("<B")[0]

    def u16(self) -> int:
        return self._unpack("<H")[0]

    def u32(self) -> int:
        return self._unpack("<I")[0]

    def u64(self) -> int:
        return self._unpack("<Q")[0]

    def f64(self) -> float:
        return self._unpack("<d")[0]

    def boolean(self) -> bool:
        return self.u8() > 0

    def string(self) -> str:
        length = self.u16()
        raw = self.buf[self.pos : self.pos + length]
        self.pos += length
        return raw.split(b"\0", 1)[0].decode("utf-8", errors="ignore").strip()

    def coords(self) -> tuple[float, float, float]:
        lat, lng, alt, _threshold = self._unpack("<dddd")
        return lat, lng, alt

    def skip(self, n: int) -> None:
        self.pos += n

    def skip_vector(self, element_size: int) -> None:
        self.skip(self.u16() * element_size)


# Datum index -> how to read it. Sizes follow the packed C++ structs in datatypes.h.
def _read_datum(r: _Reader, index: int, u: Unit) -> None:
    if index == 1:
        u.category = r.string()
    elif index == 2:
        u.alive = r.boolean()
    elif index == 3:
        u.alarm_state = {0: "auto", 1: "green", 2: "red"}.get(r.u8(), "auto")
    elif index in (4, 6, 17, 24, 25, 26, 27, 30, 32, 71):
        r.u8()
    elif index == 5:
        u.human = r.boolean()
    elif index == 7:
        u.coalition = COALITIONS.get(r.u8(), "")
    elif index in (8, 48, 49, 70):
        r.u8()
    elif index == 9:
        u.name = r.string()
    elif index == 10:
        u.unit_name = r.string()
    elif index in (11, 16, 68):
        r.string()
    elif index in (12, 33, 35, 58, 69, 73):
        r.u32()
    elif index == 13:
        u.group_id = r.u32()
    elif index == 14:
        u.group_name = r.string()
    elif index == 15:
        u.state = r.u8()
    elif index == 18:
        u.lat, u.lng, u.alt = r.coords()
    elif index == 19:
        u.speed = r.f64()
    elif index == 22:
        u.heading = r.f64()
    elif index in (20, 21, 23, 29, 31, 51, 53, 54, 55, 56, 57, 59, 60, 61, 62, 63, 64, 66, 76, 77, 78):
        r.f64()
    elif index == 28:
        r.u16()
    elif index == 34:
        r.skip(24)  # offset: 3 doubles
    elif index in (36, 52, 74, 75):
        r.coords()
    elif index == 37:
        u.roe = ROES[r.u8()]
    elif index in (38, 39):
        r.u8()
    elif index == 40:
        r.skip(7)  # TACAN: bool, uchar, char, char[4]
    elif index == 41:
        r.skip(6)  # radio: uint, uchar, uchar
    elif index == 42:
        r.skip(5)  # general settings: 5 bools
    elif index == 43:
        r.skip_vector(38)  # ammo: ushort, char[33], 3 uchar
    elif index == 44:
        u.contacts = [Contact(r.u32(), r.u8()) for _ in range(r.u16())]
    elif index == 45:
        r.skip_vector(32)  # path of coords
    elif index == 46:
        u.is_leader = r.boolean()
    elif index == 47:
        r.u8()
    elif index == 50:
        u.health = r.u8()
    elif index == 65:
        u.airborne = r.boolean()
    elif index == 67:
        r.skip_vector(12)  # draw arguments: uint + double
    elif index == 72:
        r.skip_vector(4)
    else:
        raise ValueError(f"Unknown Olympus datum index {index} at byte {r.pos}")


def decode_units(payload: bytes, units: dict[int, Unit] | None = None) -> dict[int, Unit]:
    """Decode a /units response. Pass the previous dict to apply an incremental update."""
    units = {} if units is None else units
    r = _Reader(payload)
    r.u64()  # update timestamp
    while not r.done():
        unit_id = r.u32()
        u = units.setdefault(unit_id, Unit(unit_id))
        while True:
            index = r.u8()
            if index == 255:
                break
            _read_datum(r, index, u)
    return units


class OlympusError(RuntimeError):
    pass


class OlympusClient:
    def __init__(self, base_url: str, password_hash: str, username: str = "RedCommanderBridge", timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        token = base64.b64encode(f"{username}:{password_hash}".encode()).decode()
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Basic {token}"
        self.timeout = timeout
        self.last_session_hash: str | None = None

    @classmethod
    def from_olympus_json(cls, path: str | Path, role: str = "red") -> "OlympusClient":
        """Build a client from DCS's Saved Games Config/olympus.json."""
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
        backend = cfg["backend"]
        address = backend.get("address", "localhost")
        if address in ("*", "0.0.0.0"):
            address = "localhost"
        auth = cfg.get("authentication", {})
        password = auth.get("redCommanderPassword") if role == "red" else None
        password = password or auth.get("gameMasterPassword")
        if not password:
            raise OlympusError(f"No Red Commander or Game Master password set in {path}")
        return cls(f"http://{address}:{backend['port']}/olympus", password)

    # ---- reads ----
    def _get(self, endpoint: str, **params) -> requests.Response:
        resp = self.session.get(f"{self.base_url}/{endpoint}", params=params or None, timeout=self.timeout)
        if resp.status_code == 401:
            raise OlympusError("Olympus rejected the password (401). Check olympus.json.")
        resp.raise_for_status()
        return resp

    def get_units(self) -> dict[int, Unit]:
        return decode_units(self._get("units").content)

    def get_airbases(self) -> list[dict]:
        answer = self._get("airbases").json()
        self.last_session_hash = answer.get("sessionHash")
        data = answer.get("airbases", {})
        items = data.values() if isinstance(data, dict) else data
        return [a for a in items if a]

    def session_hash(self) -> str | None:
        """Random ID Olympus picks each time a mission starts: a change means the mission was restarted."""
        self.get_airbases()
        return self.last_session_hash

    def get_mission(self) -> dict:
        return self._get("mission").json().get("mission", {})

    def command_status(self, command_hash: str) -> dict:
        return self._get("commands", commandHash=command_hash).json()

    def execute_file(self, path: str | Path) -> str | None:
        """Run a Lua file inside the mission (MIST and Olympus loaded). The file must be on the DCS machine.

        A script can hand data back by setting Olympus.executionResults["<key>"] = "<string>";
        Olympus publishes that table about once a second and command_result("<key>") reads it.
        """
        return self.send("executeFile", {"filePath": str(Path(path).resolve()).replace("\\", "/")})

    def command_result(self, key: str):
        return self.command_status(key).get("commandResult")

    # ---- writes ----
    def send(self, name: str, body: dict) -> str | None:
        resp = self.session.put(self.base_url, json={name: body}, timeout=self.timeout)
        if resp.status_code == 401:
            raise OlympusError("Olympus rejected the password (401). Check olympus.json.")
        resp.raise_for_status()
        try:
            return resp.json().get("commandHash")
        except ValueError:
            return None

    def wait_for(self, command_hash: str | None, timeout: float = 30.0, want_result: bool = True):
        """Poll /commands until DCS has executed the command. Returns the command result (a group ID for spawns)."""
        if not command_hash:
            return None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.command_status(command_hash)
            if status.get("commandExecuted") and (not want_result or status.get("commandResult") is not None):
                return status.get("commandResult")
            time.sleep(0.5)
        raise OlympusError(f"Command {command_hash} was not executed within {timeout:.0f}s")

    # Spawning. Olympus expands battery templates (e.g. "SA-11 SAM Battery") on the Lua side.
    def spawn_ground(self, group_name: str, units: list[dict], coalition: str = "red", country: str = "", spawn_points: int = 0) -> str | None:
        return self.send("spawnGroundUnits", {
            "units": units, "coalition": coalition, "country": country,
            "immediate": True, "spawnPoints": spawn_points, "groupName": group_name,
        })

    def spawn_aircraft(self, group_name: str, units: list[dict], airbase: str = "", coalition: str = "red", country: str = "", spawn_points: int = 0) -> str | None:
        """airbase set: hot start on the ramp. airbase empty: air spawn at each unit's altitude."""
        return self.send("spawnAircrafts", {
            "units": units, "coalition": coalition, "airbaseName": airbase, "country": country,
            "immediate": True, "spawnPoints": spawn_points, "groupName": group_name,
        })

    # Orders. Olympus applies these to the whole group of the given unit ID.
    def set_roe(self, unit_id: int, roe: str) -> None:
        self.send("setROE", {"ID": unit_id, "ROE": ROES.index(roe)})

    def set_alarm_state(self, unit_id: int, state: str) -> None:
        self.send("setAlarmState", {"ID": unit_id, "alarmState": ALARM_STATES[state]})

    def set_reaction_to_threat(self, unit_id: int, reaction: str) -> None:
        self.send("setReactionToThreat", {"ID": unit_id, "reactionToThreat": REACTIONS.index(reaction)})

    def set_path(self, unit_id: int, points: list[tuple[float, float]]) -> None:
        self.send("setPath", {"ID": unit_id, "path": [{"lat": lat, "lng": lng} for lat, lng in points]})

    def set_follow_roads(self, unit_id: int, follow: bool) -> None:
        self.send("setFollowRoads", {"ID": unit_id, "followRoads": follow})

    def set_altitude(self, unit_id: int, altitude_m: float) -> None:
        self.send("setAltitude", {"ID": unit_id, "altitude": altitude_m})

    def set_speed(self, unit_id: int, speed_ms: float) -> None:
        self.send("setSpeed", {"ID": unit_id, "speed": speed_ms})

    def attack_unit(self, unit_id: int, target_id: int) -> None:
        self.send("attackUnit", {"ID": unit_id, "targetID": target_id})

    def fire_at_area(self, unit_id: int, lat: float, lng: float) -> None:
        """Artillery: DCS FireAtPoint at this spot (100 m radius). Sending it again re-tasks the group."""
        self.send("fireAtArea", {"ID": unit_id, "location": {"lat": lat, "lng": lng}})

    def create_marker(self, marker_id: int, lat: float, lng: float, text: str) -> None:
        self.send("createMarker", {"markerID": marker_id, "location": {"lat": lat, "lng": lng}, "text": text})
