"""Unit tests for computeruse.act.

Every test uses ``dry_run=True`` (events are built, never posted) except the
single `HAS_AX`-guarded live test, which posts one mouse-move to the cursor's
*current* position — deliberately zero-impact. Coordinate mapping is
monkeypatched to identity (`flat_display`) so assertions are exact and
independent of this machine's display geometry.
"""

from __future__ import annotations

import dataclasses

import pytest
import Quartz

from computeruse import act
from computeruse.schema import (
    MODIFIER_KEYS,
    Bounds,
    Click,
    ComputerUseError,
    Drag,
    ErrorCode,
    KeyChord,
    MouseButton,
    Point,
    Scroll,
    ScrollUnit,
    TypeText,
    WaitCondition,
    WaitFor,
)
from tests.conftest import HAS_AX

_ALL_MODIFIER_MASK = (
    Quartz.kCGEventFlagMaskCommand
    | Quartz.kCGEventFlagMaskControl
    | Quartz.kCGEventFlagMaskAlternate
    | Quartz.kCGEventFlagMaskShift
    | Quartz.kCGEventFlagMaskSecondaryFn
)

POINT = Point(display_id=1, x=100, y=200)


def loc(built: act.BuiltEvent) -> tuple[float, float]:
    p = Quartz.CGEventGetLocation(built.event)
    return (p.x, p.y)


def etype(built: act.BuiltEvent) -> int:
    return Quartz.CGEventGetType(built.event)


def click_state(built: act.BuiltEvent) -> int:
    return Quartz.CGEventGetIntegerValueField(built.event, Quartz.kCGMouseEventClickState)


def mod_flags(built: act.BuiltEvent) -> int:
    return Quartz.CGEventGetFlags(built.event) & _ALL_MODIFIER_MASK


def keycode(built: act.BuiltEvent) -> int:
    return Quartz.CGEventGetIntegerValueField(built.event, Quartz.kCGKeyboardEventKeycode)


def event_text(built: act.BuiltEvent) -> str:
    _, s = Quartz.CGEventKeyboardGetUnicodeString(built.event, 512, None, None)
    return s


class FakePasteboard:
    """Recording pasteboard double; ``ops`` captures the call sequence."""

    def __init__(self, initial: str | None = None) -> None:
        self.content = initial
        self.ops: list[tuple] = []
        self.changes = 0

    def save(self) -> str | None:
        self.ops.append(("save", self.content))
        return self.content

    def write_text(self, text: str) -> None:
        self.ops.append(("write", text))
        self.content = text
        self.changes += 1

    def restore(self, saved: str | None) -> None:
        self.ops.append(("restore", saved))
        self.content = saved
        self.changes += 1

    def change_count(self) -> int:
        return self.changes


@pytest.fixture(autouse=True)
def _reset_integration_slots():
    """Leave the module-level resolver/checker slots clean after each test."""
    yield
    act.set_resolver(None)
    act.set_wait_checker(None)


@pytest.fixture(autouse=True)
def _no_secure_field(monkeypatch):
    """Pin both secure-field probes to False.

    They read live machine state (whether *anything* on this machine holds
    secure event input / focuses a password field right now), so unpatched
    they make every type_text test flake; the secure paths have dedicated
    tests that re-patch them."""
    monkeypatch.setattr(act, "_secure_input_enabled", lambda: False)
    monkeypatch.setattr(act, "_focused_element_secure", lambda: False)


@pytest.fixture
def flat_display(monkeypatch):
    """Identity coordinate mapping: physical pixels == global points."""
    monkeypatch.setattr(act, "_point_to_global", lambda p: (float(p.x), float(p.y)))


# --- click -------------------------------------------------------------------


class TestClick:
    def test_single_left_click_event_sequence(self, flat_display):
        events = act.click(POINT, dry_run=True)
        assert [e.kind for e in events] == ["mouse_move", "mouse_down", "mouse_up"]
        assert [etype(e) for e in events] == [
            Quartz.kCGEventMouseMoved,
            Quartz.kCGEventLeftMouseDown,
            Quartz.kCGEventLeftMouseUp,
        ]
        assert all(loc(e) == (100.0, 200.0) for e in events)
        assert click_state(events[1]) == 1
        assert click_state(events[2]) == 1

    def test_double_click_increments_click_state_per_pair(self, flat_display):
        # The canonical Quartz progression: pair 1 -> state 1, pair 2 -> 2.
        # Stamping 2 on both pairs makes AppKit fire doubleAction twice.
        events = act.click(POINT, count=2, dry_run=True)
        assert [e.kind for e in events] == [
            "mouse_move", "mouse_down", "mouse_up", "mouse_down", "mouse_up",
        ]
        assert [click_state(e) for e in events[1:]] == [1, 1, 2, 2]

    def test_triple_click(self, flat_display):
        events = act.click(POINT, count=3, dry_run=True)
        assert [e.kind for e in events[1:]] == ["mouse_down", "mouse_up"] * 3
        assert [click_state(e) for e in events[1:]] == [1, 1, 2, 2, 3, 3]

    def test_right_click_event_types(self, flat_display):
        events = act.click(POINT, button=MouseButton.RIGHT, dry_run=True)
        assert etype(events[1]) == Quartz.kCGEventRightMouseDown
        assert etype(events[2]) == Quartz.kCGEventRightMouseUp

    def test_middle_click_uses_other_button_number(self, flat_display):
        events = act.click(POINT, button=MouseButton.MIDDLE, dry_run=True)
        assert etype(events[1]) == Quartz.kCGEventOtherMouseDown
        button_number = Quartz.CGEventGetIntegerValueField(
            events[1].event, Quartz.kCGMouseEventButtonNumber
        )
        assert button_number == Quartz.kCGMouseButtonCenter

    def test_modifier_flags_applied_to_all_events(self, flat_display):
        events = act.click(POINT, modifiers=("cmd", "shift"), dry_run=True)
        expected = Quartz.kCGEventFlagMaskCommand | Quartz.kCGEventFlagMaskShift
        assert all(mod_flags(e) == expected for e in events)

    def test_unknown_modifier_rejected(self, flat_display):
        with pytest.raises(ValueError, match="unknown modifier 'super'"):
            act.click(POINT, modifiers=("super",), dry_run=True)

    @pytest.mark.parametrize("count", [0, 4, -1])
    def test_invalid_count_rejected(self, flat_display, count):
        with pytest.raises(ValueError, match="count must be 1, 2 or 3"):
            act.click(POINT, count=count, dry_run=True)

    def test_element_target_clicks_bounds_center(self, flat_display, synthetic_snapshot):
        button = synthetic_snapshot.element("e2")  # Bounds(1, 240, 140, 120, 56)
        events = act.click(button, dry_run=True)
        assert loc(events[1]) == (300.0, 168.0)

    def test_secure_element_raises_secure_field(self, flat_display, synthetic_snapshot):
        secure = synthetic_snapshot.element("e4")
        with pytest.raises(ComputerUseError) as excinfo:
            act.click(secure, dry_run=True)
        assert excinfo.value.code is ErrorCode.SECURE_FIELD
        assert excinfo.value.detail["ref"] == "e4"

    def test_pre_check_receives_click_action(self, flat_display):
        seen: list = []
        act.click(
            POINT,
            button=MouseButton.RIGHT,
            count=2,
            modifiers=("cmd",),
            pre_check=seen.append,
            dry_run=True,
        )
        [action] = seen
        assert isinstance(action, Click)
        assert action.target is POINT
        assert action.button is MouseButton.RIGHT
        assert action.count == 2
        assert action.modifiers == ("cmd",)

    def test_pre_check_veto_aborts(self, flat_display):
        def veto(action):
            raise ComputerUseError(ErrorCode.SECURE_FIELD, "denied by policy")

        with pytest.raises(ComputerUseError, match="denied by policy"):
            act.click(POINT, pre_check=veto, dry_run=True)


# --- drag --------------------------------------------------------------------


class TestDrag:
    def test_drag_interpolates_and_releases_at_end(self, flat_display):
        start, end = Point(1, 0, 0), Point(1, 100, 0)
        events = act.drag(start, end, dry_run=True)
        # 100pt / DRAG_STEP_PT(40) -> ceil = 3 interpolated moves.
        assert [e.kind for e in events] == [
            "mouse_move", "mouse_down", "mouse_drag", "mouse_drag", "mouse_drag", "mouse_up",
        ]
        assert loc(events[0]) == (0.0, 0.0)
        assert loc(events[1]) == (0.0, 0.0)
        xs = [loc(e)[0] for e in events[2:5]]
        assert xs == pytest.approx([100 / 3, 200 / 3, 100.0])
        assert xs == sorted(xs)
        assert loc(events[-1]) == (100.0, 0.0)
        assert etype(events[2]) == Quartz.kCGEventLeftMouseDragged
        assert etype(events[-1]) == Quartz.kCGEventLeftMouseUp

    def test_short_drag_has_minimum_two_steps(self, flat_display):
        events = act.drag(Point(1, 0, 0), Point(1, 10, 0), dry_run=True)
        assert [e.kind for e in events].count("mouse_drag") == 2

    def test_drag_pre_check_receives_drag_action(self, flat_display):
        seen: list = []
        act.drag(Point(1, 0, 0), Point(1, 5, 5), pre_check=seen.append, dry_run=True)
        [action] = seen
        assert isinstance(action, Drag)
        assert action.button is MouseButton.LEFT


# --- scroll ------------------------------------------------------------------


class TestScroll:
    def test_line_scroll_negates_schema_deltas(self, flat_display):
        events = act.scroll(POINT, dx=1, dy=3, dry_run=True)
        assert [e.kind for e in events] == ["mouse_move", "scroll"]
        wheel = events[1]
        assert etype(wheel) == Quartz.kCGEventScrollWheel
        assert Quartz.CGEventGetIntegerValueField(
            wheel.event, Quartz.kCGScrollWheelEventDeltaAxis1
        ) == -3
        assert Quartz.CGEventGetIntegerValueField(
            wheel.event, Quartz.kCGScrollWheelEventDeltaAxis2
        ) == -1
        assert Quartz.CGEventGetIntegerValueField(
            wheel.event, Quartz.kCGScrollWheelEventIsContinuous
        ) == 0
        assert loc(wheel) == (100.0, 200.0)

    def test_pixel_scroll_is_continuous(self, flat_display):
        events = act.scroll(POINT, dx=2, dy=-5, unit=ScrollUnit.PIXELS, dry_run=True)
        wheel = events[1]
        assert Quartz.CGEventGetIntegerValueField(
            wheel.event, Quartz.kCGScrollWheelEventPointDeltaAxis1
        ) == 5
        assert Quartz.CGEventGetIntegerValueField(
            wheel.event, Quartz.kCGScrollWheelEventPointDeltaAxis2
        ) == -2
        assert Quartz.CGEventGetIntegerValueField(
            wheel.event, Quartz.kCGScrollWheelEventIsContinuous
        ) == 1

    def test_scroll_pre_check_receives_scroll_action(self, flat_display):
        seen: list = []
        act.scroll(POINT, dy=1, pre_check=seen.append, dry_run=True)
        [action] = seen
        assert isinstance(action, Scroll)
        assert action.dy == 1 and action.unit is ScrollUnit.LINES


# --- type_text: unicode path -------------------------------------------------


class TestTypeTextUnicode:
    def test_short_text_single_chunk_roundtrip(self):
        events = act.type_text("hello", dry_run=True)
        assert [e.kind for e in events] == ["unicode_down", "unicode_up"]
        assert event_text(events[0]) == "hello"
        assert event_text(events[1]) == "hello", "key-up must carry the payload too"

    def test_unicode_events_carry_no_inherited_modifier_flags(self):
        # Fresh CGEvents inherit live hardware modifiers; a held cmd would
        # turn typed text into shortcuts (select-all + overwrite).
        events = act.type_text("hi", dry_run=True)
        assert all(mod_flags(e) == 0 for e in events)

    def test_fifty_chars_stays_on_unicode_path_and_chunks(self):
        text = "abcdefghij" * 5  # exactly 50 chars == threshold, not over it
        pb = FakePasteboard("untouched")
        events = act.type_text(text, dry_run=True, pasteboard=pb)
        assert pb.ops == []  # clipboard path not taken
        downs = [e for e in events if e.kind == "unicode_down"]
        chunks = [event_text(d) for d in downs]
        assert [len(c.encode("utf-16-le")) // 2 for c in chunks] == [20, 20, 10]
        assert "".join(chunks) == text

    def test_astral_chars_count_two_utf16_units(self):
        text = "\U0001f642" * 15  # 30 UTF-16 units
        events = act.type_text(text, dry_run=True)
        chunks = [event_text(e) for e in events if e.kind == "unicode_down"]
        assert [len(c.encode("utf-16-le")) // 2 for c in chunks] == [20, 10]
        assert "".join(chunks) == text

    def test_surrogate_pair_never_split_across_chunks(self):
        text = "a" * 19 + "\U0001f642"  # pair would straddle the 20-unit boundary
        events = act.type_text(text, dry_run=True)
        chunks = [event_text(e) for e in events if e.kind == "unicode_down"]
        assert chunks == ["a" * 19, "\U0001f642"]

    def test_empty_text_is_noop(self):
        assert act.type_text("", dry_run=True) == []

    def test_pre_check_receives_type_text_action(self):
        seen: list = []
        act.type_text("hi", pre_check=seen.append, dry_run=True)
        [action] = seen
        assert isinstance(action, TypeText) and action.text == "hi"

    def test_secure_input_active_raises_secure_field(self, monkeypatch):
        monkeypatch.setattr(act, "_secure_input_enabled", lambda: True)
        pb = FakePasteboard()
        with pytest.raises(ComputerUseError) as excinfo:
            act.type_text("x" * 60, dry_run=True, pasteboard=pb)
        assert excinfo.value.code is ErrorCode.SECURE_FIELD
        assert pb.ops == []  # refused before touching the clipboard

    def test_focused_secure_ax_field_raises_secure_field(self, monkeypatch):
        # Chrome/Electron password fields rarely enable secure event input;
        # the AX focused-element probe must catch them independently.
        monkeypatch.setattr(act, "_focused_element_secure", lambda: True)
        with pytest.raises(ComputerUseError) as excinfo:
            act.type_text("hunter2", dry_run=True)
        assert excinfo.value.code is ErrorCode.SECURE_FIELD
        assert excinfo.value.detail["api"] == "AXFocusedUIElement"


# --- type_text: clipboard path -----------------------------------------------


class TestTypeTextClipboard:
    def test_long_text_saves_pastes_and_restores(self):
        text = "x" * 51
        pb = FakePasteboard("previous contents")
        events = act.type_text(text, dry_run=True, pasteboard=pb)
        assert pb.ops == [
            ("save", "previous contents"),
            ("write", text),
            ("restore", "previous contents"),
        ]
        assert pb.content == "previous contents"
        # posted events are exactly cmd+v down/up
        assert [e.kind for e in events] == ["key_down", "key_up"]
        assert [keycode(e) for e in events] == [9, 9]  # 'v'
        assert all(mod_flags(e) == Quartz.kCGEventFlagMaskCommand for e in events)

    def test_empty_saved_pasteboard_restored_as_empty(self):
        pb = FakePasteboard(None)
        act.type_text("y" * 60, dry_run=True, pasteboard=pb)
        assert pb.ops[-1] == ("restore", None)
        assert pb.content is None

    def test_restore_skipped_when_someone_else_wrote(self):
        # A user copy (or clipboard manager write) during the paste window
        # moves changeCount; restoring would clobber their new content.
        class BusyPasteboard(FakePasteboard):
            def change_count(self) -> int:
                self.changes += 1  # someone writes between every observation
                return self.changes

        pb = BusyPasteboard("previous contents")
        act.type_text("z" * 60, dry_run=True, pasteboard=pb)
        assert ("restore", "previous contents") not in pb.ops

    def test_dry_run_without_injected_pasteboard_never_touches_system(self, monkeypatch):
        def boom(*args, **kwargs):
            raise AssertionError("system pasteboard touched in dry_run")

        monkeypatch.setattr(act, "_SystemPasteboard", boom)
        events = act.type_text("z" * 60, dry_run=True)
        assert [e.kind for e in events] == ["key_down", "key_up"]


# --- parse_chord / key_chord --------------------------------------------------


class TestParseChord:
    def test_modifier_map_matches_schema(self):
        assert set(act._MODIFIER_FLAGS) == MODIFIER_KEYS

    def test_full_chord(self):
        flags, code = act.parse_chord("cmd+shift+s")
        assert flags == Quartz.kCGEventFlagMaskCommand | Quartz.kCGEventFlagMaskShift
        assert code == 1  # 's' on US layout

    def test_bare_key_has_no_flags(self):
        assert act.parse_chord("escape") == (0, 53)

    def test_names_are_case_insensitive(self):
        assert act.parse_chord("CMD+S") == act.parse_chord("cmd+s")

    @pytest.mark.parametrize(
        ("chord", "match"),
        [
            ("", "malformed chord"),
            ("cmd+", "malformed chord"),
            ("+s", "malformed chord"),
            ("cmd++s", "malformed chord"),
            ("cmd+shift", "no non-modifier key"),
            ("super+s", "unknown modifier 'super'"),
            ("cmd+notakey", "unknown key 'notakey'"),
        ],
    )
    def test_malformed_chords_rejected(self, chord, match):
        with pytest.raises(ValueError, match=match):
            act.parse_chord(chord)

    @pytest.mark.parametrize(
        ("key", "code"),
        [("f5", 96), ("return", 36), ("enter", 36), ("tab", 48), ("left", 123)],
    )
    def test_named_and_function_keys(self, key, code):
        assert act.parse_chord(key) == (0, code)


class TestKeyChord:
    def test_builds_down_up_with_flags_and_keycode(self):
        events = act.key_chord("cmd+shift+s", dry_run=True)
        assert [e.kind for e in events] == ["key_down", "key_up"]
        assert [etype(e) for e in events] == [
            Quartz.kCGEventKeyDown,
            Quartz.kCGEventKeyUp,
        ]
        expected = Quartz.kCGEventFlagMaskCommand | Quartz.kCGEventFlagMaskShift
        assert all(keycode(e) == 1 for e in events)
        assert all(mod_flags(e) == expected for e in events)

    def test_pre_check_receives_key_chord_action(self):
        seen: list = []
        act.key_chord("cmd+q", pre_check=seen.append, dry_run=True)
        [action] = seen
        assert isinstance(action, KeyChord) and action.chord == "cmd+q"

    def test_malformed_chord_fails_before_pre_check(self):
        seen: list = []
        with pytest.raises(ValueError):
            act.key_chord("cmd+shift", pre_check=seen.append, dry_run=True)
        assert seen == []


# --- permission gate ----------------------------------------------------------

LIVE_ACTIONS = {
    "click": lambda: act.click(POINT),
    "drag": lambda: act.drag(Point(1, 0, 0), Point(1, 5, 5)),
    "scroll": lambda: act.scroll(POINT, dy=1),
    "type_short": lambda: act.type_text("hi"),
    "type_clipboard": lambda: act.type_text("x" * 60, pasteboard=FakePasteboard("s")),
    "key_chord": lambda: act.key_chord("cmd+s"),
}


class TestPermissionGate:
    @pytest.mark.parametrize("name", sorted(LIVE_ACTIONS))
    def test_live_actions_raise_structured_permission_error(self, name, monkeypatch):
        monkeypatch.setattr(act, "_ax_trusted", lambda: False)
        with pytest.raises(ComputerUseError) as excinfo:
            LIVE_ACTIONS[name]()
        assert excinfo.value.code is ErrorCode.PERMISSION_DENIED_ACCESSIBILITY
        assert excinfo.value.to_dict()["error"] == "permission_denied_accessibility"

    def test_permission_error_precedes_clipboard_side_effects(self, monkeypatch):
        monkeypatch.setattr(act, "_ax_trusted", lambda: False)
        pb = FakePasteboard("keep")
        with pytest.raises(ComputerUseError):
            act.type_text("x" * 60, pasteboard=pb)
        assert pb.ops == []

    def test_dry_run_skips_permission_gate(self, flat_display, monkeypatch):
        monkeypatch.setattr(act, "_ax_trusted", lambda: False)
        assert act.click(POINT, dry_run=True)  # no raise

    def test_pre_check_runs_before_permission_gate(self, monkeypatch):
        monkeypatch.setattr(act, "_ax_trusted", lambda: False)

        def veto(action):
            raise RuntimeError("safety veto sentinel")

        with pytest.raises(RuntimeError, match="safety veto sentinel"):
            act.click(POINT, pre_check=veto)

    def test_dry_run_never_posts(self, flat_display, monkeypatch):
        def boom(*args):
            raise AssertionError("CGEventPost called during dry_run")

        monkeypatch.setattr(act.Quartz, "CGEventPost", boom)
        act.click(POINT, count=3, dry_run=True)
        act.drag(Point(1, 0, 0), Point(1, 50, 50), dry_run=True)
        act.scroll(POINT, dy=2, dry_run=True)
        act.type_text("short", dry_run=True)
        act.type_text("x" * 60, dry_run=True, pasteboard=FakePasteboard())
        act.key_chord("cmd+shift+s", dry_run=True)

    def test_ax_trusted_probe_returns_bool(self):
        assert isinstance(act._ax_trusted(), bool)


# --- coordinate mapping --------------------------------------------------------


class TestPointToGlobal:
    def test_real_main_display_mapping(self):
        main = Quartz.CGMainDisplayID()
        bounds = Quartz.CGDisplayBounds(main)
        mode = Quartz.CGDisplayCopyDisplayMode(main)
        scale = Quartz.CGDisplayModeGetPixelWidth(mode) / bounds.size.width
        gx, gy = act._point_to_global(Point(main, int(100 * scale), int(40 * scale)))
        assert gx == pytest.approx(bounds.origin.x + 100)
        assert gy == pytest.approx(bounds.origin.y + 40)

    def test_unknown_display_rejected(self):
        with pytest.raises(ValueError, match="unknown display_id"):
            act._point_to_global(Point(999_999_999, 1, 1))


# --- element re-resolution slot -------------------------------------------------


class TestResolverSlot:
    def test_installed_resolver_supplies_live_bounds(self, flat_display, synthetic_snapshot):
        button = synthetic_snapshot.element("e2")
        moved = dataclasses.replace(button, bounds=Bounds(1, 0, 0, 10, 10))
        act.set_resolver(lambda el: moved)
        events = act.click(button, dry_run=True)
        assert loc(events[1]) == (5.0, 5.0)

    def test_resolver_stale_ref_propagates(self, flat_display, synthetic_snapshot):
        def stale(element):
            raise ComputerUseError(ErrorCode.STALE_REF, "tree rebuilt; re-observe")

        act.set_resolver(stale)
        with pytest.raises(ComputerUseError) as excinfo:
            act.click(synthetic_snapshot.element("e2"), dry_run=True)
        assert excinfo.value.code is ErrorCode.STALE_REF

    def test_resolved_secure_element_still_refused(self, flat_display, synthetic_snapshot):
        # A resolver may discover mid-flight that the target became secure.
        secure = synthetic_snapshot.element("e4")
        act.set_resolver(lambda el: secure)
        with pytest.raises(ComputerUseError) as excinfo:
            act.click(synthetic_snapshot.element("e2"), dry_run=True)
        assert excinfo.value.code is ErrorCode.SECURE_FIELD


# --- wait_for --------------------------------------------------------------------


class TestWaitFor:
    def test_returns_immediately_when_condition_holds(self, synthetic_snapshot):
        element = synthetic_snapshot.element("e2")
        assert act.wait_for(element, checker=lambda t, c: t) is element

    def test_polls_until_checker_succeeds(self, monkeypatch, synthetic_snapshot):
        monkeypatch.setattr(act, "WAIT_POLL_INTERVAL_S", 0.005)
        element = synthetic_snapshot.element("e2")
        calls: list[WaitCondition] = []

        def checker(target, condition):
            calls.append(condition)
            return element if len(calls) >= 3 else None

        result = act.wait_for(
            element, condition=WaitCondition.ACTIONABLE, timeout_s=1.0, checker=checker
        )
        assert result is element
        assert calls == [WaitCondition.ACTIONABLE] * 3

    def test_timeout_raises_structured_error(self, monkeypatch, synthetic_snapshot):
        monkeypatch.setattr(act, "WAIT_POLL_INTERVAL_S", 0.005)
        element = synthetic_snapshot.element("e2")
        with pytest.raises(ComputerUseError) as excinfo:
            act.wait_for(
                element,
                condition=WaitCondition.GONE,
                timeout_s=0.02,
                checker=lambda t, c: None,
            )
        assert excinfo.value.code is ErrorCode.TIMEOUT
        assert excinfo.value.detail == {
            "ref": "e2",
            "condition": "gone",
            "timeout_s": 0.02,
        }

    def test_module_slot_used_when_no_checker_argument(self, synthetic_snapshot):
        element = synthetic_snapshot.element("e2")
        act.set_wait_checker(lambda t, c: t)
        assert act.wait_for(element) is element

    def test_missing_checker_is_a_wiring_error(self, synthetic_snapshot):
        with pytest.raises(RuntimeError, match="no wait checker configured"):
            act.wait_for(synthetic_snapshot.element("e2"))

    def test_pre_check_receives_wait_for_action(self, synthetic_snapshot):
        element = synthetic_snapshot.element("e2")
        seen: list = []
        act.wait_for(
            element,
            condition=WaitCondition.GONE,
            timeout_s=2.0,
            pre_check=seen.append,
            checker=lambda t, c: t,
        )
        [action] = seen
        assert isinstance(action, WaitFor)
        assert action.condition is WaitCondition.GONE
        assert action.timeout_s == 2.0


# --- live (permission-gated) ------------------------------------------------------


@pytest.mark.skipif(not HAS_AX, reason="Accessibility TCC grant required to post CGEvents")
def test_live_post_mouse_move_to_current_position():
    """Posts one mouse-move to the cursor's CURRENT location — zero-impact by
    construction (no click, no type, no movement)."""
    current = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    event = act._mouse_event(Quartz.kCGEventMouseMoved, (current.x, current.y), MouseButton.LEFT)
    act._post([act.BuiltEvent("mouse_move", event)], dry_run=False)
