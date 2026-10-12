"""Inkscape tool buttons keep the short name, with the tooltip beside it.

Hermetic. No display, no bus, no Inkscape process. The accessible name is
the tool name, a newline, and the tooltip sentence ending in a shortcut.
The snapshot name is the first line. The sentence is ``hint`` and the
chord is ``shortcut``. GIMP Shift/Ctrl lines are a different pattern and
stay on that split.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.observe import DisplayGeometry, RawNode, build_snapshot
from a11y_computer_use.schema import Display, Scope


_TEXT = "Text Tool\nCreate and edit text objects (T)"
_SELECT = "Selector Tool\nSelect and transform objects (S)"
_SAVE = "Save\nSave document (Ctrl+S)"
_ZOOM = "Zoom Page\nZoom to fit page in window (5)"


def _geometry():
    return (DisplayGeometry(display=Display(0, 1280, 800, 1.0, True), origin=(0.0, 0.0)),)


class _Button:
    def __init__(self, name: str, description: str = "", role: str = "radio button") -> None:
        self._name = name
        self._description = description
        self._role = role

    def get_role_name(self):
        return self._role

    def get_name(self):
        return self._name

    def get_description(self):
        return self._description


def _read(monkeypatch, name: str, description: str = "", role: str = "radio button") -> RawNode:
    monkeypatch.setattr(
        _atspi, "_state_flags", lambda acc: (True, False, None, False, None, True, False),
    )
    monkeypatch.setattr(
        _atspi, "_extents", lambda acc, keep_zero=False: ((4.0, 40.0), (28.0, 28.0)),
    )
    # A radio button's value read imports PyGObject. macOS and Windows CI have no gi.
    monkeypatch.setattr(_atspi, "_value_text", lambda *args, **kwargs: None)
    return _atspi.ATSPIAccessor().read(_Button(name, description, role))


def test_tool_name_hint_and_shortcut_are_separate_fields(monkeypatch) -> None:
    text = _read(monkeypatch, _TEXT)
    assert text.role == "AXRadioButton"
    assert text.title == "Text Tool"
    assert text.hint == "Create and edit text objects"
    assert text.shortcut == "T"
    assert text.description == "Create and edit text objects"
    assert "\n" not in text.title
    selected = _read(monkeypatch, _SELECT)
    assert selected.title == "Selector Tool"
    assert selected.hint == "Select and transform objects"
    assert selected.shortcut == "S"
    save = _read(monkeypatch, _SAVE, role="push button")
    assert save.role == "AXButton"
    assert save.title == "Save"
    assert save.hint == "Save document"
    assert save.shortcut == "Ctrl+S"
    zoom = _read(monkeypatch, _ZOOM, role="push button")
    assert zoom.title == "Zoom Page"
    assert zoom.hint == "Zoom to fit page in window"
    assert zoom.shortcut == "5"


def test_snapshot_shows_the_clean_name_and_find_still_sees_the_hint(monkeypatch) -> None:
    raw = _read(monkeypatch, _TEXT)
    window = (
        RawNode(role="AXWindow", title="Inkscape", position=(0.0, 0.0), size=(1280.0, 800.0)),
        [(raw, [])],
    )

    class _Accessor:
        def read(self, node):
            return node[0]

        def children(self, node):
            return node[1]

    snap = build_snapshot(
        window, _Accessor(), scope=Scope.WINDOW, app="inkscape", pid=1, geometry=_geometry(),
    )
    button = next(el for el in snap.elements if el.role == "AXRadioButton")
    assert button.title == "Text Tool"
    assert button.hint == "Create and edit text objects"
    assert button.shortcut == "T"
    rendered = observe.render_text(snap, mode="full")
    assert 'radiobutton "Text Tool"' in rendered
    assert 'hint="Create and edit text objects"' in rendered
    assert 'shortcut="T"' in rendered
    assert "Create and edit text objects (T)" not in rendered
    assert "\n" not in button.title
    assert observe.find_elements(snap, text="Text Tool") == (button,)
    assert observe.find_elements(snap, text="Create and edit text objects") == (button,)
    assert observe.find_elements(snap, text="(T)") == ()
    interactive = observe.render_text(snap, mode="interactive")
    assert 'radiobutton "Text Tool"' in interactive
    assert "Create and edit text objects (T)" not in interactive


def test_an_existing_description_is_kept_when_the_name_splits(monkeypatch) -> None:
    raw = _read(monkeypatch, _SAVE, description="kept", role="push button")
    assert raw.title == "Save"
    assert raw.description == "kept"
    assert raw.hint == "Save document"
    assert raw.shortcut == "Ctrl+S"


def test_a_description_only_tooltip_fills_the_name(monkeypatch) -> None:
    raw = _read(monkeypatch, "", description=_ZOOM, role="push button")
    assert raw.title == "Zoom Page"
    assert raw.hint == "Zoom to fit page in window"
    assert raw.shortcut == "5"
    assert "\n" not in raw.title


def test_a_sentence_without_a_chord_stays_whole(monkeypatch) -> None:
    blurb = "Align all visible layers of the image\nThis plug-in only works on RGB."
    raw = _read(monkeypatch, blurb, role="push button")
    assert raw.title == blurb
    assert raw.hint == ""
    assert raw.shortcut == ""
    one_line = _read(monkeypatch, "Save document (Ctrl+S)", role="push button")
    assert one_line.title == "Save document (Ctrl+S)"
    assert one_line.shortcut == ""
    long_paren = _read(
        monkeypatch,
        "Swatch\nClick this palette entry to make it the current color (last used values)",
        role="push button",
    )
    assert "\n" in long_paren.title
    assert long_paren.shortcut == ""


def test_modifier_hint_lines_are_still_the_gimp_split(monkeypatch) -> None:
    raw = _read(
        monkeypatch,
        "",
        description=(
            "Create a new layer and add it to the image\n"
            "Shift  Create new layers with last used values"
        ),
        role="push button",
    )
    assert raw.title == "Create a new layer and add it to the image"
    assert raw.description == "Shift  Create new layers with last used values"
    assert raw.hint == ""
    assert raw.shortcut == ""


def test_live_inkscape_tool_names_are_one_line(tmp_path) -> None:
    """Live Inkscape. Toolbox names do not contain the tooltip on a new line.

    A control whose name was that multiline pattern exposes ``hint`` and
    ``shortcut``. A build whose names are already one line has no newline
    in the button title. No click is sent.
    """
    if not sys.platform.startswith("linux"):
        import pytest

        pytest.skip("Linux backend")
    if shutil.which("inkscape") is None:
        import pytest

        pytest.skip("inkscape is not installed")
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        import pytest

        pytest.skip("no DISPLAY/WAYLAND_DISPLAY")
    import pytest

    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import ComputerUseError, ErrorCode

    driver = LinuxDriver()
    try:
        driver.ensure_trusted()
    except ComputerUseError as exc:
        pytest.skip(f"AT-SPI bus not reachable: {exc.message}")
    except ImportError as exc:
        pytest.skip(f"PyGObject/Atspi missing: {exc}")

    home = tmp_path / "home"
    config = home / ".config" / "inkscape"
    config.mkdir(parents=True)
    (config / "preferences.xml").write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"no\"?>\n"
        "<inkscape version=\"1\">\n"
        "  <group id=\"options\">\n"
        "    <group id=\"boot\" enabled=\"0\"/>\n"
        "  </group>\n"
        "</inkscape>\n",
        encoding="utf-8",
    )
    svg = tmp_path / "toolbar.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"/>',
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["HOME"] = str(home)
    env.setdefault("GTK_MODULES", "gail:atk-bridge")
    env.setdefault("NO_AT_BRIDGE", "0")
    proc = subprocess.Popen(
        ["inkscape", str(svg)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 40
        snap = None
        last = ""
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError(f"inkscape exited with status {proc.returncode}")
            try:
                shot = driver.snapshot(Scope.APP, "inkscape")
            except ComputerUseError as exc:
                if exc.code is not ErrorCode.APP_NOT_FOUND:
                    raise
                shot = None
            else:
                last = observe.render_text(shot)
                tools = [
                    el for el in shot.elements
                    if el.role in {"AXRadioButton", "AXButton"} and el.title
                ]
                if len(tools) >= 5:
                    snap = shot
                    break
            time.sleep(0.4)
        assert snap is not None, last[:1600]
        tools = [
            el for el in snap.elements
            if el.role in {"AXRadioButton", "AXButton", "AXMenuButton"}
        ]
        assert tools
        rendered = observe.render_text(snap, mode="interactive")
        for el in tools:
            assert "\n" not in el.title
            if el.hint:
                assert el.hint not in el.title
            if el.shortcut:
                assert f"({el.shortcut})" not in el.title
        assert "\nCreate and edit" not in rendered
        assert "\nSave document" not in rendered
        split = [el for el in tools if el.hint and el.shortcut]
        if split:
            sample = split[0]
            assert f'"{sample.title}"' in rendered
            assert f'hint="{sample.hint}"' in rendered or sample.hint[:40] in rendered
            assert f'shortcut="{sample.shortcut}"' in rendered
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
