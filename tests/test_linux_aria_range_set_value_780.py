"""#780: set_value on a Chrome ARIA slider/spinbutton.

Chrome ignores Value.set_current_value for ARIA range widgets; the value is
aria-valuenow, which only the page changes. set_value steps the widget with
its own increment/decrement actions, or focus plus arrow keys, reads the
value back after every step, and confirms only when it equals the target.
A page whose real step is larger than AT-SPI minimum_increment (#1484)
learns that step from the first move, restores on overshoot, and never
returns refused while the live value differs from the start.
"""
import pytest

from a11y_computer_use.drivers import _atspi, _cdp_forms, _linux_input
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Bounds, ComputerUseError, Element, ErrorCode
from tests.test_linux_synthetic import _Acc, fake_atspi  # noqa: F401


class _Widget:
    """A page-driven ARIA range: arrow keys and actions move aria-valuenow.

    ``page_step`` is the page's real step (often 5 while AT-SPI reports 1).
    """

    def __init__(self, role, now, low, high, *, actions=(), readable=True, stuck=False,
                 page_step=1, reverse_stuck=False):
        self.role, self.now, self.low, self.high = role, now, low, high
        self.actions = tuple(actions)
        self.readable = readable
        self.stuck = stuck
        self.page_step = page_step
        self.reverse_stuck = reverse_stuck
        self.focused = False
        self.keys: list[str] = []
        self.done: list[str] = []
        self._forward_chord = None
        self._forward_action = None

    def move(self, delta, *, reverse=False):
        if self.stuck or (reverse and self.reverse_stuck):
            return
        self.now = max(self.low, min(self.high, self.now + delta * self.page_step))

    def key(self, chord):
        self.keys.append(chord)
        if not self.focused:
            return
        if self._forward_chord is None:
            self._forward_chord = chord
        reverse = chord != self._forward_chord
        delta = {"right": 1, "up": 1, "left": -1, "down": -1}[chord]
        self.move(delta, reverse=reverse)

    def act(self, name):
        self.done.append(name)
        if self._forward_action is None:
            self._forward_action = name
        reverse = name != self._forward_action
        delta = 1 if name == "increment" else -1
        self.move(delta, reverse=reverse)
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
        monkeypatch.setattr(_atspi, "_drop_cached_properties", lambda node: None)
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
    """A 2x step past the target is restored, then refused with the start value."""
    widget = _Widget("slider", 50, 0, 100)

    def double(delta, *, reverse=False):
        if reverse and widget.reverse_stuck:
            return
        widget.now = max(widget.low, min(widget.high, widget.now + 2 * delta))

    widget.move = double  # type: ignore[method-assign]
    with pytest.raises(ComputerUseError) as exc:
        drive(widget)._apply_set_value(_element("AXSlider"), "51", object())
    detail = exc.value.detail
    assert detail["reason"] == "overshoot"
    assert detail["actual"] == 50 and detail.get("restored") is True
    assert widget.now == 50 and detail["sent"] == 2
    assert detail["nearest"] == [50, 52]


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


# --- #1484: page step 5 while AT-SPI reports minimum_increment 1 ---------------


def test_page_step5_off_step_target_restores_start_and_names_nearest(drive):
    """Tester #1484: 45→42 overshoots to 40; restore to 45 before refusing."""
    widget = _Widget("slider", 45, 0, 100, page_step=5)
    with pytest.raises(ComputerUseError) as exc:
        drive(widget)._apply_set_value(_element("AXSlider"), "42", object())
    detail = exc.value.detail
    assert detail["reason"] == "overshoot"
    assert detail["actual"] == 45 and detail.get("restored") is True
    assert detail["step"] == 5 and detail["nearest"] == [40, 45]
    assert detail["sent"] == 2 and widget.now == 45
    assert widget.keys == ["left", "right"]
    assert detail.get("outcome") != "unverifiable"


def test_page_step5_upward_off_step_restores_start(drive):
    """Tester #1484: 40→43 overshoots to 45; restore to 40 before refusing."""
    widget = _Widget("slider", 40, 0, 100, page_step=5)
    with pytest.raises(ComputerUseError) as exc:
        drive(widget)._apply_set_value(_element("AXSlider"), "43", object())
    detail = exc.value.detail
    assert detail["reason"] == "overshoot"
    assert detail["actual"] == 40 and detail.get("restored") is True
    assert detail["nearest"] == [40, 45] and widget.now == 40
    assert widget.keys == ["right", "left"]


def test_page_step5_on_step_target_is_confirmed_with_learned_step(drive):
    widget = _Widget("slider", 50, 0, 100, page_step=5)
    assert drive(widget)._apply_set_value(_element("AXSlider"), "40", object()) is True
    assert widget.now == 40 and widget.keys == ["left", "left"]


def test_page_step5_stops_before_a_second_step_that_would_pass(drive):
    """50→42: first key lands on 45; target is off-grid; restore, do not go to 40."""
    widget = _Widget("slider", 50, 0, 100, page_step=5)
    with pytest.raises(ComputerUseError) as exc:
        drive(widget)._apply_set_value(_element("AXSlider"), "42", object())
    detail = exc.value.detail
    assert detail["reason"] == "off_step"
    assert detail["actual"] == 50 and detail.get("restored") is True
    assert detail["step"] == 5 and detail["nearest"] == [40, 45]
    assert widget.now == 50 and widget.keys == ["left", "right"]


def test_ignored_restore_key_is_unverifiable_not_refused(drive):
    """If the reverse key does nothing, the live value changed: not refused."""
    widget = _Widget("slider", 45, 0, 100, page_step=5, reverse_stuck=True)
    with pytest.raises(ComputerUseError) as exc:
        drive(widget)._apply_set_value(_element("AXSlider"), "42", object())
    detail = exc.value.detail
    assert detail.get("outcome") == "unverifiable"
    assert detail.get("next") == []
    assert detail["actual"] == 40 and detail.get("restored") is False
    assert widget.now == 40
    assert "now holds" in exc.value.message and "40" in exc.value.message


def test_refused_implies_value_unchanged_or_restored(drive):
    """Invariant: a refused ARIA step never leaves a wrong live value."""
    cases = [
        _Widget("slider", 45, 0, 100, page_step=5),
        _Widget("slider", 40, 0, 100, page_step=5),
        _Widget("slider", 50, 0, 100, page_step=5),
        _Widget("spin button", 0, 0, 100, page_step=10),
    ]
    targets = ["42", "43", "42", "15"]
    for widget, target in zip(cases, targets):
        start = widget.now
        with pytest.raises(ComputerUseError) as exc:
            drive(widget)._apply_set_value(
                _element("AXSlider" if widget.role == "slider" else "AXIncrementor"),
                target, object())
        detail = exc.value.detail
        if detail.get("outcome") == "unverifiable":
            assert widget.now != start
            continue
        assert detail.get("reason") in {"overshoot", "off_step", "step_unchanged",
                                        "value_unreadable", "focus_lost", "too_many_steps"}
        assert widget.now == start
        assert detail["actual"] == start


def test_spinbutton_page_step10_off_step_restores(drive):
    widget = _Widget("spin button", 20, 0, 100, page_step=10)
    with pytest.raises(ComputerUseError) as exc:
        drive(widget)._apply_set_value(_element("AXIncrementor"), "25", object())
    detail = exc.value.detail
    assert detail["reason"] == "overshoot"
    assert detail["actual"] == 20 and widget.now == 20 and detail.get("restored") is True
    assert detail["nearest"] == [20, 30]


def test_page_step5_at_min_boundary_clamped_overshoot_restores(drive):
    widget = _Widget("slider", 5, 0, 100, page_step=5)
    with pytest.raises(ComputerUseError) as exc:
        drive(widget)._apply_set_value(_element("AXSlider"), "2", object())
    detail = exc.value.detail
    assert detail.get("restored") is True and widget.now == 5
    assert detail["actual"] == 5


# --- Tester's #1477 gate: the first spinbutton call on a fresh page ------


def _lagging_focus(monkeypatch, widget, *, ignored_grabs=0, lag_reads=0):
    """Chrome's shapes: a grab_focus the document keeps, then FOCUSED that
    reads false for a few fresh reads before the node reports it."""
    state = {"grabs": 0, "reads": 0, "dropped": 0}

    def focus(node):
        state["grabs"] += 1
        if state["grabs"] > ignored_grabs:
            widget.focused = True
        return True

    def has(node, name):
        if name != "FOCUSED" or not widget.focused:
            return False
        state["reads"] += 1
        return state["reads"] > lag_reads

    monkeypatch.setattr(_atspi, "grab_focus", focus)
    monkeypatch.setattr(_atspi, "_state_has", has)
    monkeypatch.setattr(_atspi, "_drop_cached_properties",
                        lambda node: state.__setitem__("dropped", state["dropped"] + 1))
    return state


def test_first_spinbutton_call_waits_for_focus_to_arrive(drive, monkeypatch):
    widget = _Widget("spin button", 1, 0, 10)
    driver = drive(widget)
    state = _lagging_focus(monkeypatch, widget, lag_reads=3)
    assert driver._apply_set_value(_element("AXSpinButton"), "5", object()) is True
    assert widget.now == 5 and widget.keys == ["up"] * 4
    assert state["dropped"] >= 4  # FOCUSED is read fresh, not from Chrome's cache


def test_first_grab_focus_kept_by_the_document_is_retried(drive, monkeypatch):
    widget = _Widget("spin button", 1, 0, 10)
    driver = drive(widget)
    state = _lagging_focus(monkeypatch, widget, ignored_grabs=1)
    assert driver._apply_set_value(_element("AXSpinButton"), "5", object()) is True
    assert widget.now == 5 and state["grabs"] == 2


def test_spinbutton_that_never_takes_focus_sends_no_key(drive, monkeypatch):
    widget = _Widget("spin button", 1, 0, 10)
    driver = drive(widget)
    state = _lagging_focus(monkeypatch, widget, ignored_grabs=99)
    with pytest.raises(ComputerUseError) as exc:
        driver._apply_set_value(_element("AXSpinButton"), "5", object())
    assert exc.value.detail["reason"] == "focus_lost" and exc.value.detail["sent"] == 0
    assert widget.keys == [] and widget.now == 1
    assert state["grabs"] == LinuxDriver._ARIA_FOCUS_TRIES


# --- the spinbutton read-back carries its range: "3 (min 0, max 10)" ------


@pytest.mark.parametrize("requested, readback", [
    ("3", "3 (min 0, max 10)"), ("10", "10 (min 0, max 10)"), ("0", "0 (min 0, max 10)"),
    ("2.5", "2.5 (min 0, max 10)"),
])
def test_ranged_read_back_of_the_request_is_confirmed(requested, readback):
    from a11y_computer_use import outcome

    assert outcome.judge(changed=True, requested=requested, readback=readback,
                         before_value="1 (min 0, max 10)")[0] == "confirmed"


@pytest.mark.parametrize("readback", [
    "4 (min 0, max 10)", "3 (min 0)", "3 apples (min 0, max 10)", "13 (min 0, max 10)",
])
def test_ranged_read_back_of_another_value_is_not_confirmed(readback):
    from a11y_computer_use import outcome

    assert outcome.judge(changed=True, requested="3", readback=readback,
                         before_value="1 (min 0, max 10)")[0] != "confirmed"
