"""#780: set_value on a Chrome ARIA slider/spinbutton.

Chrome ignores Value.set_current_value for ARIA range widgets; the value is
aria-valuenow, which only the page changes. set_value steps the widget with
its own increment/decrement actions, or focus plus arrow keys, reads the
value back after every step, and confirms only when it equals the target.
"""
import pytest

from a11y_computer_use.drivers import _atspi, _cdp_forms, _linux_input
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Bounds, ComputerUseError, Element, ErrorCode
from tests.test_linux_synthetic import _Acc, fake_atspi  # noqa: F401


class _Widget:
    """A page-driven ARIA range: arrow keys and actions move aria-valuenow."""

    def __init__(self, role, now, low, high, *, actions=(), readable=True, stuck=False):
        self.role, self.now, self.low, self.high = role, now, low, high
        self.actions = tuple(actions)
        self.readable = readable
        self.stuck = stuck
        self.focused = False
        self.keys: list[str] = []
        self.done: list[str] = []

    def move(self, delta):
        if not self.stuck:
            self.now = max(self.low, min(self.high, self.now + delta))

    def key(self, chord):
        self.keys.append(chord)
        if self.focused:
            self.move({"right": 1, "up": 1, "left": -1, "down": -1}[chord])

    def act(self, name):
        self.done.append(name)
        self.move(1 if name == "increment" else -1)
        return True


def _element(role):
    return Element(ref="e38", role=role, title="Volume", value=None,
                   snapshot_id="snap-780", bounds=Bounds(0, 10, 10, 300, 20))


@pytest.fixture
def drive(monkeypatch):
    def make(widget):
        driver = LinuxDriver.__new__(LinuxDriver)
        driver._run = lambda fn: fn()
        monkeypatch.setattr(_cdp_forms, "write_element", lambda *a, **k: None)
        monkeypatch.setattr(driver, "_dom_window_title", lambda node: "")
        monkeypatch.setattr(driver, "_dom_pids", lambda node: ())
        monkeypatch.setattr(_atspi, "_refuse_model_editor_replace", lambda node: None)
        monkeypatch.setattr(_atspi, "is_sheet_cell", lambda node: False)
        monkeypatch.setattr(_atspi, "qt_checkable_item", lambda node: False)
        monkeypatch.setattr(_atspi, "qt_item_view_cell", lambda node: False)
        monkeypatch.setattr(_atspi, "control_kind", lambda node: "value")

        def ignored_write(node, value):
            number = _atspi._parse_number(value)
            if not widget.low <= number <= widget.high:
                raise ValueError(f"value {value!r} is outside {widget.low}..{widget.high}")
            raise _atspi._text_mismatch("text_mismatch", "the value read back does not match",
                                        expected=value, actual=widget.now)

        monkeypatch.setattr(_atspi, "set_numeric_value", ignored_write)
        monkeypatch.setattr(_atspi, "chromium_aria_range", lambda node: True)
        monkeypatch.setattr(_atspi, "read_current_value",
                            lambda node: float(widget.now) if widget.readable else None)
        monkeypatch.setattr(_atspi, "value_step", lambda node: 1.0)
        monkeypatch.setattr(_atspi, "raw_action_names", lambda node: widget.actions)
        monkeypatch.setattr(_atspi, "do_named_action", lambda node, name: widget.act(name))
        monkeypatch.setattr(_atspi, "_role_name", lambda node: widget.role)

        def focus(node):
            widget.focused = True
            return True

        monkeypatch.setattr(_atspi, "grab_focus", focus)
        monkeypatch.setattr(_atspi, "_state_has", lambda node, s: s == "FOCUSED" and widget.focused)
        monkeypatch.setattr(_linux_input, "press_chord", widget.key)
        monkeypatch.setattr(LinuxDriver, "_ARIA_STEP_PAUSE_S", 0.0)
        return driver
    return make


def test_slider_is_stepped_with_arrow_keys_and_confirmed_by_read_back(drive):
    widget = _Widget("slider", 50, 0, 100)
    driver = drive(widget)
    assert driver._apply_set_value(_element("AXSlider"), "80", object()) is True
    assert widget.now == 80 and widget.keys == ["right"] * 30


def test_slider_steps_down_with_left(drive):
    widget = _Widget("slider", 20, 10, 30)
    assert drive(widget)._apply_set_value(_element("AXSlider"), "15", object()) is True
    assert widget.now == 15 and widget.keys == ["left"] * 5


def test_slider_with_increment_action_uses_it_and_sends_no_key(drive):
    widget = _Widget("slider", 20, 10, 30, actions=("increment", "decrement"))
    assert drive(widget)._apply_set_value(_element("AXSlider"), "25", object()) is True
    assert widget.now == 25 and widget.done == ["increment"] * 5 and widget.keys == []


def test_spinbutton_is_stepped_with_up_and_down(drive):
    widget = _Widget("spin button", 1, 0, 10)
    driver = drive(widget)
    assert driver._apply_set_value(_element("AXIncrementor"), "5", object()) is True
    assert widget.now == 5 and widget.keys == ["up"] * 4
    assert driver._apply_set_value(_element("AXIncrementor"), "0", object()) is True
    assert widget.now == 0 and widget.keys[4:] == ["down"] * 5


@pytest.mark.parametrize("value", ["150", "-1"])
def test_out_of_range_target_is_refused_before_any_step(drive, value):
    widget = _Widget("slider", 50, 0, 100)
    with pytest.raises(ValueError):
        drive(widget)._apply_set_value(_element("AXSlider"), value, object())
    assert widget.now == 50 and widget.keys == [] and widget.done == []


def test_out_of_range_is_refused_by_set_numeric_value_without_a_write(fake_atspi, monkeypatch):  # noqa: F811
    slider = _Acc("slider", name="Volume", value=50, minimum=0, maximum=100)
    monkeypatch.setattr(fake_atspi.Value, "set_current_value",
                        lambda *a: (_ for _ in ()).throw(AssertionError("no write")))
    with pytest.raises(ValueError) as exc:
        _atspi.set_numeric_value(slider, "150")
    assert "0..100" in str(exc.value) and slider.value == 50


def test_unreadable_value_is_refused_with_nothing_sent(drive):
    widget = _Widget("slider", 50, 0, 100, readable=False)
    with pytest.raises(ComputerUseError) as exc:
        drive(widget)._apply_set_value(_element("AXSlider"), "80", object())
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "value_unreadable" and exc.value.detail["sent"] == 0
    assert widget.keys == [] and widget.done == []


def test_value_that_stops_reading_back_mid_way_is_not_confirmed(drive, monkeypatch):
    widget = _Widget("slider", 50, 0, 100)
    driver = drive(widget)
    reads = {"n": 0}

    def flaky(node):
        reads["n"] += 1
        return float(widget.now) if reads["n"] == 1 else None

    monkeypatch.setattr(_atspi, "read_current_value", flaky)
    with pytest.raises(ComputerUseError) as exc:
        driver._apply_set_value(_element("AXSlider"), "80", object())
    assert exc.value.detail["reason"] == "value_unreadable" and exc.value.detail["sent"] == 1


def test_step_that_does_not_move_the_value_stops_and_reports(drive):
    widget = _Widget("slider", 50, 0, 100, stuck=True)
    with pytest.raises(ComputerUseError) as exc:
        drive(widget)._apply_set_value(_element("AXSlider"), "80", object())
    assert exc.value.detail["reason"] == "step_unchanged"
    assert exc.value.detail["actual"] == 50 and exc.value.detail["sent"] == 1


def test_overshoot_is_not_confirmed(drive):
    widget = _Widget("slider", 50, 0, 100)
    driver = drive(widget)
    widget.move = lambda delta: setattr(widget, "now", widget.now + 2 * delta)
    with pytest.raises(ComputerUseError) as exc:
        driver._apply_set_value(_element("AXSlider"), "51", object())
    assert exc.value.detail["reason"] == "overshoot" and exc.value.detail["actual"] == 52


def test_off_step_target_is_refused_with_nothing_sent(drive, monkeypatch):
    widget = _Widget("slider", 50, 0, 100)
    driver = drive(widget)
    monkeypatch.setattr(_atspi, "value_step", lambda node: 5.0)
    with pytest.raises(ComputerUseError) as exc:
        driver._apply_set_value(_element("AXSlider"), "52", object())
    assert exc.value.detail["reason"] == "off_step" and widget.keys == []


def test_non_aria_mismatch_keeps_the_old_refusal(drive, monkeypatch):
    widget = _Widget("slider", 50, 0, 100)
    driver = drive(widget)
    monkeypatch.setattr(_atspi, "chromium_aria_range", lambda node: False)
    with pytest.raises(ComputerUseError) as exc:
        driver._apply_set_value(_element("AXSlider"), "80", object())
    assert exc.value.detail["reason"] == "text_mismatch" and widget.keys == []


def test_chromium_aria_range_is_only_chrome_slider_and_spinbutton(monkeypatch):
    monkeypatch.setattr(_atspi, "_chromium_app", lambda node: node["chrome"])
    monkeypatch.setattr(_atspi, "_role_name", lambda node: node["role"])
    monkeypatch.setattr(_atspi, "_number_input", lambda node: node.get("input", False))
    assert _atspi.chromium_aria_range({"chrome": True, "role": "slider"})
    assert _atspi.chromium_aria_range({"chrome": True, "role": "spin button"})
    assert not _atspi.chromium_aria_range({"chrome": False, "role": "slider"})
    assert not _atspi.chromium_aria_range({"chrome": True, "role": "push button"})
    assert not _atspi.chromium_aria_range({"chrome": True, "role": "spin button", "input": True})
