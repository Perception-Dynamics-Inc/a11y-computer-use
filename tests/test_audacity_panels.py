"""Audacity's painted wx panels retain honest roles and surface hints.

Hermetic. No display, bus, or Audacity process is needed.
"""

from __future__ import annotations

import pytest

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.observe import DisplayGeometry, build_snapshot
from a11y_computer_use.schema import Display, Scope


class _Node:
    def __init__(self, role, name="", *, description="", children=(),
                 size=(52.0, 52.0), focusable=False):
        self.role = role
        self.name = name
        self.description = description
        self.children = list(children)
        self.size = size
        self.focusable = focusable
        self.parent = None
        for child in self.children:
            child.parent = self

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_description(self):
        return self.description

    def get_parent(self):
        return self.parent

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]


class _Tree:
    def read(self, node):
        return _atspi.ATSPIAccessor().read(node)

    def children(self, node):
        return node.children


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    monkeypatch.setattr(_atspi, "_extents", lambda node, **kw: ((10.0, 10.0), node.size))
    monkeypatch.setattr(
        _atspi, "_state_flags",
        lambda node: (True, False, None, False, None, node.focusable, False),
    )
    monkeypatch.setattr(_atspi, "_value_text", lambda *args, **kw: None)


def _frame(*children, app="audacity", role="frame"):
    frame = _Node(role, "Audacity", children=children, size=(1200.0, 700.0))
    _Node("application", app, children=[frame])
    return frame


def _snapshot(frame):
    return build_snapshot(
        frame, _Tree(), scope=Scope.WINDOW, app="audacity", pid=1,
        geometry=(DisplayGeometry(Display(0, 1280, 800, 1.0, True), (0.0, 0.0)),),
    )


@pytest.mark.parametrize("description", [
    "Play (Space) / Play Once (Shift+Space)", "Pause (P)", "Stop (Space)",
    "Record (R)", "Zoom In", "Selection Tool",
])
def test_toolbar_tool_is_a_findable_clickable_button(description):
    tool = _Node("panel", description=description)
    frame = _frame(_Node("tool bar", "Transport", children=[_Node("panel", children=[tool])],
                         size=(500.0, 60.0)))
    snap = _snapshot(frame)
    matches = observe.find_elements(snap, role="button", text=description)
    assert len(matches) == 1
    assert matches[0].title == description
    assert matches[0].clickable


@pytest.mark.parametrize("toolbar,description", [
    ("Transport", "Play (Space) / Play Once (Shift+Space)"),
    ("Transport", "Pause (P)"), ("Transport", "Stop"),
    ("Transport", "Record (R) / Record New Track (Shift+R)"),
    ("Edit", "Zoom In"), ("Tools", "Selection Tool"),
])
def test_toolbar_sibling_grip_identifies_buttons(toolbar, description):
    grip = _Node("panel", description=f"Audacity {toolbar} Toolbar", size=(10.0, 63.0))
    tool = _Node("panel", description=description)
    frame = _frame(_Node("panel", children=[grip, tool]))
    matches = observe.find_elements(_snapshot(frame), role="button")
    assert [el.title for el in matches] == [description]
    assert matches[0].clickable
    assert _atspi.ATSPIAccessor().read(grip).role == "AXGroup"


@pytest.mark.parametrize("case", ["other_app", "dialog", "separate_container", "named", "children"])
def test_toolbar_grip_does_not_reclassify_unrelated_panels(case):
    grip = _Node("panel", description="Audacity Transport Toolbar")
    tool = _Node("panel", "Named" if case == "named" else "", description="Play (Space)",
                 children=[_Node("label", "Child")] if case == "children" else [])
    containers = ([_Node("panel", children=[grip]), _Node("panel", children=[tool])]
                  if case == "separate_container" else [_Node("panel", children=[grip, tool])])
    _frame(*containers, app="other" if case == "other_app" else "audacity",
           role="dialog" if case == "dialog" else "frame")
    assert _atspi.ATSPIAccessor().read(tool).role == "AXGroup"


def test_track_and_clock_remain_opaque_with_surface_notes():
    track = _Node("panel", size=(1096.0, 436.0), focusable=True)
    clock = _Node("panel", size=(230.0, 41.0))
    frame = _frame(track, _Node("tool bar", "Time", children=[clock], size=(250.0, 60.0)))
    snap = _snapshot(frame)
    regions = observe.find_elements(snap, role="opaque_region")
    assert {el.title for el in regions} == {"Audacity track / waveform", "Audacity Time clock"}
    assert "Tracks > Mute/Unmute" in next(el.note for el in regions if "waveform" in el.title)
    assert "not exposed" in next(el.note for el in regions if "clock" in el.title)
    assert not observe.find_elements(snap, text="Mute")
    assert not observe.find_elements(snap, text="Solo")
    assert not observe.find_elements(snap, text="probe_tone")


@pytest.mark.parametrize("app,role", [("other", "frame"), ("audacity", "dialog")])
def test_other_apps_and_dialogs_are_not_reclassified(app, role):
    track = _Node("panel", size=(1096.0, 436.0), focusable=True)
    tool = _Node("panel", description="Play (Space)")
    _frame(track, _Node("tool bar", "Transport", children=[tool]), app=app, role=role)
    raw = _atspi.ATSPIAccessor().read(track)
    assert raw.surface_name == ""
    assert raw.note == ""
    assert _atspi.ATSPIAccessor().read(tool).role == "AXGroup"


def test_real_track_controls_and_named_panels_are_preserved():
    mute = _Node("push button", "Mute")
    track = _Node("panel", children=[mute], size=(1096.0, 436.0), focusable=True)
    named = _Node("panel", "Real panel", description="Help")
    frame = _frame(track, _Node("tool bar", "Transport", children=[named]))
    snap = _snapshot(frame)
    assert len(observe.find_elements(snap, role="button", text="Mute")) == 1
    assert _atspi.ATSPIAccessor().read(track).surface_name == ""
    assert _atspi.ATSPIAccessor().read(named).role == "AXGroup"


def test_tooltip_outside_toolbar_is_not_a_button():
    panel = _Node("panel", description="Play (Space)")
    _frame(panel)
    assert _atspi.ATSPIAccessor().read(panel).role == "AXGroup"
