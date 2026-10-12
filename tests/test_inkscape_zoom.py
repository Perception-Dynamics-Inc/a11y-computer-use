"""Inkscape's zoom spin shows a percent and stores log2 of that percent.

Hermetic. No display, no bus, no Inkscape process. ``50%`` and a bare
number outside the Value range become ``log2(percent / 100)``. A bare
number inside that range stays on the Value scale, including the step
snap that turns ``0`` into a small negative log. A percent the control
cannot represent names both ranges. Nothing here types or clicks.

The live test at the bottom runs only on Linux with Inkscape and a
display. It skips when the status zoom is not this log-percent pair.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import time

import pytest

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Bounds, ComputerUseError, Element, ErrorCode


_ZOOM_MIN = math.log(0.01) / math.log(2.0)
_ZOOM_MAX = 8.0


class _States:
    def __init__(self, names):
        self.names = set(names)

    def contains(self, member):
        return str(member) in self.names


class _Spin:
    def __init__(self, text, value, minimum, maximum, increment=None):
        self.role = "spin button"
        self.name = "Zoom"
        self.text = text
        self.value = value
        self.minimum = minimum
        self.maximum = maximum
        self.increment = increment
        self.states = {"SHOWING", "VISIBLE", "ENABLED", "SENSITIVE", "EDITABLE"}
        self.children = []
        self.parent = None
        self.attrs = {}
        self.value_sets: list[float] = []
        self.hold_value = False
        self.snap_on_set = False

    def get_role_name(self):
        return self.role

    def get_name(self):
        return self.name

    def get_description(self):
        return ""

    def get_toolkit_name(self):
        return ""

    def get_state_set(self):
        return _States(self.states)

    def get_parent(self):
        return self.parent

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]

    def get_attributes(self):
        return dict(self.attrs)

    def clear_cache(self):
        return None


class _Atspi:
    class Text:
        @staticmethod
        def get_character_count(acc):
            if acc.text is None:
                raise RuntimeError("no text")
            return len(acc.text)

        @staticmethod
        def get_text(acc, start, end):
            if acc.text is None:
                raise RuntimeError("no text")
            if end is not None and int(end) < 0:
                return acc.text
            return acc.text[int(start):int(end)]

    class Value:
        @staticmethod
        def get_current_value(acc):
            if acc.value is None:
                raise RuntimeError("no value")
            return acc.value

        @staticmethod
        def get_minimum_value(acc):
            return acc.minimum

        @staticmethod
        def get_maximum_value(acc):
            return acc.maximum

        @staticmethod
        def get_minimum_increment(acc):
            if acc.increment is None:
                raise RuntimeError("no increment")
            return acc.increment

        @staticmethod
        def set_current_value(acc, new):
            acc.value_sets.append(float(new))
            if acc.hold_value:
                return True
            number = float(new)
            if acc.snap_on_set and acc.increment:
                step = float(acc.increment)
                low = float(acc.minimum)
                number = low + round((number - low) / step) * step
                number = float(format(number, ".10g"))
            upper = acc.maximum if acc.maximum is not None else number
            lower = acc.minimum if acc.minimum is not None else number
            acc.value = min(max(number, lower), upper)
            return True


def _element():
    return Element(
        "e219", "AXTextField", "Zoom", None, Bounds(0, 40, 760, 64, 22), "snap-zoom",
        editable=True, enabled=True,
    )


def _driver(monkeypatch, spin: _Spin) -> LinuxDriver:
    monkeypatch.setattr(_atspi, "_atspi", lambda: _Atspi)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: spin)

    def forbid(*_args, **_kwargs):
        raise AssertionError("set_value typed or clicked")

    monkeypatch.setattr(_atspi, "_type_string", forbid)
    monkeypatch.setattr(_atspi, "_type_into_target", forbid)
    monkeypatch.setattr(_atspi, "_click_center", forbid)
    monkeypatch.setattr(_atspi, "_chord_into_target", forbid)
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    return driver


def _zoom(text="  88%", *, increment=0.1, value=None) -> _Spin:
    if value is None:
        shown = float(str(text).replace("%", "").strip())
        value = math.log(shown / 100.0) / math.log(2.0)
    return _Spin(text, value, _ZOOM_MIN, _ZOOM_MAX, increment)


def test_percent_literal_converts_to_log2_without_snapping(monkeypatch) -> None:
    spin = _zoom()
    driver = _driver(monkeypatch, spin)
    assert driver.set_value(_element(), "50%") is True
    assert spin.value_sets == [math.log(0.5) / math.log(2.0)]
    assert spin.value == -1.0

    spaced = _zoom()
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: spaced)
    assert driver.set_value(_element(), "50 %") is True
    assert spaced.value == -1.0

    nbsp = _zoom()
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: nbsp)
    assert driver.set_value(_element(), "50\u00a0%") is True
    assert nbsp.value == -1.0

    full = _zoom()
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: full)
    assert driver.set_value(_element(), "100%") is True
    assert full.value == 0.0

    upper = _zoom()
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: upper)
    assert driver.set_value(_element(), "25600%") is True
    assert upper.value == 8.0

    lower = _zoom()
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: lower)
    assert driver.set_value(_element(), "1%") is True
    assert lower.value == pytest.approx(_ZOOM_MIN)


def test_bare_number_outside_the_log_range_is_a_percent(monkeypatch) -> None:
    spin = _zoom()
    driver = _driver(monkeypatch, spin)
    assert driver.set_value(_element(), "50") is True
    assert spin.value == -1.0
    assert spin.value_sets == [-1.0]

    hundred = _zoom()
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: hundred)
    assert driver.set_value(_element(), "100") is True
    assert hundred.value == 0.0

    nine = _zoom()
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: nine)
    assert driver.set_value(_element(), "9") is True
    assert nine.value == pytest.approx(math.log(0.09) / math.log(2.0))


def test_bare_number_inside_the_log_range_stays_on_that_scale(monkeypatch) -> None:
    """``0`` and ``8`` are inside -6.64..8, so they are not percents.

    The step snap is the one already applied to every GTK spin. ``0``
    becomes the small negative log the issue records, not ``0%``.
    """
    zero = _zoom()
    driver = _driver(monkeypatch, zero)
    assert driver.set_value(_element(), "0") == "-0.0438562"
    assert zero.value == pytest.approx(-0.04385618977)
    assert zero.value != 0.0

    eight = _zoom()
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: eight)
    held = driver.set_value(_element(), "8")
    assert held == "7.95614"
    assert eight.value == pytest.approx(7.95614381)
    assert eight.value > 1


def test_percent_outside_one_to_25600_names_both_ranges(monkeypatch) -> None:
    spin = _zoom()
    driver = _driver(monkeypatch, spin)
    with pytest.raises(ValueError) as exc:
        driver.set_value(_element(), "0%")
    message = str(exc.value)
    assert message == (
        "value '0%' is outside 1%..25600% (value scale -6.64386..8)"
    )
    assert spin.value_sets == []

    with pytest.raises(ValueError) as exc:
        driver.set_value(_element(), "30000%")
    assert "1%..25600%" in str(exc.value)
    assert "-6.64386..8" in str(exc.value)
    assert spin.value_sets == []


def test_a_word_on_the_zoom_spin_is_still_not_a_number(monkeypatch) -> None:
    spin = _zoom()
    driver = _driver(monkeypatch, spin)
    with pytest.raises(ValueError) as exc:
        driver.set_value(_element(), "abc")
    assert "not a number" in str(exc.value)
    assert "-6.64386..8" in str(exc.value)
    assert spin.value_sets == []


def test_a_held_zoom_value_is_a_mismatch(monkeypatch) -> None:
    spin = _zoom()
    spin.hold_value = True
    driver = _driver(monkeypatch, spin)
    with pytest.raises(ComputerUseError) as exc:
        driver.set_value(_element(), "50%")
    assert exc.value.detail["reason"] == "text_mismatch"
    assert spin.value == pytest.approx(math.log(0.88) / math.log(2.0))
    assert spin.value_sets == [-1.0]


def test_a_toolkit_step_reports_the_percent_it_held(monkeypatch) -> None:
    spin = _zoom()
    spin.snap_on_set = True
    driver = _driver(monkeypatch, spin)
    held = driver.set_value(_element(), "50%")
    assert spin.value_sets == [-1.0]
    assert isinstance(held, str) and held.endswith("%")
    assert held != "50%"
    assert float(held[:-1]) == pytest.approx((2.0 ** spin.value) * 100.0, abs=0.02)


def test_an_ordinary_spin_does_not_treat_numbers_as_percents(monkeypatch) -> None:
    spin = _Spin("3", 3.0, 0.0, 10.0, 1)
    driver = _driver(monkeypatch, spin)
    with pytest.raises(ValueError) as exc:
        driver.set_value(_element(), "50%")
    assert "not a number" in str(exc.value)
    assert "0..10" in str(exc.value)
    assert spin.value_sets == []
    with pytest.raises(ValueError) as exc:
        driver.set_value(_element(), "15")
    assert "outside" in str(exc.value)
    assert driver.set_value(_element(), "7") is True
    assert spin.value == 7.0


def test_a_percent_valued_spin_strips_the_sign_and_still_snaps(monkeypatch) -> None:
    spin = _Spin("88%", 88.0, 1.0, 400.0, 1)
    driver = _driver(monkeypatch, spin)
    assert driver.set_value(_element(), "50%") is True
    assert spin.value == 50.0
    stepped = _Spin("50%", 50.0, 1.0, 400.0, 1)
    monkeypatch.setattr(observe, "ax_handle_for", lambda *_args: stepped)
    assert driver.set_value(_element(), "50.6%") == "51"
    assert stepped.value == 51.0


def test_a_percent_label_that_matches_neither_scale_is_not_converted(monkeypatch) -> None:
    spin = _Spin("88%", 3.0, 0.0, 10.0, 1)
    driver = _driver(monkeypatch, spin)
    with pytest.raises(ValueError) as exc:
        driver.set_value(_element(), "50%")
    message = str(exc.value)
    assert "percent" in message
    assert "88%" in message
    assert "0..10" in message
    assert spin.value_sets == []
    assert driver.set_value(_element(), "4") is True
    assert spin.value == 4.0


def test_live_inkscape_zoom_accepts_a_percent(tmp_path) -> None:
    """Live Inkscape. ``50%`` lands on the log-scale zoom, and ``0%`` does not.

    The status control shows a percent while its Value is log2 of that
    percent. A build that does not expose that pair is skipped. No click
    and no keystroke is sent; ``set_value`` uses the Value interface.
    """
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux backend")
    if shutil.which("inkscape") is None:
        pytest.skip("inkscape is not installed")
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        pytest.skip("no DISPLAY/WAYLAND_DISPLAY")

    from a11y_computer_use.schema import Scope

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
    svg = tmp_path / "zoom.svg"
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
        zoom = None
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
                for el in shot.elements:
                    if not isinstance(el.value, str):
                        continue
                    handle = observe.ax_handle_for(shot.snapshot_id, el.ref)
                    if handle is None:
                        continue
                    try:
                        unit = driver._run(lambda node=handle: _atspi._percent_value_unit(node))
                    except Exception:
                        unit = None
                    if unit is not None and unit[0] == "log2":
                        zoom = el
                        break
                if zoom is not None:
                    break
            time.sleep(0.4)
        if zoom is None:
            pytest.skip("inkscape did not expose a log-scale percent zoom: " + last[:500])

        landed = driver.set_value(zoom, "50%")
        shot = driver.snapshot(Scope.APP, "inkscape")
        shown = _shown_log_percent(driver, shot)
        assert shown is not None, observe.render_text(shot)[:1600]
        if landed is True:
            assert shown == pytest.approx(50.0, abs=1.0)
        else:
            assert isinstance(landed, str) and landed.endswith("%")
            assert float(landed[:-1]) == pytest.approx(shown, abs=1.0)
            assert abs(shown - 50.0) <= 5.0

        bare = _log_zoom_element(driver, shot)
        assert bare is not None
        landed_bare = driver.set_value(bare, "100")
        shot = driver.snapshot(Scope.APP, "inkscape")
        shown = _shown_log_percent(driver, shot)
        assert shown is not None
        if landed_bare is True:
            assert shown == pytest.approx(100.0, abs=1.0)
        else:
            assert isinstance(landed_bare, str) and landed_bare.endswith("%")
            assert float(landed_bare[:-1]) == pytest.approx(shown, abs=1.0)

        refused = _log_zoom_element(driver, shot)
        assert refused is not None
        with pytest.raises(ValueError) as exc:
            driver.set_value(refused, "0%")
        message = str(exc.value)
        assert "1%" in message and "25600%" in message
        assert "value scale" in message
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _shown_log_percent(driver, shot):
    element = _log_zoom_element(driver, shot)
    if element is None or not isinstance(element.value, str):
        return None
    text = element.value.replace("\u00a0", " ").replace("\u202f", " ")
    number = _atspi._percent_literal(text)
    return number


def _log_zoom_element(driver, shot):
    for el in shot.elements:
        if not isinstance(el.value, str):
            continue
        handle = observe.ax_handle_for(shot.snapshot_id, el.ref)
        if handle is None:
            continue
        try:
            unit = driver._run(lambda node=handle: _atspi._percent_value_unit(node))
        except Exception:
            continue
        if unit is not None and unit[0] == "log2":
            return el
    return None
