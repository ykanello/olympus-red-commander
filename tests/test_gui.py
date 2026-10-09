"""The GUI's YAML helpers (no window is opened)."""

import pytest

pytest.importorskip("tkinter")

from bridge import gui  # noqa: E402

from .test_bridge import ROOT  # noqa: E402


def test_scenario_fields_show_file_values_and_rule_defaults():
    doc = gui.load_doc(ROOT / "scenarios/take-kutaisi.yaml")
    sections = gui.scenario_fields(doc)
    by_path = {f.path: f for fields in sections.values() for f in fields}
    assert by_path[("objective", "location", "lat")].value == 42.176
    assert by_path[("campaign", "staging", "name")].value == "Gali"
    assert by_path[("rules", "skill")].in_file and not by_path[("rules", "poll_seconds")].in_file
    assert ("campaign", "staging_lat") not in by_path  # staging is edited as campaign.staging.*


def test_text_round_trip():
    assert gui.parse_text(gui.to_text(None), False) is None
    assert gui.parse_text(gui.to_text(["Late Cold War", "Modern"]), False) == ["Late Cold War", "Modern"]
    assert gui.parse_text("12.5", False) == 12.5 and gui.parse_text("true", False) is True
    assert gui.parse_text("yes", True) == "yes"  # names stay text


def test_set_path_creates_sections():
    doc = {}
    gui.set_path(doc, ("olympus", "address"), "localhost:4512")
    gui.set_path(doc, ("rules", "x"), None)
    assert doc == {"olympus": {"address": "localhost:4512"}, "rules": {}}
