"""A stand-in for the Olympus backend: same URLs, same binary /units format, no DCS."""

from __future__ import annotations

import base64
import itertools
import json
import struct
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PASSWORD = "0" * 64  # stands in for the sha256 hash in olympus.json


@dataclass
class FakeUnit:
    id: int
    category: str
    coalition: int  # 1 red, 2 blue
    name: str
    group_name: str
    lat: float
    lng: float
    alt: float = 0.0
    alive: bool = True
    is_leader: bool = True
    contacts: list[int] = field(default_factory=list)


def _s(index: int, text: str) -> bytes:
    raw = text.encode()
    return struct.pack("<BH", index, len(raw)) + raw


def encode_unit(u: FakeUnit) -> bytes:
    out = struct.pack("<I", u.id)
    out += _s(1, u.category)
    out += struct.pack("<B?", 2, u.alive)
    out += struct.pack("<BB", 3, 0)  # alarm state auto
    out += struct.pack("<BB", 7, u.coalition)
    out += _s(9, u.name)
    out += _s(11, "Enfield11")  # callsign: a string we skip
    out += struct.pack("<BI", 13, u.id * 10)
    out += _s(14, u.group_name)
    out += struct.pack("<Bdddd", 18, u.lat, u.lng, u.alt, 0.0)
    out += struct.pack("<Bd", 22, 1.5)
    out += struct.pack("<B?BcI", 40, True, 40, b"X", 0)[:8]  # TACAN, 7 bytes after the index
    out += struct.pack("<BH", 43, 1) + struct.pack("<H33sBBB", 4, b"R-73", 1, 1, 1)  # one ammo entry
    out += struct.pack("<BH", 44, len(u.contacts)) + b"".join(struct.pack("<IB", c, 4) for c in u.contacts)
    out += struct.pack("<B?", 46, u.is_leader)
    out += struct.pack("<BB", 50, 100)
    out += struct.pack("<B?", 65, u.alt > 50)
    out += struct.pack("<B", 255)
    return out


class FakeOlympus:
    def __init__(self):
        self.units: dict[int, FakeUnit] = {}
        self.commands: list[tuple[str, dict]] = []
        self.airbases = {
            "1": {"callsign": "Kutaisi", "coalition": "red", "latitude": 42.176, "longitude": 42.482},
            "2": {"callsign": "Senaki-Kolkhi", "coalition": "red", "latitude": 42.24, "longitude": 42.06},
            "3": {"callsign": "Batumi", "coalition": "blue", "latitude": 41.61, "longitude": 41.60},
        }
        self._ids = itertools.count(1000)
        self._hashes = itertools.count(1)
        self.lock = threading.Lock()

    def add(self, **kw) -> FakeUnit:
        u = FakeUnit(id=next(self._ids), **kw)
        self.units[u.id] = u
        return u

    def names_sent(self) -> list[str]:
        return [name for name, _ in self.commands]

    def _spawn(self, body: dict, category: str) -> None:
        for i, spec in enumerate(body["units"]):
            loc = spec["location"]
            self.add(category=category, coalition=1 if body["coalition"] == "red" else 2, name=spec["unitType"],
                     group_name=body["groupName"], lat=loc["lat"], lng=loc["lng"], alt=spec.get("altitude") or 0,
                     is_leader=i == 0)

    def handle_put(self, payload: dict) -> dict:
        with self.lock:
            for name, body in payload.items():
                self.commands.append((name, body))
                if name == "spawnGroundUnits":
                    self._spawn(body, "GroundUnit")
                elif name == "spawnAircrafts":
                    self._spawn(body, "Aircraft")
            return {"commandHash": f"h{next(self._hashes)}"}

    def units_payload(self) -> bytes:
        with self.lock:
            return struct.pack("<Q", 1) + b"".join(encode_unit(u) for u in self.units.values())

    def serve(self) -> tuple[ThreadingHTTPServer, str]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _authorized(self) -> bool:
                auth = self.headers.get("Authorization", "")
                ok = auth.startswith("Basic ") and base64.b64decode(auth[6:]).decode().split(":", 1)[1] == PASSWORD
                if not ok:
                    self.send_response(401)
                    self.end_headers()
                return ok

            def _reply(self, body: bytes, kind: str = "application/json"):
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if not self._authorized():
                    return
                url = urlparse(self.path)
                what = url.path.rstrip("/").split("/")[-1]
                if what == "units":
                    self._reply(fake.units_payload(), "application/octet-stream")
                elif what == "airbases":
                    self._reply(json.dumps({"airbases": fake.airbases}).encode())
                elif what == "commands":
                    self._reply(json.dumps({"commandExecuted": True, "commandResult": 1}).encode())
                else:
                    self._reply(b"{}")

            def do_PUT(self):
                if not self._authorized():
                    return
                length = int(self.headers.get("Content-Length", 0))
                self._reply(json.dumps(fake.handle_put(json.loads(self.rfile.read(length)))).encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, f"http://127.0.0.1:{server.server_port}/olympus"
