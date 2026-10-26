"""Hermetic actionability waits. No AT-SPI bus and no display.

A wait reports success only when the latest read is visible, enabled, stable,
and receiving events, and the checker returned that element. A timeout names
the first check that failed.
"""

from __future__ import annotations

import pytest

from a11y_computer_use.drivers import _actionable, _atspi_events
from a11y_computer_use.drivers._actionable import Observation, wait_until_actionable, wait_until_checker
from a11y_computer_use.schema import Bounds, ComputerUseError, Element, ErrorCode, WaitCondition


def _element(*, enabled: bool = True, clickable: bool = True, ref: str = "e1") -> Element:
    return Element(
        ref=ref,
        role="AXButton",
        title="Save",
        value=None,
        bounds=Bounds(0, 10, 20, 80, 30),
        snapshot_id="snap-1",
        enabled=enabled,
        clickable=clickable,
    )


def _ready(box: tuple[int, int, int, int] = (10, 20, 80, 30)) -> Observation:
    return Observation(True, True, box, True)


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []
        self.waits: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds

    def wait_event(self, seconds: float) -> bool:
        self.waits.append(seconds)
        self.t += seconds
        return False


def _run(samples: list[Observation], *, timeout_s: float, clock: _Clock | None = None,
         confirm_result: Element | None = None, use_events: bool = False):
    clock = clock or _Clock()
    pending = list(samples)
    seen: list[Observation] = []

    def read() -> Observation:
        if not pending:
            return seen[-1]
        item = pending.pop(0)
        seen.append(item)
        return item

    def confirm() -> Element | None:
        return confirm_result

    kwargs = {"now": clock.now}
    if use_events:
        kwargs["wait_event"] = clock.wait_event
    else:
        kwargs["sleep"] = clock.sleep
    return wait_until_actionable(
        _element(), timeout_s=timeout_s, read=read, confirm=confirm, **kwargs,
    ), clock


def test_event_names_include_state_children_bounds_and_focus() -> None:
    names = _atspi_events.event_names()
    assert "object:state-changed" in names
    assert "object:children-changed" in names
    assert "object:bounds-changed" in names
    assert "focus:" in names


def test_listener_stays_off_without_a_display(monkeypatch) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("A11Y_COMPUTER_USE_ATSPI_EVENTS", "0")
    assert _atspi_events.enabled() is False
    # The Linux default starts the thread on the first driver call, so a
    # full-suite process can already be listening. A missing display must
    # still refuse to start one that is down.
    monkeypatch.setattr(_atspi_events._listening, "is_set", lambda: False)
    monkeypatch.setattr(_atspi_events, "_listener_failed", False)
    started: list[int] = []
    monkeypatch.setattr(_atspi_events, "_ensure", lambda: started.append(1))
    assert _atspi_events.ensure_started() is False
    assert started == []
    monkeypatch.setattr(_atspi_events._listening, "is_set", lambda: True)
    assert _atspi_events.ensure_started() is True
    assert started == []
    assert _actionable.FALLBACK_POLL_S <= 0.1
    assert _actionable.EVENT_WAIT_CAP_S <= 0.4
    assert _actionable.FALLBACK_POLL_S == 0.1
    assert _actionable.EVENT_WAIT_CAP_S == 0.4


def test_timeout_arguments_are_rejected() -> None:
    target = _element()
    for bad in (True, False, "1", None, object()):
        with pytest.raises(TypeError):
            wait_until_actionable(target, timeout_s=bad, read=lambda: _ready(), confirm=lambda: target)
    for bad in (float("nan"), float("inf"), -0.1):
        with pytest.raises(ValueError):
            wait_until_actionable(target, timeout_s=bad, read=lambda: _ready(), confirm=lambda: target)
    with pytest.raises(TypeError):
        wait_until_actionable(target, timeout_s=1, read=None, confirm=lambda: target)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        wait_until_checker(target, condition="exists", timeout_s=0, checker=lambda t, c: t)  # type: ignore[arg-type]


def test_timeout_names_the_first_failed_check_and_does_not_return_the_element() -> None:
    cases = [
        (Observation(False, False, None, False), "visible"),
        (Observation(True, False, (10, 20, 80, 30), True), "enabled"),
        (Observation(True, True, (10, 20, 80, 30), False), "stable"),
    ]
    for sample, check in cases:
        clock = _Clock()
        with pytest.raises(ComputerUseError) as info:
            wait_until_actionable(
                _element(), timeout_s=0, read=lambda s=sample: s, confirm=lambda: _element(),
                now=clock.now, sleep=clock.sleep,
            )
        assert info.value.code is ErrorCode.TIMEOUT
        assert info.value.detail["check"] == check
        assert info.value.detail["condition"] == "actionable"
        assert clock.sleeps == []


def test_one_matching_sample_is_not_stable_and_a_changed_box_never_succeeds() -> None:
    live = _element()
    confirmed = {"n": 0}

    def confirm():
        confirmed["n"] += 1
        return live

    with pytest.raises(ComputerUseError) as info:
        wait_until_actionable(
            _element(), timeout_s=0, read=lambda: _ready(), confirm=confirm,
            now=lambda: 0.0, sleep=lambda _s: None,
        )
    assert info.value.detail["check"] == "stable"
    assert confirmed["n"] == 0

    boxes = [(10, 20, 80, 30), (14, 20, 80, 30)]
    index = {"n": 0}

    def read() -> Observation:
        box = boxes[index["n"] % 2]
        index["n"] += 1
        return Observation(True, True, box, True)

    clock = _Clock()
    index["n"] = 0
    confirmed["n"] = 0
    with pytest.raises(ComputerUseError) as moving:
        wait_until_actionable(
            _element(), timeout_s=0.3, read=read, confirm=confirm,
            now=clock.now, sleep=clock.sleep,
        )
    assert moving.value.detail["check"] == "stable"
    assert moving.value.code is ErrorCode.TIMEOUT
    assert all(item <= 0.1 for item in clock.sleeps)
    assert clock.t <= 0.3 + 1e-9


def test_success_is_the_confirmed_element_only_after_a_second_stable_read() -> None:
    live = _element(ref="e9")
    original = _element(ref="e1")
    box = (10, 20, 80, 30)
    samples = [_ready(box), _ready(box), _ready(box)]
    result, clock = _run(samples, timeout_s=1.0, confirm_result=live)
    assert result is live
    assert result is not original
    assert result.actionable is True
    assert clock.sleeps
    assert all(item <= _actionable.FALLBACK_POLL_S for item in clock.sleeps)


def test_an_event_is_not_success_and_the_wake_stays_inside_the_cap() -> None:
    live = _element()
    box = (10, 20, 80, 30)
    # Stable on the second sample, then the confirmation re-read is covered.
    samples = [
        _ready(box),
        _ready(box),
        Observation(True, True, box, False),
        Observation(True, True, box, False),
    ]
    clock = _Clock()
    with pytest.raises(ComputerUseError) as info:
        wait_until_actionable(
            _element(), timeout_s=0.5, read=_iter(samples), confirm=lambda: live,
            now=clock.now, wait_event=clock.wait_event,
        )
    assert info.value.detail["check"] == "receiving_events"
    assert info.value.detail["receiving_events"] is False
    assert info.value.detail["visible"] is True
    assert info.value.detail["enabled"] is True
    assert all(item <= _actionable.EVENT_WAIT_CAP_S for item in clock.waits)
    assert clock.t <= 0.5 + 1e-9


def test_confirm_without_an_actionable_element_is_not_success() -> None:
    box = (10, 20, 80, 30)
    samples = [_ready(box), _ready(box), _ready(box), _ready(box)]
    clock = _Clock()
    with pytest.raises(ComputerUseError) as info:
        wait_until_actionable(
            _element(), timeout_s=0.2, read=_iter(samples), confirm=lambda: None,
            now=clock.now, sleep=clock.sleep,
        )
    assert info.value.detail["check"] == "actionable"
    assert info.value.code is ErrorCode.TIMEOUT


def test_exists_and_gone_return_only_the_checker_element() -> None:
    target = _element()
    found = wait_until_checker(
        target, condition=WaitCondition.EXISTS, timeout_s=0,
        checker=lambda element, condition: element, now=lambda: 0.0, sleep=lambda _s: None,
    )
    assert found is target
    gone = wait_until_checker(
        target, condition=WaitCondition.GONE, timeout_s=0,
        checker=lambda element, condition: element, now=lambda: 0.0, sleep=lambda _s: None,
    )
    assert gone is target
    clock = _Clock()
    with pytest.raises(ComputerUseError) as info:
        wait_until_checker(
            target, condition=WaitCondition.EXISTS, timeout_s=0.25,
            checker=lambda element, condition: None, now=clock.now, sleep=clock.sleep,
        )
    assert info.value.code is ErrorCode.TIMEOUT
    assert info.value.detail == {
        "ref": "e1",
        "condition": "exists",
        "timeout_s": 0.25,
    }
    assert "check" not in info.value.detail
    assert all(item <= 0.1 for item in clock.sleeps)
    with pytest.raises(TypeError):
        wait_until_checker(
            target, condition=WaitCondition.EXISTS, timeout_s=0,
            checker=lambda element, condition: "nope",  # type: ignore[return-value]
            now=lambda: 0.0, sleep=lambda _s: None,
        )


class _CacheNode:
    def __init__(self, *kids: "_CacheNode") -> None:
        self.kids = list(kids)
        self.parent: _CacheNode | None = None
        self.cleared = 0
        for kid in self.kids:
            kid.parent = self

    def clear_cache(self) -> None:
        self.cleared += 1

    def get_child_count(self) -> int:
        return len(self.kids)

    def get_child_at_index(self, index: int) -> "_CacheNode":
        return self.kids[index]

    def get_parent(self) -> "_CacheNode | None":
        return self.parent


def test_refresh_event_cache_is_a_noop_until_the_listener_is_up(monkeypatch) -> None:
    from a11y_computer_use.drivers import _atspi

    node = _CacheNode()
    monkeypatch.setattr(_atspi_events, "listening", lambda: False)
    _atspi._refresh_event_cache(node)
    _atspi._refresh_event_cache(None)
    assert node.cleared == 0
    monkeypatch.setattr(_atspi_events, "listening", lambda: True)
    _atspi._refresh_event_cache(node)
    assert node.cleared == 1


def test_refresh_event_cache_drops_the_menu_and_its_items(monkeypatch) -> None:
    from a11y_computer_use.drivers import _atspi

    nested = _CacheNode()
    item = _CacheNode(nested)
    menu = _CacheNode(item)
    combo = _CacheNode(menu)
    monkeypatch.setattr(_atspi_events, "listening", lambda: True)
    _atspi._refresh_event_cache(combo)
    assert combo.cleared == 1
    assert menu.cleared == 1
    assert item.cleared == 1
    assert nested.cleared == 0


def test_gecko_choice_shows_refreshes_before_the_selected_read(monkeypatch) -> None:
    from a11y_computer_use.drivers import _atspi

    order: list[str] = []

    def refresh(combo: object) -> None:
        order.append("refresh")

    def selected(combo: object) -> str:
        order.append("read")
        return "Blue"

    monkeypatch.setattr(_atspi, "_refresh_event_cache", refresh)
    monkeypatch.setattr(_atspi, "_selected_option_text", selected)
    assert _atspi._gecko_choice_shows(object(), "Blue", wait=False) is True
    assert order == ["refresh", "read"]


def test_gecko_set_combo_refreshes_before_collecting_options(monkeypatch) -> None:
    from a11y_computer_use.drivers import _atspi

    order: list[str] = []
    monkeypatch.setattr(_atspi, "_gecko_app", lambda acc: True)
    monkeypatch.setattr(_atspi, "_refresh_event_cache", lambda acc: order.append("refresh"))
    monkeypatch.setattr(_atspi, "_combo_entry", lambda acc: order.append("entry") or "entry")
    monkeypatch.setattr(_atspi, "_collect_options", lambda acc: order.append("collect") or [])
    monkeypatch.setattr(_atspi, "_set_entry_contents", lambda entry, value: True)
    monkeypatch.setattr(_atspi, "_close_combo_popup", lambda acc: None)
    monkeypatch.setattr(_atspi, "_state_has", lambda acc, name: False)
    monkeypatch.setattr(_atspi, "_combo_landed", lambda acc, entry, value: True)
    _atspi.set_combo_value(object(), "Blue")
    assert order[:3] == ["refresh", "entry", "collect"]


def test_property_reads_drop_one_node_while_the_listener_is_up(monkeypatch) -> None:
    """Name, state, parent, and child reads bypass the event cache.

    ``clear_cache_single`` forgets this accessible only. The recursive
    ``clear_cache`` is what ``_refresh_event_cache`` counts, and a property
    read must not call it. With the listener off the cached name stays.
    """
    from a11y_computer_use.drivers import _atspi

    class Node:
        def __init__(self) -> None:
            self.single = 0
            self.recursive = 0
            self.name = "Red"
            self.states: set[str] = set()
            self.parent = "old-parent"
            self.kids = 1

        def clear_cache_single(self) -> None:
            self.single += 1
            self.name = "Blue"
            self.states = {"SELECTED"}
            self.parent = "frame"
            self.kids = 2

        def clear_cache(self) -> None:
            self.recursive += 1

        def get_name(self) -> str:
            return self.name

        def get_state_set(self):
            names = set(self.states)
            return type("States", (), {"names": names})()

        def get_parent(self):
            return self.parent

        def get_child_count(self) -> int:
            return self.kids

    node = Node()
    monkeypatch.setattr(_atspi_events, "listening", lambda: False)
    assert _atspi._node_name(node) == "Red"
    assert _atspi._state_has(node, "SELECTED") is False
    assert node.single == 0 and node.recursive == 0
    monkeypatch.setattr(_atspi_events, "listening", lambda: True)
    assert _atspi._node_name(node) == "Blue"
    assert _atspi._state_has(node, "SELECTED") is True
    assert _atspi._parent_of(node) == "frame"
    assert _atspi._child_count(node) == 2
    assert node.single == 4
    assert node.recursive == 0


def test_other_combos_do_not_refresh_the_event_cache(monkeypatch) -> None:
    from a11y_computer_use.drivers import _atspi

    called: list[object] = []
    monkeypatch.setattr(_atspi, "_gecko_app", lambda acc: False)
    monkeypatch.setattr(_atspi, "_refresh_event_cache", lambda acc: called.append(acc))
    monkeypatch.setattr(_atspi, "_combo_entry", lambda acc: "entry")
    monkeypatch.setattr(_atspi, "_collect_options", lambda acc: [])
    monkeypatch.setattr(_atspi, "_set_entry_contents", lambda entry, value: True)
    monkeypatch.setattr(_atspi, "_close_combo_popup", lambda acc: None)
    monkeypatch.setattr(_atspi, "_state_has", lambda acc, name: False)
    monkeypatch.setattr(_atspi, "_combo_landed", lambda acc, entry, value: True)
    _atspi.set_combo_value(object(), "Blue")
    assert called == []


def test_refresh_cached_tree_is_a_noop_until_the_listener_is_up(monkeypatch) -> None:
    from a11y_computer_use.drivers import _atspi

    dropped: list[object] = []
    monkeypatch.setattr(_atspi, "_invalidate_tree_cache", lambda root: dropped.append(root))
    monkeypatch.setattr(_atspi_events, "listening", lambda: False)
    _actionable.refresh_cached_tree("root")
    _actionable.refresh_cached_tree(None)
    assert dropped == []
    monkeypatch.setattr(_atspi_events, "listening", lambda: True)
    _actionable.refresh_cached_tree("root")
    assert dropped == ["root"]


def test_snapshot_root_drops_cached_children_only_while_listening(monkeypatch) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("A11Y_COMPUTER_USE_ATSPI_EVENTS", raising=False)
    from a11y_computer_use.drivers import _atspi
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import Scope

    driver = LinuxDriver()
    dropped: list[object] = []
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(_atspi, "find_root", lambda name, scope: "the-root")
    monkeypatch.setattr(_atspi_events, "listening", lambda: False)

    def refresh(root: object) -> None:
        if _atspi_events.listening():
            dropped.append(root)

    monkeypatch.setattr(_actionable, "refresh_cached_tree", refresh)
    assert driver._root_for_snapshot("chrome", Scope.WINDOW) == ("chrome", "the-root")
    assert dropped == []
    monkeypatch.setattr(_atspi_events, "listening", lambda: True)
    assert driver._root_for_snapshot("chrome", Scope.WINDOW) == ("chrome", "the-root")
    assert dropped == ["the-root"]


def test_linux_wait_for_does_not_treat_a_checker_hit_as_actionable(monkeypatch) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("A11Y_COMPUTER_USE_ATSPI_EVENTS", "0")
    from a11y_computer_use.drivers.linux import LinuxDriver

    target = _element()
    driver = LinuxDriver()
    with pytest.raises(ComputerUseError) as info:
        driver.wait_for(
            target, condition=WaitCondition.ACTIONABLE, timeout_s=0,
            checker=lambda element, condition: element,
        )
    assert info.value.code is ErrorCode.TIMEOUT
    assert info.value.detail["check"] == "visible"
    assert driver.wait_for(
        target, condition=WaitCondition.EXISTS, timeout_s=0,
        checker=lambda element, condition: element,
    ) is target
    with pytest.raises(ValueError, match="needs a checker"):
        driver.wait_for(target, condition=WaitCondition.EXISTS, timeout_s=0, checker=None)
    with pytest.raises(TypeError):
        driver.wait_for(
            target, condition=WaitCondition.EXISTS, timeout_s=True,  # type: ignore[arg-type]
            checker=lambda element, condition: element,
        )


def test_linux_actionable_wait_re_resolves_a_ref_whose_snapshot_was_evicted(monkeypatch) -> None:
    """#1175: a ref kept from an earlier find outlived its snapshot's handles.

    observe keeps handles for the last _MAX_EPOCHS snapshots. After enough
    other calls the stored handle was gone, every read probed nothing, and
    the wait ran its whole budget to "did not become visible" on an enabled
    button. The wait must re-resolve the ref through the checker and probe
    the live accessible, and a ref that no longer resolves is stale_ref at
    once, not a timeout.
    """
    import time

    from a11y_computer_use import observe
    from a11y_computer_use.drivers.linux import LinuxDriver

    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("A11Y_COMPUTER_USE_ATSPI_EVENTS", "0")
    old = _element()
    live = Element(
        ref="e40", role="AXButton", title="Save", value=None,
        bounds=Bounds(0, 10, 20, 80, 30), snapshot_id="snap-live",
        enabled=True, clickable=True,
    )
    button = object()
    handles = {("snap-live", "e40"): button}
    monkeypatch.setattr(observe, "ax_handle_for", lambda sid, ref: handles.get((sid, ref)))
    monkeypatch.setattr(
        _actionable, "probe_accessible",
        lambda acc: _ready() if acc is button else Observation(False, False, None, False),
    )
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    started = time.monotonic()
    found = driver.wait_for(
        old, condition=WaitCondition.ACTIONABLE, timeout_s=5,
        checker=lambda element, condition: live,
    )
    assert found is live
    assert time.monotonic() - started < 1.0

    started = time.monotonic()
    with pytest.raises(ComputerUseError) as info:
        driver.wait_for(
            old, condition=WaitCondition.ACTIONABLE, timeout_s=5,
            checker=lambda element, condition: None,
        )
    assert info.value.code is ErrorCode.STALE_REF
    assert info.value.detail["ref"] == "e1"
    assert time.monotonic() - started < 1.0


def test_probe_fail_closed_on_visibility_enabled_and_the_hit(monkeypatch) -> None:
    from a11y_computer_use.drivers import _atspi, _linux_system

    acc = object()
    child = object()
    sibling = object()
    states = {"SHOWING": True, "ENABLED": True, "SENSITIVE": False}

    monkeypatch.setattr(_atspi, "accessible_gone", lambda _acc: False)
    monkeypatch.setattr(_atspi, "hidden_web_target", lambda _acc: False)
    monkeypatch.setattr(_atspi, "_state_has", lambda _acc, name: states.get(name) is True)
    monkeypatch.setattr(_atspi, "_extents", lambda _acc: ((10.2, 20.4), (80.2, 30.2)))
    monkeypatch.setattr(_atspi, "_misses_primary_screen", lambda _pos, _size: False)
    monkeypatch.setattr(_atspi, "_call_first", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(_atspi, "_role_name", lambda _acc: "push button")
    monkeypatch.setattr(_atspi, "pid_of", lambda _acc: 5)
    monkeypatch.setattr(_linux_system, "pid_at_point", lambda _x, _y: 5)

    def parent_of(node):
        if node is child:
            return acc
        return None

    monkeypatch.setattr(_atspi, "_parent_of", parent_of)
    monkeypatch.setattr(_atspi, "_deepest_at_point", lambda *_args, **_kwargs: child)
    ready = _actionable.probe_accessible(acc)
    assert ready.visible is True
    assert ready.enabled is True
    assert ready.receiving_events is True
    assert ready.bounds == (10, 20, 80, 30)

    monkeypatch.setattr(_atspi, "_deepest_at_point", lambda *_args, **_kwargs: sibling)
    missed = _actionable.probe_accessible(acc)
    assert missed.visible is True and missed.receiving_events is False

    monkeypatch.setattr(_linux_system, "pid_at_point", lambda _x, _y: 9)
    monkeypatch.setattr(_atspi, "_deepest_at_point", lambda *_args, **_kwargs: acc)
    covered = _actionable.probe_accessible(acc)
    assert covered.receiving_events is False

    monkeypatch.setattr(_atspi, "pid_of", lambda _acc: None)
    monkeypatch.setattr(_linux_system, "pid_at_point", lambda _x, _y: 5)
    unproven = _actionable.probe_accessible(acc)
    assert unproven.receiving_events is False

    monkeypatch.setattr(_atspi, "_call_first", lambda *_args, **_kwargs: None)
    no_state = _actionable.probe_accessible(acc)
    assert no_state.enabled is False

    monkeypatch.setattr(_atspi, "_misses_primary_screen", lambda _pos, _size: True)
    off = _actionable.probe_accessible(acc)
    assert off.visible is False and off.bounds is None

    states["SHOWING"] = False
    hidden = _actionable.probe_accessible(acc)
    assert hidden == Observation(False, False, None, False)

    monkeypatch.setattr(_atspi, "accessible_gone", lambda _acc: True)
    assert _actionable.probe_accessible(acc) == Observation(False, False, None, False)
    assert _actionable.probe_accessible(None) == Observation(False, False, None, False)


def _iter(samples: list[Observation]):
    pending = list(samples)

    def read() -> Observation:
        if not pending:
            return samples[-1]
        return pending.pop(0)

    return read
