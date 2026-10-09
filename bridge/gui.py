"""A small window for the Red commander: edit the scenario and settings, start and stop the bridge, see its log.

  python -m bridge.gui        (or double-click start-red-commander-gui.bat)

Status lights, checked every few seconds:
- DCS: a DCS.exe or DCS_server.exe process is running on this machine.
- Mission (Olympus): the Olympus backend answers. It only does while a mission is running with the mod enabled.
- Olympus web: the Olympus web interface port (frontend.port in olympus.json) accepts connections.
- API key: an Anthropic API key is set, in the environment or in the box below.
- Commander: the bridge process started from this window is running.
"""

from __future__ import annotations

import dataclasses
import json
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import requests
import yaml

from .planner import PRICES
from .scenario import Campaign, Rules
from .scenario import load_scenario as check_scenario

ROOT = Path(__file__).resolve().parent.parent
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
COST_LINE = re.compile(r"This mission: (\d+) calls, \$([\d.]+)")
CHECK_EVERY_MS = 5000
STOP_GRACE_S = 10
WINDOWS = sys.platform == "win32"

GREEN, RED, AMBER, GRAY = "#2e7d32", "#c62828", "#f9a825", "#9e9e9e"

# ---------- YAML that keeps the comments in your files, when ruamel.yaml is installed ----------

try:
    from ruamel.yaml import YAML

    _rt = YAML()
    _rt.preserve_quotes = True
    _rt.width = 4096
    _rt.representer.add_representer(type(None), lambda r, _: r.represent_scalar("tag:yaml.org,2002:null", "null"))

    def load_doc(path: Path) -> dict:
        return (_rt.load(path.read_text(encoding="utf-8")) if path.exists() else None) or {}

    def dump_doc(doc: dict, path: Path) -> None:
        with path.open("w", encoding="utf-8") as fh:
            _rt.dump(doc, fh)
except ImportError:  # PyYAML only: saving works, but drops the comments
    def load_doc(path: Path) -> dict:
        return (yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None) or {}

    def dump_doc(doc: dict, path: Path) -> None:
        path.write_text(yaml.safe_dump(json.loads(json.dumps(doc)), sort_keys=False, allow_unicode=True), encoding="utf-8")


def to_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return yaml.safe_dump(json.loads(json.dumps(value)), default_flow_style=True, width=4096).strip()
    return str(value)


def parse_text(text: str, as_str: bool):
    """Entry text back to a YAML value. Empty means null (for example keep_sams_dark_until_km: off)."""
    text = text.strip()
    if text == "":
        return None
    if as_str:
        return text
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return text


def get_path(doc: dict, path: tuple):
    for key in path:
        if not isinstance(doc, dict) or key not in doc:
            return None
        doc = doc[key]
    return doc


def set_path(doc: dict, path: tuple, value) -> None:
    for key in path[:-1]:
        if not isinstance(doc.get(key), dict):
            doc[key] = {}
        doc = doc[key]
    if value is None and path[-1] not in doc:
        return
    doc[path[-1]] = value


@dataclasses.dataclass
class Field:
    path: tuple
    value: object
    in_file: bool  # False: a default the file does not set yet; it is only written if you change it
    as_str: bool = False
    choices: list | None = None
    browse: bool = False
    hint: str = ""

    @property
    def label(self) -> str:
        return ".".join(str(p) for p in self.path[1:]) if len(self.path) > 1 else str(self.path[0])


def _walk(doc: dict, prefix: tuple):
    for key, value in doc.items():
        if isinstance(value, dict) and value:
            yield from _walk(value, prefix + (key,))
        elif isinstance(value, list) and any(isinstance(v, dict) for v in value):
            continue  # lists of entries (inventory) are edited in the file
        else:
            yield prefix + (key,), value


def scenario_fields(doc: dict) -> dict[str, list[Field]]:
    """The scenario's settings grouped by section, plus the rules the file leaves at their defaults."""
    sections: dict[str, list[Field]] = {"General": [], "Objective": [], "Campaign": [], "Rules": [], "Catalog": []}
    for key, value in doc.items():
        if key in ("objective", "campaign", "rules", "catalog", "inventory"):
            continue
        sections["General"].append(Field((key,), value, True, as_str=isinstance(value, str)))
    for name, key, defaults, skip in [("Objective", "objective", None, ()),
                                      ("Campaign", "campaign", Campaign, ("hq", "staging_name", "staging_lat", "staging_lng")),
                                      ("Rules", "rules", Rules, ()),
                                      ("Catalog", "catalog", None, ())]:
        section = doc.get(key)
        if key == "campaign" and not section:
            continue
        section = section if isinstance(section, dict) else {}
        for path, value in _walk(section, (key,)):
            sections[name].append(Field(path, value, True, as_str=isinstance(value, str)))
        if defaults:
            for f in dataclasses.fields(defaults):
                if f.name not in section and f.name not in skip and f.default is not dataclasses.MISSING:
                    sections[name].append(Field((key, f.name), f.default, False, as_str=isinstance(f.default, str)))
    return {k: v for k, v in sections.items() if v}


def config_fields(doc: dict) -> list[Field]:
    o, p = doc.get("olympus") or {}, doc.get("planner") or {}
    get = lambda d, k, default: (d[k], True) if k in d else (default, False)
    rows = [
        (("olympus", "saved_games_dcs"), get(o, "saved_games_dcs", ""), dict(as_str=True, browse=True, hint="empty: find it under %USERPROFILE%")),
        (("olympus", "address"), get(o, "address", ""), dict(as_str=True, hint="host:port; empty: as in olympus.json")),
        (("planner", "model"), get(p, "model", "claude-opus-5-5"), dict(as_str=True, choices=list(PRICES))),
        (("planner", "effort"), get(p, "effort", "high"), dict(as_str=True, choices=EFFORTS)),
        (("planner", "max_tokens"), get(p, "max_tokens", 16000), {}),
        (("planner", "repair_rounds"), get(p, "repair_rounds", 1), {}),
        (("check_terrain",), get(doc, "check_terrain", True), {}),
        (("log_dir",), get(doc, "log_dir", "logs"), dict(as_str=True)),
    ]
    return [Field(path, value, in_file, **opts) for path, (value, in_file), opts in rows]


# ---------- status checks (run on a worker thread) ----------

def dcs_running() -> tuple[str, str]:
    try:
        if WINDOWS:
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=10,
                                 creationflags=subprocess.CREATE_NO_WINDOW).stdout.lower()
            found = [n for n in ("dcs_server.exe", "dcs.exe") if f'"{n}"' in out]
        else:
            out = subprocess.run(["ps", "-eo", "comm"], capture_output=True, text=True, timeout=10).stdout.lower()
            found = [n for n in ("dcs_server", "dcs") if n in out.split()]
    except (OSError, subprocess.SubprocessError):
        return GRAY, "cannot check"
    return (GREEN, f"running ({found[0]})") if found else (RED, "not running")


def olympus_status(cfg: dict) -> list[tuple[str, str]]:
    """[(colour, text) for the mission/backend light, (colour, text) for the web interface light]."""
    from .__main__ import olympus_paths
    from .olympus import OlympusClient, OlympusError
    try:
        olympus_json, _ = olympus_paths(cfg)
        client = OlympusClient.from_olympus_json(olympus_json)
        raw = json.loads(olympus_json.read_text(encoding="utf-8"))
    except SystemExit:
        return [(RED, "olympus.json not found: set the Saved Games folder in Settings"), (GRAY, "-")]
    except (OSError, ValueError, KeyError, OlympusError) as exc:
        return [(RED, f"olympus.json: {exc}"), (GRAY, "-")]
    if (cfg.get("olympus") or {}).get("address"):
        client.base_url = f"http://{cfg['olympus']['address']}/olympus"
    client.timeout = 3
    host = client.base_url.split("//", 1)[1].split(":", 1)[0]
    try:
        bases = [a for a in client.get_airbases() if a.get("coalition") == "red"]
        mission = (GREEN, f"mission running, {len(bases)} red airbases ({client.base_url})")
    except OlympusError as exc:
        mission = (RED, str(exc))
    except requests.RequestException:
        mission = (RED, f"no answer at {client.base_url}: start the mission (unpaused)")
    port = (raw.get("frontend") or {}).get("port")
    if not port:
        return [mission, (GRAY, "no frontend.port in olympus.json")]
    try:
        with socket.create_connection((host, int(port)), timeout=2):
            web = (GREEN, f"up at http://{host}:{port}")
    except OSError:
        web = (RED, f"nothing on {host}:{port}")
    return [mission, web]


# ---------- the window ----------

class Light:
    def __init__(self, parent, row: int, name: str):
        self.canvas = tk.Canvas(parent, width=16, height=16, highlightthickness=0)
        self.dot = self.canvas.create_oval(2, 2, 14, 14, fill=GRAY, outline="")
        self.canvas.grid(row=row, column=0, padx=(0, 6), pady=1)
        ttk.Label(parent, text=name, width=16).grid(row=row, column=1, sticky="w")
        self.text = ttk.Label(parent, text="checking...", foreground="#555")
        self.text.grid(row=row, column=2, sticky="w")

    def set(self, colour: str, text: str) -> None:
        self.canvas.itemconfigure(self.dot, fill=colour)
        self.text.configure(text=text)


class FieldForm:
    """Entries for a list of Fields, laid out in a grid."""

    def __init__(self, parent, fields: list[Field], start_row: int = 0):
        self.vars: list[tuple[Field, tk.Variable, str]] = []
        row = start_row
        for f in fields:
            ttk.Label(parent, text=f.label, foreground="#000" if f.in_file else "#777").grid(row=row, column=0, sticky="w", padx=(8, 6), pady=1)
            if isinstance(f.value, bool):
                var = tk.BooleanVar(value=f.value)
                ttk.Checkbutton(parent, variable=var).grid(row=row, column=1, sticky="w")
                original = str(f.value)
            else:
                original = to_text(f.value)
                var = tk.StringVar(value=original)
                if f.choices:
                    widget = ttk.Combobox(parent, textvariable=var, values=f.choices, width=40)
                else:
                    widget = ttk.Entry(parent, textvariable=var, width=42)
                widget.grid(row=row, column=1, sticky="we")
            if f.browse:
                ttk.Button(parent, text="Browse...", command=lambda v=var: self._browse(v)).grid(row=row, column=2, sticky="w", padx=4)
            elif f.hint or not f.in_file:
                ttk.Label(parent, text=f.hint or "default", foreground="#777").grid(row=row, column=2, sticky="w", padx=4)
            self.vars.append((f, var, original))
            row += 1
        parent.columnconfigure(1, weight=1)
        self.next_row = row

    @staticmethod
    def _browse(var: tk.StringVar) -> None:
        folder = filedialog.askdirectory(title="Your DCS Saved Games folder (the one with Config/olympus.json)")
        if folder:
            var.set(folder)

    def apply(self, doc: dict) -> bool:
        """Write what changed into doc and say whether anything did. Values left alone keep their formatting."""
        changed = False
        for f, var, original in self.vars:
            if isinstance(var, tk.BooleanVar):
                if str(var.get()) != original:
                    set_path(doc, f.path, var.get())
                    changed = True
                continue
            text = var.get()
            if text.strip() == original.strip():
                continue
            changed = True
            value = parse_text(text, f.as_str)
            if value is None and f.as_str:  # an empty path or address: remove it so the default applies
                parent = get_path(doc, f.path[:-1]) if len(f.path) > 1 else doc
                if isinstance(parent, dict):
                    parent.pop(f.path[-1], None)
                continue
            if isinstance(f.value, (int, float)) and not isinstance(f.value, bool) and not isinstance(value, (int, float, type(None))):
                raise ValueError(f"{f.label} must be a number, not '{text.strip()}'")
            set_path(doc, f.path, value)
        return changed


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue[str] = queue.Queue()
        self.status: queue.Queue[dict] = queue.Queue()
        self.exit_code: int | None = None
        self.cfg_path = ROOT / "config.yaml"
        self.cfg: dict = {}
        self.api_key = tk.StringVar()
        root.title("Red Commander")
        root.geometry("1000x780")
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        lights = ttk.LabelFrame(root, text="Status", padding=6)
        lights.pack(fill="x", padx=8, pady=(8, 4))
        self.lights = {k: Light(lights, i, name) for i, (k, name) in enumerate(
            [("dcs", "DCS"), ("mission", "Mission (Olympus)"), ("web", "Olympus web"), ("key", "API key"), ("bridge", "Commander")])}

        panes = ttk.PanedWindow(root, orient="vertical")
        panes.pack(fill="both", expand=True, padx=8, pady=4)
        tabs = ttk.Notebook(panes)
        panes.add(tabs, weight=3)
        self.scenario_tab = ttk.Frame(tabs, padding=4)
        self.settings_tab = ttk.Frame(tabs, padding=4)
        tabs.add(self.scenario_tab, text="Scenario")
        tabs.add(self.settings_tab, text="Settings")
        self._build_scenario_tab()
        self._build_settings_tab()

        controls = ttk.Frame(panes)
        panes.add(controls, weight=2)
        bar = ttk.Frame(controls)
        bar.pack(fill="x", pady=(4, 4))
        self.start_btn = ttk.Button(bar, text="Start", command=lambda: self.start("run"))
        self.start_btn.pack(side="left")
        self.plan_btn = ttk.Button(bar, text="Plan only", command=lambda: self.start("plan"))
        self.plan_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(bar, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left")
        self.no_reviews = tk.BooleanVar(value=False)
        self.replan = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="No reviews (campaign)", variable=self.no_reviews).pack(side="left", padx=(12, 0))
        ttk.Checkbutton(bar, text="New plan on mission restart", variable=self.replan).pack(side="left", padx=6)
        self.cost = ttk.Label(bar, text="Claude this mission: 0 calls, $0.00")
        self.cost.pack(side="right")

        log_frame = ttk.Frame(controls)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, height=14, wrap="word", font=("Consolas", 9) if WINDOWS else ("TkFixedFont", 9), state="disabled")
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.tag_configure("WARNING", foreground="#b26a00")
        self.log.tag_configure("ERROR", foreground=RED)
        self.log.tag_configure("note", foreground="#1565c0")

        self.root.after(200, self.drain)
        self.check_status()

    # ----- scenario tab -----
    def _build_scenario_tab(self) -> None:
        top = ttk.Frame(self.scenario_tab)
        top.pack(fill="x")
        ttk.Label(top, text="Scenario file").pack(side="left")
        self.scenario_var = tk.StringVar()
        files = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "scenarios").glob("*.yaml"))
        self.scenario_box = ttk.Combobox(top, textvariable=self.scenario_var, values=files, width=50, state="readonly")
        self.scenario_box.pack(side="left", padx=6)
        self.scenario_box.bind("<<ComboboxSelected>>", lambda e: self.load_scenario())
        ttk.Button(top, text="Save", command=self.save_scenario).pack(side="left")
        ttk.Button(top, text="Save as...", command=lambda: self.save_scenario(ask=True)).pack(side="left", padx=4)
        ttk.Button(top, text="Open in editor", command=lambda: self.open_file(ROOT / self.scenario_var.get())).pack(side="left")

        canvas = tk.Canvas(self.scenario_tab, highlightthickness=0)
        scroll = ttk.Scrollbar(self.scenario_tab, command=canvas.yview)
        self.form_frame = ttk.Frame(canvas)
        self.form_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        window = canvas.create_window((0, 0), window=self.form_frame, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        wheel = lambda e: canvas.yview_scroll((-1 if e.delta > 0 else 1) if e.delta else (-1 if e.num == 4 else 1), "units")
        canvas.bind("<Enter>", lambda e: [canvas.bind_all(ev, wheel) for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>")])
        canvas.bind("<Leave>", lambda e: [canvas.unbind_all(ev) for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>")])
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True, pady=(6, 0))
        self.scenario_forms: list[FieldForm] | None = None
        start = "scenarios/take-kutaisi.yaml" if (ROOT / "scenarios/take-kutaisi.yaml").exists() else (files[0] if files else "")
        self.scenario_var.set(start.replace("/", os.sep))
        if start:
            self.load_scenario()

    def load_scenario(self) -> None:
        for child in self.form_frame.winfo_children():
            child.destroy()
        path = ROOT / self.scenario_var.get()
        try:
            self.scenario_doc = load_doc(path)
        except Exception as exc:  # a YAML error: show it, the file can still be fixed in the editor
            ttk.Label(self.form_frame, text=f"Cannot read {path.name}: {exc}", foreground=RED).grid(row=0, column=0)
            self.scenario_forms = None
            return
        self.scenario_forms, row = [], 0
        for section, items in scenario_fields(self.scenario_doc).items():
            ttk.Label(self.form_frame, text=section, font=("TkDefaultFont", 10, "bold")).grid(row=row, column=0, sticky="w", pady=(8, 2))
            form = FieldForm(self.form_frame, items, row + 1)
            self.scenario_forms.append(form)
            row = form.next_row
        if self.scenario_doc.get("inventory"):
            ttk.Label(self.form_frame, text="The inventory list is edited in the file (Open in editor).",
                      foreground="#777").grid(row=row, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(self.form_frame, text="Grey names are defaults the file does not set; they are only written if you change them. "
                  "Empty means none (null).", foreground="#777").grid(row=row + 1, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def save_scenario(self, ask: bool = False) -> bool:
        if self.scenario_forms is None:
            messagebox.showerror("Scenario not saved", "Fix the scenario file first (Open in editor).")
            return False
        path = ROOT / self.scenario_var.get()
        if ask:
            chosen = filedialog.asksaveasfilename(initialdir=ROOT / "scenarios", defaultextension=".yaml",
                                                  filetypes=[("Scenario", "*.yaml")])
            if not chosen:
                return False
            path = Path(chosen)
        tmp = path.with_name(path.name + ".tmp")
        try:
            changed = [form.apply(self.scenario_doc) for form in self.scenario_forms]
            if not ask and not any(changed):
                return True
            dump_doc(self.scenario_doc, tmp)
            check_scenario(tmp)  # the same checks the bridge makes
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            messagebox.showerror("Scenario not saved", f"{exc}")
            return False
        tmp.replace(path)
        self.note(f"Saved {path.name}")
        if ask:
            try:
                rel = str(path.relative_to(ROOT))
            except ValueError:
                rel = str(path)
            self.scenario_box.configure(values=sorted(set(self.scenario_box.cget("values")) | {rel}))
            self.scenario_var.set(rel)
        self.load_scenario()
        return True

    # ----- settings tab -----
    def _build_settings_tab(self) -> None:
        for child in self.settings_tab.winfo_children():
            child.destroy()
        source = self.cfg_path if self.cfg_path.exists() else ROOT / "config.example.yaml"
        self.cfg_doc = load_doc(source)
        self.cfg = json.loads(json.dumps(self.cfg_doc)) if self.cfg_path.exists() else {}
        self.settings_form = FieldForm(self.settings_tab, config_fields(self.cfg_doc))
        row = self.settings_form.next_row
        ttk.Label(self.settings_tab, text="Anthropic API key").grid(row=row, column=0, sticky="w", padx=(8, 6), pady=(10, 1))
        ttk.Entry(self.settings_tab, textvariable=self.api_key, show="*", width=42).grid(row=row, column=1, sticky="we", pady=(10, 1))
        ttk.Label(self.settings_tab, text="optional; used for this window only, never saved", foreground="#777").grid(
            row=row, column=2, sticky="w", padx=4, pady=(10, 1))
        bar = ttk.Frame(self.settings_tab)
        bar.grid(row=row + 1, column=0, columnspan=3, sticky="w", padx=8, pady=10)
        ttk.Button(bar, text="Save settings", command=self.save_settings).pack(side="left")
        ttk.Button(bar, text="Open config.yaml", command=lambda: self.open_file(self.cfg_path)).pack(side="left", padx=6)
        if not self.cfg_path.exists():
            ttk.Label(self.settings_tab, text="No config.yaml yet: saving creates it from config.example.yaml.",
                      foreground="#777").grid(row=row + 2, column=0, columnspan=3, sticky="w", padx=8)

    def save_settings(self) -> bool:
        try:
            if not self.settings_form.apply(self.cfg_doc) and self.cfg_path.exists():
                return True
        except ValueError as exc:
            messagebox.showerror("Settings not saved", str(exc))
            return False
        effort = (self.cfg_doc.get("planner") or {}).get("effort")
        if effort and effort not in EFFORTS:
            messagebox.showerror("Settings not saved", f"effort must be one of {', '.join(EFFORTS)}")
            return False
        dump_doc(self.cfg_doc, self.cfg_path)
        self.note(f"Saved {self.cfg_path.name}")
        self._build_settings_tab()
        return True

    # ----- running the bridge -----
    def start(self, command: str) -> None:
        if self.proc and self.proc.poll() is None:
            return
        if not (self.save_settings() and self.save_scenario()):
            return
        cmd = [sys.executable, "-u", "-m", "bridge", command, self.scenario_var.get(), "--config", str(self.cfg_path)]
        if command == "run" and self.no_reviews.get():
            cmd.append("--no-reviews")
        if command == "run" and self.replan.get():
            cmd.append("--replan")
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        if self.api_key.get().strip():
            env["ANTHROPIC_API_KEY"] = self.api_key.get().strip()
        self.note("> " + " ".join(cmd[2:]))
        self.cost.configure(text="Claude this mission: 0 calls, $0.00")
        self.proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace", bufsize=1,
                                     creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0)
        self.exit_code = None
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
        self._buttons()

    def _read(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            self.lines.put(line.rstrip("\n"))
        proc.wait()
        self.lines.put(f"\x00{proc.returncode}")

    def stop(self) -> None:
        if not self.proc or self.proc.poll() is not None:
            return
        self.note("Stopping the commander (units stay in the mission)...")
        try:
            self.proc.send_signal(signal.CTRL_BREAK_EVENT if WINDOWS else signal.SIGINT)
        except OSError:
            self.proc.terminate()
        proc = self.proc
        self.root.after(STOP_GRACE_S * 1000, lambda: proc.poll() is None and proc.kill())

    def _buttons(self) -> None:
        running = bool(self.proc and self.proc.poll() is None)
        self.start_btn.configure(state="disabled" if running else "normal")
        self.plan_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        self._local_lights()

    # ----- log -----
    def note(self, text: str) -> None:
        self._append(text, "note")

    def _append(self, line: str, tag: str | None = None) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n", tag or next((t for t in ("ERROR", "WARNING") if f" {t} " in line), ()))
        if int(self.log.index("end-1c").split(".")[0]) > 5000:
            self.log.delete("1.0", "1000.0")
        self.log.configure(state="disabled")
        self.log.see("end")

    def drain(self) -> None:
        while True:
            try:
                line = self.lines.get_nowait()
            except queue.Empty:
                break
            if line.startswith("\x00"):
                self.exit_code = int(line[1:])
                self.note(f"The commander has stopped (exit code {self.exit_code}).")
                self._buttons()
                continue
            self._append(line)
            if m := COST_LINE.search(line):
                self.cost.configure(text=f"Claude this mission: {m.group(1)} calls, ${float(m.group(2)):.2f}")
        self.root.after(200, self.drain)

    # ----- status lights -----
    def check_status(self) -> None:
        cfg = dict(self.cfg)
        threading.Thread(target=lambda: self.status.put({"dcs": dcs_running(), **dict(zip(("mission", "web"), olympus_status(cfg)))}),
                         daemon=True).start()
        self.root.after(500, self._show_status)

    def _show_status(self) -> None:
        try:
            result = self.status.get_nowait()
        except queue.Empty:
            self.root.after(500, self._show_status)
            return
        for key, (colour, text) in result.items():
            self.lights[key].set(colour, text)
        self._local_lights()
        self.root.after(CHECK_EVERY_MS, self.check_status)

    def _local_lights(self) -> None:
        if self.api_key.get().strip() or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            self.lights["key"].set(GREEN, "set" + (" (from this window)" if self.api_key.get().strip() else " (environment)"))
        else:
            self.lights["key"].set(RED, "not set: setx ANTHROPIC_API_KEY, or paste one under Settings")
        if self.proc and self.proc.poll() is None:
            self.lights["bridge"].set(GREEN, f"running (pid {self.proc.pid})")
        elif self.exit_code not in (None, 0):
            self.lights["bridge"].set(RED, f"stopped with exit code {self.exit_code}: see the log")
        else:
            self.lights["bridge"].set(GRAY, "not running")

    # ----- misc -----
    def open_file(self, path: Path) -> None:
        if not path.exists():
            messagebox.showinfo("Not there yet", f"{path.name} does not exist yet. Save first.")
            return
        if WINDOWS:
            os.startfile(path)  # opens with the program .yaml files are associated with (or asks)
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def on_close(self) -> None:
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno("Quit", "The commander is running. Stop it and quit? Its units stay in the mission."):
                return
            self.stop()
            deadline = time.time() + STOP_GRACE_S
            while self.proc.poll() is None and time.time() < deadline:
                time.sleep(0.2)
            if self.proc.poll() is None:
                self.proc.kill()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if WINDOWS else "clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
