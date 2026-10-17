"""Typed action outcomes. Hermetic: a fake driver and a scripted agent.

The sentence each tool already returned stays the string value. ``outcome``,
``next``, and ``evidence`` are attributes, and the MCP tool result puts them
in structured content. A live process is used only for the crash case, where
the pid has to really exit.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
import time

import pytest

from a11y_computer_use import outcome, safety, server
from a11y_computer_use.agent.models.base import ToolCall
from a11y_computer_use.agent.models.scripted import ScriptedModel
from a11y_computer_use.schema import (
    Bounds,
    ComputerUseError,
    Display,
    Element,
    ErrorCode,
    Point,
    Scope,
    Snapshot,
)
from tests.test_agent_core import FakeRuntime, done, el, run, turn, window

APP = "demo"


def _element(ref: str, role: str, title: str, **kwargs) -> Element:
    bounds = kwargs.pop("bounds", Bounds(0, 10, 20, 80, 24))
    return Element(
        ref=ref,
        role=role,
        title=title,
        value=kwargs.pop("value", None),
        bounds=bounds,
        snapshot_id="snap",
        **kwargs,
    )


def _tree() -> list[Element]:
    return [
        _element("e1", "AXWindow", "Demo", path=("AXWindow",)),
        _element(
            "e2", "AXButton", "Save", parent="e1", path=("AXWindow", "AXButton"),
            clickable=True,
        ),
        _element(
            "e3", "AXTextField", "Name", parent="e1", path=("AXWindow", "AXTextField"),
            value="", editable=True, focused=True, clickable=True,
        ),
        _element(
            "e4", "AXStaticText", "Idle label", parent="e1",
            path=("AXWindow", "AXStaticText"), clickable=True,
        ),
        _element(
            "e5", "AXStaticText", "ready", parent="e1",
            path=("AXWindow", "AXStaticText"),
        ),
        _element(
            "e6", "AXPopUpButton", "Color", parent="e1",
            path=("AXWindow", "AXPopUpButton"), value="Red", clickable=True,
        ),
        _element(
            "e7", "AXList", "Rows", parent="e1", path=("AXWindow", "AXList"),
            bounds=Bounds(0, 10, 80, 200, 120),
        ),
        _element(
            "e8", "AXButton", "Publish", parent="e1", path=("AXWindow", "AXButton"),
            clickable=True, enabled=False,
        ),
    ]


class TreeDriver:
    """Mutable accessibility tree. ``resolves_apps`` keeps the test off the OS."""

    name = "fake"
    resolves_apps = True

    def __init__(self) -> None:
        self.elements = _tree()
        self.pid = 4242
        self.front = APP
        self.menu = {"open": False, "path": []}
        self.rows = [{
            "window_id": 7,
            "app": APP,
            "title": "Demo",
            "on_screen": True,
            "bounds": {"display_id": 0, "x": 1, "y": 2, "width": 300, "height": 200},
        }]
        self.apps = [{"app": APP, "name": "Demo", "pid": 4242}]
        self.dead = False
        self.apply_type = False
        self.move_lands = True
        self.calls: list[tuple] = []

    def ensure_trusted(self) -> None:
        return None

    def frontmost_app(self):
        return self.front, self.pid

    def main_display_id(self) -> int:
        return 0

    def snapshot(self, scope, app) -> Snapshot:
        if self.dead:
            raise ComputerUseError(ErrorCode.APP_NOT_FOUND, "gone", detail={"app": app})
        return Snapshot(
            snapshot_id="snap",
            scope=scope,
            app=app,
            pid=self.pid,
            created_at=0.0,
            displays=(Display(0, 800, 600, 1.0, True),),
            elements=tuple(self.elements),
        )

    def screenshot(self, display_id=None):
        """A blank display capture. An empty tree asks for one on macOS.

        Auto-OCR runs when a snapshot has no clickable or editable element.
        The image covers the window rect the driver publishes (display 0,
        800 by 600) so the crop is inside the picture.
        """
        from a11y_computer_use.capture import Screenshot

        display = Display(0 if display_id is None else int(display_id), 800, 600, 1.0, True)
        png = getattr(self, "_png", None)
        if png is None:
            import io

            from PIL import Image

            buffer = io.BytesIO()
            Image.new("RGB", (display.width, display.height), "white").save(buffer, format="PNG")
            png = buffer.getvalue()
            self._png = png
        return Screenshot(png=png, display=display)

    def resolve_ref(self, snap: Snapshot, ref: str, *, live: Snapshot | None = None) -> Element:
        del live
        return snap.element(ref)

    def _replace(self, ref: str, **changes) -> None:
        self.elements = [
            dataclasses.replace(item, **changes) if item.ref == ref else item
            for item in self.elements
        ]

    def press_element(self, element: Element) -> bool:
        self.calls.append(("press", element.ref))
        if element.ref == "e2":
            self._replace("e5", title="saved")
            return True
        if element.ref == "e4":
            return True
        return False

    def click(self, target, **kwargs) -> None:
        self.calls.append(("click", getattr(target, "ref", None), kwargs))

    def type_text(self, text: str, **kwargs) -> int:
        del kwargs
        self.calls.append(("type", text))
        if self.apply_type:
            current = next(item for item in self.elements if item.ref == "e3")
            self._replace("e3", value=f"{current.value or ''}{text}")
        return len(text)

    def key_chord(self, chord: str, **kwargs) -> None:
        del kwargs
        self.calls.append(("key", chord))
        effect = getattr(self, "key_effect", None)
        if callable(effect):
            effect(chord)
            return
        if chord == "ctrl+s":
            self._replace("e5", title="keyed")

    def scroll(self, target, **kwargs) -> None:
        self.calls.append(("scroll", getattr(target, "ref", None), kwargs))
        if getattr(self, "scroll_moves", False):
            row = next(item for item in self.elements if item.ref == "e7")
            bounds = row.bounds
            self._replace("e7", bounds=Bounds(
                bounds.display_id, bounds.x, bounds.y + 40, bounds.width, bounds.height,
            ))

    def set_value(self, element: Element, value: str):
        self.calls.append(("set_value", element.ref, value))
        shown = getattr(self, "landed", value)
        if shown is False:
            return False
        self._replace(element.ref, value=shown)
        return shown

    def menu_items(self, app, path):
        del app, path
        return [{"title": "New", "enabled": True, "shortcut": "", "submenu": False, "checked": None}]

    def menu_state(self, app):
        del app
        return dict(self.menu)

    def menu_press(self, app, path):
        del app
        self.menu = {"open": True, "path": [part.strip() for part in path.split(">")]}
        self._replace("e5", title="menu")
        return path.split(">")[-1].strip()

    def menu_close(self, app):
        del app
        path = list(self.menu.get("path") or [])
        self.menu = {"open": False, "path": []}
        return path

    def running_apps(self):
        return list(self.apps)

    def launch_app(self, identifier, activate=None):
        del activate
        self.calls.append(("launch", identifier))
        return None

    def activate_app(self, identifier):
        self.calls.append(("activate", identifier))
        if getattr(self, "focus_lands", True):
            self.front = identifier
        return identifier

    def windows(self):
        return list(self.rows)

    def window_owner(self, window_id: int) -> str:
        for row in self.rows:
            if int(row["window_id"]) == int(window_id):
                return str(row["app"])
        raise ComputerUseError(ErrorCode.APP_NOT_FOUND, "no window", detail={"window_id": window_id})

    def close_window(self, window_id: int) -> None:
        self.calls.append(("close", window_id))
        if getattr(self, "close_removes", True):
            self.rows = [row for row in self.rows if int(row["window_id"]) != int(window_id)]

    def move_window(self, window_id: int, x: int, y: int) -> None:
        self.calls.append(("move", window_id, x, y))
        if not self.move_lands:
            return
        for row in self.rows:
            if int(row["window_id"]) == int(window_id):
                row["bounds"] = {**row["bounds"], "x": x, "y": y}

    def minimize_window(self, window_id: int) -> None:
        self.calls.append(("minimize", window_id))


def _runtime(tmp_path, driver: TreeDriver):
    store = safety.PermissionStore(tmp_path / "perm.json")
    store.set_tier(APP, safety.Tier.FULL)
    runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)
    runtime.desktop_snapshot(APP)
    return runtime


@pytest.mark.parametrize("ref,front_id,owner_pid,covered", [
    ("editor", 2, 4242, True),
    ("preferences-control", 2, 4242, False),
    ("editor", 1, 4242, False),
    ("editor", 2, 9000, False),  # Another app preserves the existing AX click path.
    ("editor", 2, None, False),  # An unknown owner is not evidence of a cover.
])
def test_linux_same_app_window_cover_before_ax_click(tmp_path, monkeypatch, ref, front_id, owner_pid, covered):
    from a11y_computer_use import observe
    from a11y_computer_use.drivers import _atspi, _linux_system
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = TreeDriver()
    driver.elements = [
        _element("main", "AXWindow", "Geany", bounds=Bounds(0, 0, 0, 800, 600)),
        _element("editor", "AXTextArea", "Document", parent="main", editable=True,
                 bounds=Bounds(0, 0, 0, 800, 600)),
        _element("preferences", "AXDialog", "Preferences", bounds=Bounds(0, 100, 100, 600, 400)),
        _element("preferences-control", "AXButton", "Close", parent="preferences",
                 clickable=True, bounds=Bounds(0, 350, 280, 100, 40)),
    ]
    linux = LinuxDriver()
    monkeypatch.setattr(linux, "_run", lambda fn: fn())
    monkeypatch.setattr(linux, "_gtk4_point_untrusted", lambda element, handle: False)
    monkeypatch.setattr(observe, "ax_handle_for", lambda snap, target: target)
    monkeypatch.setattr(_linux_system, "pid_at_point", lambda x, y: owner_pid)
    monkeypatch.setattr(_atspi, "find_root", lambda app, scope: object())
    monkeypatch.setattr(_atspi, "pid_of", lambda root: 4242)
    monkeypatch.setattr(_atspi, "toplevel_raise_target", lambda handle: (
        False, "Geany" if handle == "editor" else "Preferences", 4242, (0, 0, 800, 600),
    ))
    monkeypatch.setattr(_linux_system, "window_id_for_toplevel",
                        lambda **kw: 1 if kw["title"] == "Geany" else 2)
    monkeypatch.setattr(_linux_system, "window_at_point", lambda x, y: {
        "window_id": front_id, "title": "Preferences" if front_id == 2 else "Geany",
    })
    driver.same_app_window_cover = linux.same_app_window_cover

    def press(element):
        driver.calls.append(("press", element.ref))
        return True  # AT-SPI claims success even for the covered textarea.

    driver.press_element = press
    runtime = _runtime(tmp_path, driver)
    if covered:
        with pytest.raises(ComputerUseError) as caught:
            runtime.click(ref)
        assert caught.value.detail["reason"] == "covered"
        assert caught.value.detail["outcome"] == "refused"
        assert "Preferences" in caught.value.message
        assert "clicked" not in caught.value.message
        assert not driver.calls
    else:
        assert runtime.click(ref).startswith("clicked")
        assert driver.calls == [("press", ref)]


def test_action_result_equals_its_sentence() -> None:
    result = outcome.ActionResult(
        "typed 5 characters", outcome="confirmed", next=(), evidence="read back 'hello'",
    )
    assert result == "typed 5 characters"
    assert result.startswith("typed 5")
    assert isinstance(result, str)
    assert result.as_dict() == {
        "outcome": "confirmed",
        "next": [],
        "evidence": "read back 'hello'",
    }
    with pytest.raises(ValueError):
        outcome.ActionResult("x", outcome="maybe")


def test_judge_uses_readback_state_and_process_death() -> None:
    assert outcome.judge(changed=True) == ("confirmed", "the accessibility state changed")
    assert outcome.judge(changed=False) == ("suspected_noop", "the accessibility state did not change")
    assert outcome.judge(changed=None)[0] == "unverifiable"
    assert outcome.judge(
        changed=True, requested="hello", readback="hello",
    ) == ("confirmed", "read back 'hello'")
    assert outcome.judge(
        changed=True, requested="a", readback="hella", before_value="hell",
    )[0] == "confirmed"
    replaced = outcome.judge(
        changed=True,
        requested="WORD",
        readback="Hello WORD start",
        before_value="Hello world start",
        selection=(6, 11),
    )
    assert replaced == ("confirmed", "read back 'Hello WORD start'")
    assert outcome.judge(
        changed=True,
        requested="WORD",
        readback="Hello WORD start",
        before_value="Hello world start",
    )[0] == "partial"
    assert outcome.judge(
        changed=True,
        requested="WORD",
        readback="Hello WORD startSecond line",
        before_value="Hello world start",
        expected="Hello WORD startSecond line",
    )[0] == "confirmed"
    assert outcome.judge(
        changed=True, requested="hello", readback="help", before_value="",
    )[0] == "partial"
    assert outcome.judge(
        changed=False, requested="hello", readback="", before_value="",
    )[0] == "suspected_noop"
    died = outcome.judge(changed=True, process_died=True)
    assert died == ("partial", "the target process exited after the action")
    assert outcome.suggest_next("click", "confirmed") == ()
    assert outcome.suggest_next("click", "partial", process_died=True) == ("ref", "foreground")
    assert outcome.suggest_next("set_value", "suspected_noop")[0] == "ref"
    assert "cdp" in outcome.suggest_next("select", "partial", browser=True)
    assert outcome.next_for_error("stale_ref", {}) == ("ref", "coordinates", "keyboard")
    hidden = outcome.next_for_error("unsupported", {"reason": "not_showing"})
    assert hidden[0] == "foreground"
    assert outcome.next_for_error("secure_field", {}) == ()
    assert outcome.next_for_error("focus_lost", {}) == ("ref", "cdp")
    assert "keyboard" not in outcome.next_for_error(
        "focus_lost", {"reason": "focus_lost", "outcome": "refused"},
    )
    refused = outcome.refused_result(
        text="unsupported: e2 is not showing",
        code="unsupported",
        message="e2 is not showing",
        detail={"reason": "not_showing", "outcome": "refused", "next": ["foreground", "ref"]},
    )
    assert refused.outcome == "refused"
    assert refused.next == ("foreground", "ref")
    assert "re-observe" not in refused


def test_click_that_changes_the_tree_is_confirmed(tmp_path) -> None:
    driver = TreeDriver()
    runtime = _runtime(tmp_path, driver)
    result = runtime.click("e2")
    assert result == "clicked e2 (AXButton 'Save')"
    assert result.outcome == "confirmed"
    assert result.next == ()
    assert "changed" in result.evidence


def test_writer_paragraph_click_is_confirmed_only_when_the_caret_lands(tmp_path, monkeypatch) -> None:
    """A Writer click that only changes focus is not confirmed.

    The caret has to be in the paragraph that was clicked. A state change
    alone used to confirm a click that landed in the paragraph above.
    """
    from a11y_computer_use import observe
    from a11y_computer_use.drivers import _atspi

    driver = TreeDriver()
    driver.name = "linux"

    def press(element: Element) -> bool:
        driver.calls.append(("press", element.ref))
        if element.ref == "e4":
            driver._replace("e5", title="focused")
            return True
        return False

    driver.press_element = press  # type: ignore[method-assign]
    monkeypatch.setattr(observe, "ax_handle_for", lambda snapshot_id, ref: object() if ref == "e4" else None)
    monkeypatch.setattr(
        _atspi, "paragraph_click_verdict",
        lambda handle: ("partial", "the caret is not in the target paragraph"),
    )
    runtime = _runtime(tmp_path, driver)
    missed = runtime.click("e4")
    assert missed.outcome == "partial"
    assert missed.evidence == "the caret is not in the target paragraph"
    assert "changed" not in missed.evidence

    monkeypatch.setattr(
        _atspi, "paragraph_click_verdict",
        lambda handle: ("confirmed", "the caret is in the target paragraph"),
    )
    driver._replace("e5", title="ready")
    landed = runtime.click("e4")
    assert landed.outcome == "confirmed"
    assert landed.evidence == "the caret is in the target paragraph"


def test_side_pane_click_is_confirmed_only_when_the_location_changes(tmp_path, monkeypatch) -> None:
    """Selecting the row is not confirmation. The location has to change."""
    from a11y_computer_use import observe
    from a11y_computer_use.drivers import _atspi

    driver = TreeDriver()
    driver.name = "linux"

    def press(element: Element) -> bool:
        driver.calls.append(("press", element.ref))
        driver._replace("e5", title="selected")
        return True

    driver.press_element = press  # type: ignore[method-assign]
    monkeypatch.setattr(observe, "ax_handle_for", lambda snapshot_id, ref: object() if ref == "e4" else None)
    monkeypatch.setattr(_atspi, "navigating_folder_row", lambda handle: True)
    monkeypatch.setattr(_atspi, "folder_location", lambda handle: "/home/ubuntu/")
    monkeypatch.setattr(
        _atspi, "folder_navigation_verdict",
        lambda handle, before: ("partial", "the row was selected but the location did not change"),
    )
    runtime = _runtime(tmp_path, driver)
    missed = runtime.click("e4")
    assert missed.outcome == "partial"
    assert missed.evidence == "the row was selected but the location did not change"
    assert missed.outcome != "confirmed"

    monkeypatch.setattr(
        _atspi, "folder_navigation_verdict",
        lambda handle, before: ("confirmed", "the location changed"),
    )
    driver._replace("e5", title="opened")
    opened = runtime.click("e4")
    assert opened.outcome == "confirmed"
    assert opened.evidence == "the location changed"


def test_canvas_click_stays_unverifiable_when_the_tree_changes(tmp_path, monkeypatch) -> None:
    """Focus moving onto a canvas is not confirmation.

    The accessibility tree cannot see the pixels. The outcome says so and
    points at crop or a screenshot, even when the tree changed.
    """
    from a11y_computer_use import observe
    from a11y_computer_use.drivers import _atspi

    driver = TreeDriver()
    driver.name = "linux"

    def press(element: Element) -> bool:
        driver.calls.append(("press", element.ref))
        driver._replace("e5", title="focused")
        return True

    driver.press_element = press  # type: ignore[method-assign]
    monkeypatch.setattr(observe, "ax_handle_for", lambda snapshot_id, ref: object() if ref == "e4" else None)
    monkeypatch.setattr(
        _atspi, "opaque_click_verdict",
        lambda handle: (
            "unverifiable",
            "accessibility cannot see inside this canvas or unnamed image, so the click is not confirmed; "
            "verify with crop or a screenshot",
        ),
    )
    runtime = _runtime(tmp_path, driver)
    result = runtime.click("e4")
    assert result.startswith("clicked ")
    assert result.outcome == "unverifiable"
    assert result.outcome != "confirmed"
    assert "crop" in result.evidence and "screenshot" in result.evidence
    assert "changed" not in result.evidence


def test_click_on_an_inert_label_is_suspected_noop(tmp_path) -> None:
    driver = TreeDriver()
    runtime = _runtime(tmp_path, driver)
    result = runtime.click("e4")
    assert result == "clicked e4 (AXStaticText 'Idle label')"
    assert result.outcome == "suspected_noop"
    assert "did not change" in result.evidence
    assert "ref" in result.next
    assert "coordinates" in result.next


def test_second_inert_click_without_a_snapshot_is_suspected_noop(tmp_path) -> None:
    """The second click is judged against the tree just before it.

    The first click changes the tree. A second click with no snapshot in
    between used to compare against that older tree and report confirmed.
    """
    driver = TreeDriver()
    runtime = _runtime(tmp_path, driver)
    first = runtime.click("e2")
    assert first.outcome == "confirmed"
    second = runtime.click("e2")
    assert second == "clicked e2 (AXButton 'Save')"
    assert second.outcome == "suspected_noop"
    assert "did not change" in second.evidence


def _chrome_page(*extra: Element) -> list[Element]:
    """A window, a churning toolbar, and a titled page with a button."""
    return [
        _element("w", "AXWindow", "Outcome Probe - Google Chrome", path=("AXWindow",)),
        _element("bar", "AXToolbar", "", parent="w", path=("AXWindow", "AXToolbar")),
        _element(
            "status", "AXStaticText", "tick", parent="bar",
            path=("AXWindow", "AXToolbar", "AXStaticText"),
        ),
        _element(
            "page", "AXGroup", "Outcome Probe", parent="w",
            path=("AXWindow", "AXGroup"), focused=True,
        ),
        _element(
            "btn", "AXButton", "Div button", parent="page",
            path=("AXWindow", "AXGroup", "AXButton"), clickable=True,
        ),
        _element(
            "label", "AXStaticText", "ready", parent="page",
            path=("AXWindow", "AXGroup", "AXStaticText"),
        ),
        *extra,
    ]


def test_relevant_state_compares_mixed_none_and_text_values() -> None:
    """Two controls can share a name while one value is missing and one is text.

    Sorting those rows used to raise TypeError and abort the click.
    """
    elements = (
        _element("w", "AXWindow", "Demo", path=("AXWindow",)),
        _element("a", "AXButton", "Close", parent="w", path=("AXWindow", "AXButton")),
        _element(
            "b", "AXButton", "Close", parent="w",
            path=("AXWindow", "AXGroup", "AXButton"), checked=False,
        ),
        _element(
            "c", "AXStaticText", "Note", parent="w",
            path=("AXWindow", "AXStaticText"), value="1",
        ),
        _element(
            "d", "AXStaticText", "Note", parent="w",
            path=("AXWindow", "AXGroup", "AXStaticText"),
        ),
    )
    snap = Snapshot(
        "snap", Scope.WINDOW, APP, 1, 0.0, (), elements,
    )
    assert outcome.relevant_state_changed(snap, snap, elements[1]) is False


def test_chrome_churn_outside_the_page_does_not_confirm_a_click(tmp_path) -> None:
    """Toolbar text and document focus are not proof a page button did nothing.

    A label inside the page still confirms. The next click, with the toolbar
    moving again, is suspected_noop.
    """
    driver = TreeDriver()
    driver.elements = _chrome_page()

    def press(element: Element) -> bool:
        driver.calls.append(("press", element.ref))
        status = next(item for item in driver.elements if item.ref == "status")
        driver._replace("status", title="tock" if status.title == "tick" else "tick")
        page = next(item for item in driver.elements if item.ref == "page")
        driver._replace("page", focused=not page.focused)
        if getattr(driver, "save_page", False) and element.ref == "btn":
            driver._replace("label", title="saved")
        if getattr(driver, "focus_button", False) and element.ref == "btn":
            driver._replace("btn", focused=True)
        if getattr(driver, "retitle", False):
            driver._replace("w", title="Other Probe - Google Chrome")
        return True

    driver.press_element = press  # type: ignore[method-assign]
    runtime = _runtime(tmp_path, driver)
    inert = runtime.click("btn")
    assert inert.outcome == "suspected_noop", (inert.outcome, inert.evidence)
    assert "did not change" in inert.evidence

    driver.save_page = True
    changed = runtime.click("btn")
    assert changed.outcome == "confirmed", (changed.outcome, changed.evidence)
    again = runtime.click("btn")
    assert again.outcome == "suspected_noop", (again.outcome, again.evidence)

    driver.save_page = False
    driver.focus_button = True
    runtime.desktop_snapshot(APP)
    focused = runtime.click("btn")
    assert focused.outcome == "confirmed", (focused.outcome, focused.evidence)

    driver.focus_button = False
    driver.retitle = True
    runtime.desktop_snapshot(APP)
    renamed = runtime.click("btn")
    assert renamed.outcome == "confirmed", (renamed.outcome, renamed.evidence)


def test_set_value_in_a_background_window_is_confirmed_from_app_scope(tmp_path) -> None:
    """A field that window scope omits is confirmed from the app-scope read-back.

    The ref comes from an app snapshot. A window-scope reread would not
    contain the field and would report unverifiable. The write is confirmed
    only when that wider read-back equals the request. A different read-back
    is not confirmed. A cover is a separate refusal and does not reach this.
    """
    driver = TreeDriver()
    driver.elements = [
        *driver.elements,
        _element(
            "e9", "AXTextField", "Field A",
            path=("Alpha", "AXTextField"),
            value="", editable=True,
        ),
    ]
    original = driver.snapshot

    def snapshot(scope, app):
        shot = original(scope, app)
        kind = scope.value if isinstance(scope, Scope) else str(scope)
        if kind == "window":
            return dataclasses.replace(
                shot, elements=tuple(el for el in shot.elements if el.ref != "e9"),
            )
        return shot

    driver.snapshot = snapshot  # type: ignore[method-assign]
    runtime = _runtime(tmp_path, driver)
    runtime.desktop_snapshot(APP, scope="app")
    written = runtime.set_value("e9", "alpha-ok")
    assert written == "set e9 = 'alpha-ok'"
    assert written.outcome == "confirmed", (written.outcome, written.evidence)
    assert "alpha-ok" in written.evidence
    field = next(item for item in driver.elements if item.ref == "e9")
    assert field.value == "alpha-ok"
    window = driver.snapshot(Scope.WINDOW, APP)
    assert all(el.ref != "e9" for el in window.elements)

    runtime.desktop_snapshot(APP, scope="app")
    driver.landed = "nope"
    missed = runtime.set_value("e9", "alpha-ok")
    assert missed.outcome != "confirmed"
    assert missed.outcome in {"partial", "suspected_noop"}
    assert "alpha-ok" not in missed.evidence or "nope" in missed.evidence


def test_set_value_on_a_covered_element_is_refused(tmp_path) -> None:
    driver = TreeDriver()
    driver.occlusion = lambda _element, _app: "other"  # type: ignore[attr-defined]
    runtime = _runtime(tmp_path, driver)
    with pytest.raises(ComputerUseError) as exc:
        runtime.set_value("e3", "Ann")
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "covered"
    assert exc.value.detail["outcome"] == "refused"
    assert exc.value.detail["next"][0] == "foreground"
    field = next(item for item in driver.elements if item.ref == "e3")
    assert field.value in (None, "")


def test_click_that_kills_the_process_is_partial(tmp_path) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    driver = TreeDriver()
    driver.name = "linux"
    driver.pid = proc.pid

    def click(target, **_kwargs) -> None:
        # An on-screen Linux button is a pointer click, not DoAction.
        driver.calls.append(("click", getattr(target, "ref", None)))
        driver.dead = True
        proc.kill()
        proc.wait(timeout=2)

    driver.click = click  # type: ignore[method-assign]
    try:
        runtime = _runtime(tmp_path, driver)
        runtime._PROCESS_SETTLE_S = 0.5
        result = runtime.click("e2")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
    assert result == "clicked e2 (AXButton 'Save')"
    assert result.outcome == "partial"
    assert "exited after the action" in result.evidence
    assert result.next == ("ref", "foreground")


def test_a_zombie_process_counts_as_exited() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    assert outcome.pid_alive(proc.pid)
    proc.kill()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and outcome.pid_alive(proc.pid):
        time.sleep(0.02)
    try:
        assert outcome.pid_alive(proc.pid) is False
    finally:
        proc.wait(timeout=2)


def test_a_pid_that_was_never_alive_is_not_a_crash(tmp_path) -> None:
    driver = TreeDriver()
    driver.name = "linux"
    driver.pid = 2**30 + 17
    runtime = _runtime(tmp_path, driver)
    result = runtime.click("e4")
    assert result.outcome == "suspected_noop"


def test_disabled_click_stays_a_raised_refusal(tmp_path) -> None:
    driver = TreeDriver()
    runtime = _runtime(tmp_path, driver)
    with pytest.raises(ComputerUseError) as exc:
        runtime.click("e8")
    assert exc.value.code is ErrorCode.ELEMENT_DISABLED
    marker = outcome.refused_result(
        text="disabled", code=exc.value.code.value, message=exc.value.message, detail=exc.value.detail,
    )
    assert marker.outcome == "refused"
    assert marker.next == ("ref", "keyboard")


def test_type_readback_confirms_and_a_miss_does_not(tmp_path) -> None:
    driver = TreeDriver()
    driver.apply_type = True
    runtime = _runtime(tmp_path, driver)
    result = runtime.type_text("hello")
    assert result == "typed 5 characters"
    assert result.outcome == "confirmed"
    assert "hello" in result.evidence

    driver.apply_type = False
    runtime.desktop_snapshot(APP)
    missed = runtime.type_text("nope")
    assert missed == "typed 4 characters"
    assert missed.outcome == "suspected_noop"
    assert "keyboard" in missed.next


def test_key_confirms_when_the_tree_changes(tmp_path) -> None:
    driver = TreeDriver()
    runtime = _runtime(tmp_path, driver)
    result = runtime.key("ctrl+s")
    assert result == "pressed ctrl+s"
    assert result.outcome == "confirmed"
    runtime.desktop_snapshot(APP)
    quiet = runtime.key("ctrl+a")
    assert quiet == "pressed ctrl+a"
    assert quiet.outcome == "suspected_noop"


def test_key_confirms_text_under_a_tab_group_and_the_selected_cell(tmp_path) -> None:
    """The page fingerprint skips a tab group and does not record selection.

    Mousepad's document is under a tab group, so BackSpace used to look like
    a noop. A spreadsheet Down only moves which cell is selected. Toolbar
    text still does not confirm a key.
    """
    driver = TreeDriver()
    driver.elements = [
        _element("w", "AXWindow", "Untitled 1 - Mousepad", path=("AXWindow",)),
        _element(
            "tab", "AXTabGroup", "", parent="w",
            path=("AXWindow", "AXGroup", "AXTabGroup"),
        ),
        _element(
            "doc", "AXTextArea", "", parent="tab",
            path=("AXWindow", "AXGroup", "AXTabGroup", "AXButton", "AXTextArea"),
            value="ab0", focused=True, editable=True,
        ),
    ]

    def effect(chord: str) -> None:
        if chord == "BackSpace":
            driver._replace("doc", value="ab")
        elif chord == "Down":
            driver._replace("a1", selected=False, focused=False)
            driver._replace("a2", selected=True, focused=True)

    driver.key_effect = effect
    before = driver.snapshot(Scope.WINDOW, APP)
    driver._replace("doc", value="ab")
    after = driver.snapshot(Scope.WINDOW, APP)
    assert outcome.relevant_state_changed(before, after, None) is False
    assert outcome.key_focus_evidence(
        None, None, outcome.key_focus_rows(before), outcome.key_focus_rows(after),
    ) == "the focused text changed"
    driver._replace("doc", value="ab0")

    runtime = _runtime(tmp_path, driver)
    pressed = runtime.key("BackSpace")
    assert pressed.outcome == "confirmed", (pressed.outcome, pressed.evidence)
    assert pressed.evidence == "the focused text changed"

    driver.elements = _chrome_page()

    def churn(_chord: str) -> None:
        status = next(item for item in driver.elements if item.ref == "status")
        driver._replace("status", title="tock" if status.title == "tick" else "tick")

    driver.key_effect = churn
    runtime.desktop_snapshot(APP)
    quiet = runtime.key("F5")
    assert quiet.outcome == "suspected_noop", (quiet.outcome, quiet.evidence)

    driver.elements = [
        _element("w", "AXWindow", "Calc", path=("AXWindow",)),
        _element(
            "a1", "AXCell", "A1", parent="w",
            path=("AXWindow", "AXTable", "AXCell"), selected=True, focused=True,
        ),
        _element(
            "a2", "AXCell", "A2", parent="w",
            path=("AXWindow", "AXTable", "AXCell"),
        ),
    ]
    driver.key_effect = effect
    # These cells are not clickable or editable, so the snapshot takes the
    # vision handoff and captures the display. macOS has that engine; other
    # platforms use a scripted one so the same capture runs here.
    if runtime._ocr_engine is None:
        from a11y_computer_use import ocr

        runtime._ocr_engine = ocr.FakeOcr()
    runtime.desktop_snapshot(APP)
    moved = runtime.key("Down")
    assert moved.outcome == "confirmed", (moved.outcome, moved.evidence)
    assert moved.evidence == "the focused cell changed"

    assert outcome.key_focus_evidence(
        ("ab", 3, 3, 3, None), ("ab", 2, 2, 2, None), (), (),
    ) == "the caret moved"
    assert outcome.key_focus_evidence(
        ("ab", 2, 0, 2, None), ("ab", 2, 2, 2, None), (), (),
    ) == "the selection changed"
    assert outcome.key_focus_evidence(
        (None, None, None, None, "A1"), (None, None, None, None, "A2"), (), (),
    ) == "the focused cell changed"
    assert outcome.key_focus_evidence(None, ("ab", 1, 1, 1, None), (), ()) is None


def test_scroll_uses_bounds_and_set_value_uses_readback(tmp_path) -> None:
    driver = TreeDriver()
    driver.scroll_moves = True
    runtime = _runtime(tmp_path, driver)
    moved = runtime.scroll("e7", dy=3)
    assert moved.startswith("scrolled e7")
    assert moved.outcome == "confirmed"

    driver.scroll_moves = False
    runtime.desktop_snapshot(APP)
    still = runtime.scroll("e7", dy=3)
    assert still.outcome == "suspected_noop"

    written = runtime.set_value("e3", "Ann")
    assert written == "set e3 = 'Ann'"
    assert written.outcome == "confirmed"
    assert "Ann" in written.evidence

    runtime.desktop_snapshot(APP)
    driver.landed = "An"
    partial = runtime.set_value("e3", "Ann")
    assert partial.outcome == "partial"
    assert "Ann" in partial.evidence


def test_select_is_judged_like_set_value(tmp_path) -> None:
    driver = TreeDriver()
    runtime = _runtime(tmp_path, driver)
    result = runtime.call_tool("select", {"ref": "e6", "value": "Blue"})
    assert result == "set e6 = 'Blue'"
    assert result.outcome == "confirmed"
    assert result.next == ()


def test_menu_app_and_window_carry_outcomes(tmp_path) -> None:
    driver = TreeDriver()
    runtime = _runtime(tmp_path, driver)
    listed = runtime.menu(APP, "File", action="list")
    assert listed.startswith("[")
    assert listed.outcome == "confirmed"
    assert listed.next == ()

    pressed = runtime.menu(APP, "File > New")
    assert pressed == f"pressed menu item 'New' in {APP}"
    assert pressed.outcome == "confirmed"

    closed = runtime.menu(APP, action="close")
    assert closed.outcome == "confirmed"
    none_open = runtime.menu(APP, action="close")
    assert none_open.outcome == "suspected_noop"
    assert "no menu was open" in none_open.evidence

    apps = runtime.app("list")
    assert apps.outcome == "confirmed"
    launched = runtime.app("launch", APP)
    assert launched == f"launched {APP}"
    assert launched.outcome == "confirmed"

    focused = runtime.app("focus", APP)
    assert focused == f"focused {APP}"
    assert focused.outcome == "confirmed"

    runtime.QUIT_SETTLE_S = 0
    driver.apps = []
    quit_result = runtime.app("quit", APP)
    assert quit_result == f"quit {APP}"
    assert quit_result.outcome == "confirmed"

    moved = runtime.window("move", window_id=7, x=8, y=9)
    assert moved == "moved window 7 to (8, 9) (demo)"
    assert moved.outcome == "confirmed"
    driver.move_lands = False
    missed_move = runtime.window("move", window_id=7, x=40, y=50)
    assert missed_move.outcome == "partial"
    driver.move_lands = True
    driver.close_removes = False
    stayed = runtime.window("close", window_id=7)
    assert stayed == "closed window 7 (demo)"
    assert stayed.outcome == "suspected_noop"
    driver.close_removes = True
    gone = runtime.window("close", window_id=7)
    assert gone.outcome == "confirmed"
    driver.rows = [{
        "window_id": 7, "app": APP, "title": "Demo", "on_screen": True,
        "bounds": {"display_id": 0, "x": 8, "y": 9, "width": 300, "height": 200},
    }]
    showing = runtime.window("minimize", window_id=7)
    assert showing.outcome == "suspected_noop"


@pytest.mark.anyio
async def test_mcp_click_keeps_the_sentence_and_publishes_structured_outcome(tmp_path) -> None:
    from mcp.shared.memory import create_connected_server_and_client_session as client_session

    driver = TreeDriver()
    runtime = _runtime(tmp_path, driver)
    mcp = server.build_server(runtime=runtime)
    async with client_session(mcp) as client:
        result = await client.call_tool("click", {"ref": "e2"})
    assert result.isError is False
    assert result.content[0].text == "clicked e2 (AXButton 'Save')"
    structured = result.structuredContent
    assert structured["outcome"] == "confirmed"
    assert structured["next"] == []
    assert structured["evidence"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_hidden_ref_click_does_not_become_focus(tmp_path) -> None:
    """A hidden ref stays that click. Focusing the app is not the same intent.

    The error names the refusal. The model can ask for focus on a later turn.
    """
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    runtime = FakeRuntime(elements)

    def call_tool(name, params, confirm=None):
        del confirm
        runtime.calls.append((name, dict(params)))
        if name == "click":
            raise ComputerUseError(
                ErrorCode.UNSUPPORTED,
                "e2 (AXButton 'Save') is not showing",
                detail={
                    "reason": "not_showing",
                    "outcome": "refused",
                    "next": ["foreground", "ref"],
                    "evidence": "e2 is still in the tree but its document is not showing",
                },
            )
        if name == "app":
            return outcome.ActionResult(
                "focused demo", outcome="confirmed", next=(), evidence="demo is frontmost",
            )
        return outcome.ActionResult(f"{name} ok", outcome="confirmed", next=(), evidence="ok")

    runtime.call_tool = call_tool  # type: ignore[method-assign]
    result, events, runtime, _agent = run(
        ScriptedModel([
            turn(ToolCall("click", {"ref": "e2"})),
            turn(done("saved", [{"element": {"role": "AXButton", "name": "Save"}}])),
        ]),
        elements,
        runtime=runtime,
        trace_dir=tmp_path,
    )
    assert all(name != "app" for name, _params in runtime.calls)
    assert runtime.calls[0] == ("click", {"ref": "e2"})
    finished = next(
        event for event in events
        if event.type == "step_finished" and event.data.get("index") == 1
    )
    assert finished.data["verified"] is False
    assert finished.data["error"] and "not showing" in finished.data["error"]
    assert result.step_log[0].error and "not showing" in result.step_log[0].error
    assert result.status == "success"


def test_agent_escalates_a_noop_and_counts_it_as_stuck(tmp_path) -> None:
    save = el(
        "e2", "AXButton", "Save", parent="e1", clickable=True, focused=True,
        bounds=Bounds(0, 20, 30, 80, 24),
    )
    clock = el("clock", "AXStaticText", "t0", parent="e1")
    elements = window(save, clock)
    runtime = FakeRuntime(elements)
    feedback: list[str] = []

    def call_tool(name, params, confirm=None):
        del confirm
        runtime.calls.append((name, dict(params)))
        return outcome.ActionResult(
            "clicked e2",
            outcome="suspected_noop",
            next=("coordinates", "keyboard", "foreground"),
            evidence="the accessibility state did not change",
        )

    def snapshot(app, **kwargs):
        del app, kwargs
        runtime.tick = getattr(runtime, "tick", 0) + 1
        runtime.elements = [
            dataclasses.replace(item, title=f"t{runtime.tick}") if item.ref == "clock" else item
            for item in runtime.elements
        ]
        runtime._current = Snapshot(
            "snap", Scope.WINDOW, "demo", 1, 0.0, (), tuple(runtime.elements),
        )
        return f"screen {runtime.tick}"

    runtime.call_tool = call_tool  # type: ignore[method-assign]
    runtime.desktop_snapshot = snapshot  # type: ignore[method-assign]

    def script(messages):
        feedback.extend(
            message.content for message in messages if isinstance(message.content, str)
        )
        return turn(ToolCall("click", {"ref": "e2"}))

    result, events, runtime, _agent = run(
        ScriptedModel(script),
        elements,
        runtime=runtime,
        trace_dir=tmp_path,
        max_steps=6,
        max_replans=0,
    )
    assert result.reason == "stuck"
    assert runtime.calls[0] == ("click", {"ref": "e2"})
    assert runtime.calls[1][0] == "click"
    assert "x" in runtime.calls[1][1]
    assert runtime.calls[2] == ("key", {"chord": "Return"})
    assert any("outcome: suspected_noop" in text for text in feedback)
    assert any(event.type == "stuck" and event.data["terminal"] is True for event in events)


def _billing_tree(*, expanded: bool = False) -> list[Element]:
    """Chrome's line box: the static text contains the button, and is wider than it."""
    line = Bounds(0, 59, 151, 965, 22)
    button = Bounds(0, 59, 153, 95, 20)
    return [
        _element("w", "AXWindow", "cuabilling", path=("AXWindow",), bounds=Bounds(0, 0, 0, 1000, 800)),
        _element(
            "page", "AXGroup", "cuabilling", parent="w",
            path=("AXWindow", "AXGroup"), bounds=Bounds(0, 52, 133, 900, 700), focused=True,
        ),
        _element(
            "line", "AXStaticText", "Billing section", parent="page",
            path=("AXWindow", "AXGroup", "AXStaticText"), value="Billing section", bounds=line,
        ),
        _element(
            "btn", "AXButton", "Billing section", parent="line",
            path=("AXWindow", "AXGroup", "AXStaticText", "AXButton"),
            clickable=True, expanded=expanded, bounds=button,
        ),
        _element(
            "link", "AXLink", "Link text", parent="page",
            path=("AXWindow", "AXGroup", "AXLink"), clickable=True,
            bounds=Bounds(0, 139, 190, 58, 18),
        ),
        _element(
            "linktext", "AXStaticText", "Link text", parent="link",
            path=("AXWindow", "AXGroup", "AXLink", "AXStaticText"), value="Link text",
            bounds=Bounds(0, 139, 190, 58, 18),
        ),
    ]


def test_statictext_click_activates_the_button_inside_the_line(tmp_path) -> None:
    """A line-wide static text is not the button. The click activates the button.

    The text center sits past the button. Reporting ``clicked`` for that point
    left the control collapsed. The button's expanded state has to change, and
    a press that does not change it is not ``clicked``.
    """
    driver = TreeDriver()
    driver.elements = _billing_tree()

    def press(element: Element) -> bool:
        driver.calls.append(("press", element.ref))
        if element.ref == "btn":
            driver._replace("btn", expanded=True)
            return True
        if element.ref == "link":
            driver._replace("link", title="Link text opened")
            return True
        return False

    driver.press_element = press  # type: ignore[method-assign]
    runtime = _runtime(tmp_path, driver)
    result = runtime.click("line")
    assert result.startswith("clicked line")
    assert "activated btn" in result
    assert result.outcome == "confirmed"
    assert [call[1] for call in driver.calls if call[0] == "press"] == ["btn"]
    assert all(call[0] != "click" for call in driver.calls)

    link = runtime.click("linktext")
    assert link.startswith("clicked linktext")
    assert "activated link" in link
    assert link.outcome == "confirmed"

    driver.elements = _billing_tree()
    runtime.desktop_snapshot(APP)

    def press_without_effect(element: Element) -> bool:
        driver.calls.append(("press", element.ref))
        return element.ref == "btn"

    driver.press_element = press_without_effect  # type: ignore[method-assign]
    with pytest.raises(ComputerUseError) as exc:
        runtime.click("line")
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "not_activated"
    assert exc.value.detail["control"] == "btn"
    assert not str(exc.value).startswith("clicked")


def test_linux_text_click_points_at_the_button_not_the_line(tmp_path) -> None:
    """On Linux a button ref is a pointer click. The text ref uses that box."""
    driver = TreeDriver()
    driver.name = "linux"
    driver.elements = _billing_tree()

    def click(target, **kwargs) -> None:
        del kwargs
        driver.calls.append(("click", getattr(target, "ref", None)))
        if getattr(target, "ref", None) == "btn":
            driver._replace("btn", expanded=True)

    driver.click = click  # type: ignore[method-assign]
    driver.press_element = lambda element: False  # type: ignore[method-assign]
    runtime = _runtime(tmp_path, driver)
    result = runtime.click("line")
    assert result.outcome == "confirmed"
    assert ("click", "btn") in driver.calls
    assert ("click", "line") not in driver.calls


def test_statictext_over_two_controls_is_not_clicked(tmp_path) -> None:
    driver = TreeDriver()
    driver.elements = _billing_tree() + [
        _element(
            "other", "AXButton", "Other", parent="line",
            path=("AXWindow", "AXGroup", "AXStaticText", "AXButton"),
            clickable=True, bounds=Bounds(0, 200, 153, 40, 20),
        ),
    ]
    runtime = _runtime(tmp_path, driver)
    with pytest.raises(ComputerUseError) as exc:
        runtime.click("line")
    assert exc.value.detail["reason"] == "ambiguous_control"
    assert "btn" in exc.value.detail["controls"] and "other" in exc.value.detail["controls"]
    assert driver.calls == []
    assert not str(exc.value).startswith("clicked")


def test_confirmed_outcome_verifies_without_a_digest_change(tmp_path) -> None:
    elements = window(el("e2", "AXButton", "Save", parent="e1", clickable=True))
    runtime = FakeRuntime(elements)

    def call_tool(name, params, confirm=None):
        del confirm
        runtime.calls.append((name, dict(params)))
        return outcome.ActionResult(
            "clicked e2", outcome="confirmed", next=(), evidence="the accessibility state changed",
        )

    runtime.call_tool = call_tool  # type: ignore[method-assign]
    result, events, _runtime, agent = run(
        ScriptedModel([
            turn(ToolCall("click", {"ref": "e2"})),
            turn(done("saved", [{"element": {"role": "AXButton", "name": "Save"}}])),
        ]),
        elements,
        runtime=runtime,
        trace_dir=tmp_path,
    )
    click_done = next(event for event in events if event.type == "step_finished" and event.data["index"] == 1)
    assert click_done.data["verified"] is True
    assert agent._levels.get(("click", "e2"), 0) == 0
    assert agent._hints == {}
    assert result.status == "success"
    assert result.step_log[0].verified is True


def _welcome_tree(*, expanded: bool = False, dialog: bool = True, cover: bool = True):
    """A Search button and a welcome dialog that can sit over its center.

    The button center is (100, 94). A covering dialog owns 30 lines, so the
    dialog and those lines are 31 nodes.
    """
    search = _element(
        "search", "AXButton", "Search (Ctrl+Shift+F)", parent="main",
        path=("AXWindow", "AXButton"), clickable=True, expanded=expanded,
        bounds=Bounds(0, 40, 80, 120, 28),
    )
    elements = [
        _element(
            "main", "AXWindow", "Visual Studio Code", path=("AXWindow",),
            bounds=Bounds(0, 0, 0, 800, 600),
        ),
        search,
        _element(
            "page", "AXGroup", "Editor", parent="main", path=("AXWindow", "AXGroup"),
            bounds=Bounds(0, 0, 120, 800, 480),
        ),
    ]
    if dialog:
        box = Bounds(0, 20, 40, 400, 300) if cover else Bounds(0, 500, 500, 80, 40)
        elements.append(_element(
            "welcome", "AXDialog", "Welcome", parent="main",
            path=("AXWindow", "AXDialog"), bounds=box,
        ))
        for index in range(30):
            elements.append(_element(
                f"d{index}", "AXStaticText", f"line {index}", parent="welcome",
                path=("AXWindow", "AXDialog", "AXStaticText"),
                bounds=Bounds(0, 30, 50 + index, 200, 12),
            ))
    return elements


def _snap(elements: list[Element]) -> Snapshot:
    return Snapshot("snap", Scope.WINDOW, APP, 1, 0.0, (), tuple(elements))


def test_occluded_click_effect_names_the_covering_dialog() -> None:
    """Closing a dialog that covers Search is not a change to Search.

    A dialog that does not contain the center, a target that expands, and a
    button inside the dialog are ordinary effects.
    """
    covered = _welcome_tree()
    search = next(item for item in covered if item.ref == "search")
    opened = _welcome_tree(dialog=False)
    assert outcome.occluded_click_effect(_snap(covered), _snap(opened), search) == (
        "covered by 'Welcome'"
    )
    expanded = _welcome_tree(expanded=True, dialog=False)
    assert outcome.occluded_click_effect(_snap(covered), _snap(expanded), search) is None
    aside = _welcome_tree(cover=False)
    assert outcome.occluded_click_effect(_snap(aside), _snap(opened), search) is None
    assert outcome.occluded_click_effect(_snap(opened), _snap(covered), search) is None

    dismiss = _element(
        "dismiss", "AXButton", "Dismiss", parent="welcome",
        path=("AXWindow", "AXDialog", "AXButton"), clickable=True,
        bounds=Bounds(0, 40, 60, 80, 24),
    )
    before = _welcome_tree() + [dismiss]
    assert outcome.occluded_click_effect(_snap(before), _snap(opened), dismiss) is None

    untitled = _welcome_tree()
    blank = next(item for item in untitled if item.ref == "welcome")
    untitled = [dataclasses.replace(blank, title="") if item.ref == "welcome" else item for item in untitled]
    assert outcome.occluded_click_effect(_snap(untitled), _snap(opened), search) == "covered by dialog"


def test_covered_ref_click_is_not_clicked_and_the_point_stays(tmp_path) -> None:
    """The pointer still hits Search. The welcome dialog is what changed.

    An explicit x/y click is the same point. A button inside the dialog, a
    button the dialog does not cover, and a Search click that expands Search
    stay clicks.
    """
    driver = TreeDriver()
    driver.name = "linux"
    driver.elements = _welcome_tree()
    sent: list = []

    def without_welcome(elements: list[Element]) -> list[Element]:
        return [
            item for item in elements
            if item.ref not in {"welcome", "dismiss"} and item.parent != "welcome"
        ]

    def click(target, **kwargs) -> None:
        del kwargs
        sent.append(target)
        ref = getattr(target, "ref", None)
        if ref == "search":
            driver.elements = without_welcome(driver.elements)
            if getattr(driver, "expand_search", False):
                driver._replace("search", expanded=True)
        elif ref == "dismiss" or getattr(target, "x", None) == 100:
            driver.elements = without_welcome(driver.elements)

    driver.click = click  # type: ignore[method-assign]
    driver.press_element = lambda element: False  # type: ignore[method-assign]
    runtime = _runtime(tmp_path, driver)
    covered = runtime.click("search", verify=True)
    assert covered.outcome == "suspected_noop"
    assert covered.evidence == "covered by 'Welcome'"
    assert not str(covered).startswith("clicked")
    assert "did not reach the target" in covered
    assert "+0 -31 ~0" in covered
    assert sent[0].ref == "search"
    assert (sent[0].bounds.x, sent[0].bounds.y, sent[0].bounds.width, sent[0].bounds.height) == (
        40, 80, 120, 28,
    )

    driver.elements = _welcome_tree() + [
        _element(
            "dismiss", "AXButton", "Dismiss", parent="welcome",
            path=("AXWindow", "AXDialog", "AXButton"), clickable=True,
            bounds=Bounds(0, 40, 60, 80, 24),
        ),
    ]
    runtime.desktop_snapshot(APP)
    dismissed = runtime.click("dismiss")
    assert str(dismissed).startswith("clicked dismiss")
    assert dismissed.outcome == "confirmed"
    assert sent[-1].ref == "dismiss"

    driver.elements = _welcome_tree(cover=False)
    runtime.desktop_snapshot(APP)
    aside = runtime.click("search")
    assert str(aside).startswith("clicked search")
    assert aside.outcome == "confirmed"

    driver.elements = _welcome_tree()
    driver.expand_search = True
    runtime.desktop_snapshot(APP)
    reached = runtime.click("search")
    assert str(reached).startswith("clicked search")
    assert reached.outcome == "confirmed"
    driver.expand_search = False

    driver.elements = _welcome_tree()
    runtime.desktop_snapshot(APP)
    point = runtime.click(x=100, y=94)
    assert str(point).startswith("clicked (100, 94)")
    assert sent[-1] == Point(display_id=0, x=100, y=94)


@pytest.mark.parametrize("operation", ["click", "crop"])
@pytest.mark.parametrize("title", ["", "Overlay panel"])
def test_popover_cover_refuses_before_input_or_capture(tmp_path, operation, title):
    """#609: a top-layer cover must not receive a covered ref's click/crop."""
    driver = TreeDriver()
    driver.elements.append(_element(
        "overlay", "AXPopover", title, parent="e1", bounds=Bounds(0, 0, 0, 200, 100),
    ))
    runtime = _runtime(tmp_path, driver)
    with pytest.raises(ComputerUseError) as caught:
        getattr(runtime, operation)("e2")
    assert caught.value.code == ErrorCode.NOT_VISIBLE
    assert caught.value.detail["reason"] == "covered"
    assert "covered by popover" in caught.value.message
    assert driver.calls == []


@pytest.mark.parametrize("case", ["ancestor", "self", "aside", "missing_bounds"])
def test_popover_cover_requires_unrelated_measured_overlap(tmp_path, case):
    driver = TreeDriver()
    cover = _element("overlay", "AXPopover", "", bounds=Bounds(0, 0, 0, 200, 100))
    target = driver.elements[1]
    if case == "ancestor":
        target = dataclasses.replace(target, parent=cover.ref)
    elif case == "self":
        target = cover
    elif case == "aside":
        cover = dataclasses.replace(cover, bounds=Bounds(0, 200, 200, 100, 100))
    else:
        cover = dataclasses.replace(cover, bounds=None)
    driver.elements = [driver.elements[0], cover, target] if case != "self" else [cover]
    runtime = _runtime(tmp_path, driver)
    assert runtime._popover_cover(target) is None


def test_linux_popover_cover_reads_newly_opened_top_layer(tmp_path):
    """A cover opened after the issued snapshot still blocks the click."""
    driver = TreeDriver()
    driver.elements.append(_element("web", "AXWebArea", "Page"))
    runtime = _runtime(tmp_path, driver)
    driver.name = "linux"
    driver.elements.append(_element(
        "overlay", "AXPopover", "", bounds=Bounds(0, 0, 0, 200, 100),
    ))
    assert runtime._popover_cover(runtime._current.element("e2"), refresh=True) == "popover"
    driver.elements = [el for el in driver.elements if el.ref != "overlay"]
    assert runtime._popover_cover(runtime._current.element("e2"), refresh=True) is None


@pytest.mark.parametrize("owner_pid,front_id,covered", [
    (4242, 2, True),
    (4242, 1, False),
    (9000, 2, False),
    (None, 2, False),
    (4242, None, False),
])
def test_linux_snapshot_and_find_mark_same_app_window_cover(
    monkeypatch, owner_pid, front_id, covered,
):
    """Synthetic X11 ownership: only the covered toolbar gets a warning."""
    from a11y_computer_use import observe
    from a11y_computer_use.drivers import _atspi, _linux_system
    from a11y_computer_use.drivers.linux import LinuxDriver

    elements = (
        _element("main", "AXWindow", "Archive"),
        _element("toolbar", "AXButton", "Extract", parent="main", clickable=True,
                 note="existing hint"),
        _element("unknown", "AXButton", "Extract", parent="main", clickable=True,
                 bounds=None),
        _element("dialog", "AXDialog", "Extract files"),
        _element("confirm", "AXButton", "Extract", parent="dialog", clickable=True,
                 bounds=Bounds(0, 350, 280, 100, 40)),
    )
    source = Snapshot("snap", Scope.WINDOW, "xarchiver", 4242, 0, (), elements)
    driver = LinuxDriver()
    # Bypass the native walk, retaining the actual post-walk annotation and
    # same-app click gate. Native calls below are scripted ownership evidence.
    monkeypatch.setattr(driver, "_gtk4_point_untrusted", lambda el, handle: False)
    monkeypatch.setattr(observe, "ax_handle_for", lambda snap, ref: ref)
    monkeypatch.setattr(_linux_system, "pid_at_point", lambda x, y: owner_pid)
    monkeypatch.setattr(_linux_system, "window_id_for_toplevel",
                        lambda **kw: 1 if kw["title"] == "Archive" else 2)
    monkeypatch.setattr(_linux_system, "window_at_point", lambda x, y: (
        {"window_id": 2 if x > 300 else front_id, "title": "Extract files"}
        if front_id else None
    ))
    # Discovery now shares one native stacking read across all controls.
    monkeypatch.setattr(_linux_system, "window_cover_rows", lambda: ([
        {"window_id": 1, "title": "Archive", "pid": 4242, "on_screen": True,
         "bounds": {"x": 0, "y": 0, "width": 800, "height": 600}},
        {"window_id": 2, "title": "Extract files", "pid": owner_pid, "on_screen": True,
         "bounds": {"x": 0 if front_id == 2 else 300, "y": 0,
                    "width": 500, "height": 600}},
    ] if front_id else []))
    # _run is also used by the cover check, so dispatch just that native call.
    def run(fn):
        if fn.__name__ == "_do":
            return source
        return fn()

    monkeypatch.setattr(driver, "_run", run)
    monkeypatch.setattr(_atspi, "toplevel_raise_target", lambda handle: (
        False, "Archive" if handle in {"toolbar", "main"} else "Extract files",
        4242, (0, 0, 800, 600),
    ))
    snap = driver._snapshot_root("xarchiver", object(), Scope.WINDOW)
    matches = observe.find_elements(snap, text="Extract", role="AXButton")
    toolbar = snap.element("toolbar")
    assert ("covered by 'Extract files'" in (toolbar.note or "")) is covered
    assert "existing hint" in toolbar.note
    assert snap.element("confirm").note is None
    assert snap.element("unknown").note is None
    assert toolbar.bounds == elements[1].bounds
    assert toolbar.clickable
    assert [el.ref for el in matches] == ["toolbar", "unknown", "confirm"]
    for rendered in (observe.render_text(snap), observe.render_text(snap, mode="interactive"),
                     observe.render_matches(snap, matches)):
        assert ("covered by 'Extract files'" in rendered) is covered


@pytest.mark.parametrize("operation", ["click", "crop"])
def test_linux_native_popover_guard_does_not_snapshot(tmp_path, monkeypatch, operation):
    """#717: the pre-input guard must not walk a native app again."""
    driver = TreeDriver()
    runtime = _runtime(tmp_path, driver)
    driver.name = "linux"
    original = runtime._popover_cover
    checked = []

    def guard(element, *, refresh=False):
        if refresh:
            with monkeypatch.context() as patch:
                def unexpected_snapshot(*args):
                    pytest.fail("native popover guard requested a full snapshot")
                patch.setattr(driver, "snapshot", unexpected_snapshot)
                result = original(element, refresh=True)
            checked.append(element.ref)
            return result
        return original(element)

    monkeypatch.setattr(runtime, "_popover_cover", guard)
    getattr(runtime, operation)("e2")
    assert checked == ["e2"]


@pytest.mark.parametrize("source", ["browser", "web_area", "document", "popover"])
def test_linux_plausible_popover_refresh_refuses_covered_click(tmp_path, monkeypatch, source):
    if source == "browser":
        monkeypatch.setattr(sys.modules[__name__], "APP", "chromium")
    driver = TreeDriver()
    if source == "web_area":
        driver.elements.append(_element("web", "AXWebArea", "Page"))
    elif source == "popover":
        driver.elements.append(_element(
            "aside", "AXPopover", "", bounds=Bounds(0, 400, 400, 100, 100),
        ))
    runtime = _runtime(tmp_path, driver)
    driver.name = "linux"
    if source == "document":
        monkeypatch.setattr(driver, "element_document_url", lambda element: "https://example.test/", raising=False)
    driver.elements.append(_element(
        "overlay", "AXPopover", "", bounds=Bounds(0, 0, 0, 200, 100),
    ))
    with pytest.raises(ComputerUseError) as caught:
        runtime.click("e2")
    assert caught.value.code == ErrorCode.NOT_VISIBLE
    assert caught.value.detail["reason"] == "covered"
    assert driver.calls == []


def test_linux_discovery_cover_work_scales_with_windows_and_refreshes(monkeypatch):
    """#718: many controls share one stacking read and one lookup per window."""
    from a11y_computer_use import observe
    from a11y_computer_use.drivers import _atspi, _linux_system
    from a11y_computer_use.drivers.linux import LinuxDriver

    elements = (
        _element("main", "AXWindow", "Archive"),
        _element("dialog", "AXDialog", "Extract files"),
        *(_element(f"button{i}", "AXButton", "Extract", parent="main", clickable=True)
          for i in range(100)),
        _element("confirm", "AXButton", "Extract", parent="dialog", clickable=True),
    )
    source = Snapshot("snap", Scope.WINDOW, "xarchiver", 4242, 0, (), elements)
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: source if fn.__name__ == "_do" else fn())
    monkeypatch.setattr(driver, "_gtk4_point_untrusted", lambda el, handle: False)
    monkeypatch.setattr(observe, "ax_handle_for", lambda snap, ref: ref)
    native_reads = []

    def toplevel(handle):
        native_reads.append(handle)
        assert handle in {"main", "dialog"}
        return False, "Archive" if handle == "main" else "Extract files", 4242, (0, 0, 800, 600)

    monkeypatch.setattr(_atspi, "toplevel_raise_target", toplevel)
    reads = []
    rows = [
        {"window_id": wid, "title": title, "pid": 4242, "on_screen": True,
         "bounds": {"x": 0, "y": 0, "width": 800, "height": 600}}
        for wid, title in [(1, "Archive"), (2, "Extract files")]
    ]

    def stacking():
        reads.append(True)
        return list(rows)

    def unexpected(*args, **kwargs):
        pytest.fail("discovery must not repeat a full window/process scan or point probe")

    monkeypatch.setattr(_linux_system, "window_cover_rows", stacking)
    monkeypatch.setattr(_linux_system, "windows", unexpected)
    monkeypatch.setattr(_linux_system, "pid_at_point", unexpected)
    monkeypatch.setattr(_linux_system, "window_at_point", unexpected)
    snap = driver._snapshot_root("xarchiver", object(), Scope.WINDOW)
    assert all("covered by 'Extract files'" in snap.element(f"button{i}").note for i in range(100))
    assert snap.element("confirm").note is None
    assert reads == [True]
    assert native_reads == ["main", "dialog"]

    # Reordering windows must invalidate all discovery evidence on the next snapshot.
    rows.reverse()
    snap = driver._snapshot_root("xarchiver", object(), Scope.WINDOW)
    assert all(snap.element(f"button{i}").note is None for i in range(100))
    assert snap.element("confirm").note == "covered by 'Archive'"
    assert len(reads) == 2
    assert native_reads == ["main", "dialog", "main", "dialog"]

    # An action still probes current native ownership, independently of discovery.
    monkeypatch.setattr(_linux_system, "pid_at_point", lambda x, y: 4242)
    monkeypatch.setattr(_linux_system, "window_id_for_toplevel", lambda **kw: 1)
    monkeypatch.setattr(_linux_system, "window_at_point", lambda x, y: {
        "window_id": 2, "title": "Extract files",
    })
    monkeypatch.setattr(_atspi, "toplevel_raise_target", lambda handle: (
        False, "Archive", 4242, (0, 0, 800, 600),
    ))
    assert driver.same_app_window_cover(snap.element("button0"), "xarchiver") == "Extract files"


def test_linux_discovery_caches_failed_native_window_lookup(monkeypatch):
    from a11y_computer_use import observe
    from a11y_computer_use.drivers import _atspi
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    owner = _element("main", "AXWindow", "Archive")
    target = _element("button", "AXButton", "Extract", parent="main", clickable=True)
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(driver, "_gtk4_point_untrusted", lambda el, handle: False)
    monkeypatch.setattr(observe, "ax_handle_for", lambda snap, ref: ref)
    calls = []

    def unavailable(handle):
        calls.append(handle)
        raise RuntimeError("window disappeared")

    monkeypatch.setattr(_atspi, "toplevel_raise_target", unavailable)
    evidence = (owner, [{"window_id": 1}], {})
    for _ in range(100):
        assert driver.same_app_window_cover(target, "xarchiver", _discovery=evidence) is None
    assert calls == ["main"]


@pytest.mark.parametrize("rows", [0, 2000])
@pytest.mark.parametrize("cover", ["none", "new", "dismissed", "ancestor", "aside"])
@pytest.mark.parametrize("operation", ["click", "crop"])
def test_linux_web_cover_reuses_ref_resolution_tree(
    tmp_path, monkeypatch, rows, cover, operation,
):
    """#737: the guard consumes fresh evidence without a second page read."""
    from a11y_computer_use.drivers.linux import LinuxDriver

    class ResolvedTreeDriver(TreeDriver):
        resolve_ref = LinuxDriver.resolve_ref
        _remember_popover_cover = LinuxDriver._remember_popover_cover
        take_resolved_popover_cover = LinuxDriver.take_resolved_popover_cover

    driver = ResolvedTreeDriver()
    driver.elements.append(_element("web", "AXWebArea", "Page"))
    driver.elements.extend(_element(f"row{i}", "AXStaticText", str(i)) for i in range(rows))
    overlay = _element("overlay", "AXPopover", "Menu", bounds=Bounds(0, 0, 0, 200, 100))
    if cover == "dismissed":
        driver.elements.append(overlay)
    runtime = _runtime(tmp_path, driver)
    driver.name = "linux"
    if cover in {"new", "ancestor", "aside"}:
        if cover == "aside":
            overlay = dataclasses.replace(overlay, bounds=Bounds(0, 400, 400, 100, 100))
        driver.elements.append(overlay)
    if cover == "ancestor":
        # Preserve target ancestry in both the issued and resolved tree.
        driver._replace("e2", parent="overlay")
        runtime._current = driver.snapshot(runtime._current.scope, APP)
    if cover == "dismissed":
        driver.elements.remove(overlay)
    original = runtime._popover_cover
    checked = []

    def guard(element, *, refresh=False):
        if not refresh:
            return original(element)
        with monkeypatch.context() as patch:
            def unexpected_snapshot(*args):
                pytest.fail("web cover guard requested another full page snapshot")
            patch.setattr(driver, "snapshot", unexpected_snapshot)
            result = original(element, refresh=True)
        checked.append(result)
        return result

    monkeypatch.setattr(runtime, "_popover_cover", guard)
    if cover == "new":
        with pytest.raises(ComputerUseError) as caught:
            getattr(runtime, operation)("e2")
        assert caught.value.code == ErrorCode.NOT_VISIBLE
        assert caught.value.detail["reason"] == "covered"
        assert driver.calls == []
        assert checked == ["popover 'Menu'"]
    else:
        getattr(runtime, operation)("e2")
        assert checked == [None]


def test_linux_resolved_cover_evidence_is_target_bound_and_single_use():
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    target = _tree()[1]
    driver._resolved_popover_cover = (target, "popover")
    assert driver.take_resolved_popover_cover(dataclasses.replace(target)) == (False, None)
    assert driver.take_resolved_popover_cover(target) == (False, None)
    driver._resolved_popover_cover = (target, None)
    assert driver.take_resolved_popover_cover(target) == (True, None)
    assert driver.take_resolved_popover_cover(target) == (False, None)


def test_calc_verified_type_avoids_window_reread(monkeypatch):
    """Use the driver's real settled read once, without a window traversal."""
    runtime = server.Runtime.__new__(server.Runtime)
    runtime.driver = TreeDriver()
    runtime.driver.name = "linux"
    runtime._current = runtime.driver.snapshot(Scope.WINDOW, "soffice.bin")
    runtime.driver._sheet_type_readback = ("soffice.bin", "42")
    monkeypatch.setattr(runtime, "_process_died", lambda _before: False)
    monkeypatch.setattr(runtime, "_reread", lambda *_args: pytest.fail("full window reread"))
    assert runtime._judge_mutation("soffice.bin", None, None, "42", previous="") == (
        "confirmed", "read back '42'",
    )
    assert runtime.driver._sheet_type_readback is None
    assert runtime._capture("soffice.bin", targeted=True) is None
    # The read cannot confirm another write or a click; failed probes fall back.
    reads = []
    monkeypatch.setattr(runtime, "_reread", lambda *args: reads.append(args))
    runtime._judge_mutation("soffice.bin", None, None, "42")
    assert len(reads) == 2


@pytest.mark.parametrize("settled", [("other", "42"), ("soffice.bin", "24"), None])
def test_calc_unverified_type_keeps_window_fallback(monkeypatch, settled):
    runtime = server.Runtime.__new__(server.Runtime)
    runtime.driver = TreeDriver()
    runtime.driver.name = "linux"
    runtime.driver._sheet_type_readback = settled
    runtime._current = None
    monkeypatch.setattr(runtime, "_process_died", lambda _before: False)
    reads = []
    monkeypatch.setattr(runtime, "_reread", lambda *args: reads.append(args))
    assert runtime._judge_mutation("soffice.bin", None, None, "42")[0] != "confirmed"
    assert len(reads) == 2


@pytest.mark.parametrize("after_cell,fast", [("A2", True), ("A1", False), (None, False)])
def test_calc_key_checks_live_cell_before_window(monkeypatch, after_cell, fast):
    runtime = server.Runtime.__new__(server.Runtime)
    runtime.driver = TreeDriver()
    runtime.driver.name = "linux"
    runtime._current = None
    monkeypatch.setattr(runtime, "_process_died", lambda _before: False)
    monkeypatch.setattr(runtime, "_key_focus_probe", lambda _app: (None, None, None, None, after_cell))
    reads = []
    monkeypatch.setattr(runtime, "_reread", lambda *args: reads.append(args))
    judged, _ = runtime._judge_mutation(
        "soffice.bin", None, None, None, key_focus=True,
        focus_before=(None, None, None, None, "A1"), key_chord="Return",
    )
    assert (judged == "confirmed") is fast
    assert len(reads) == (0 if fast else 1)


def test_calc_fast_verification_preserves_process_death(monkeypatch):
    runtime = server.Runtime.__new__(server.Runtime)
    runtime.driver = TreeDriver()
    runtime.driver.name = "linux"
    runtime.driver._sheet_type_readback = ("soffice.bin", "42")
    runtime._current = None
    monkeypatch.setattr(runtime, "_process_died", lambda _before: True)
    assert runtime._judge_mutation("soffice.bin", None, None, "42")[0] == "partial"


def test_calc_targeted_baseline_keeps_pid_and_refreshes_for_click(monkeypatch):
    runtime = server.Runtime.__new__(server.Runtime)
    runtime.driver = TreeDriver()
    runtime.driver.name = "linux"
    runtime._current = runtime.driver.snapshot(Scope.WINDOW, "soffice.bin")
    before = runtime._freeze(runtime._current)
    before.update(pid_alive=True, pid=1234)
    runtime.driver._sheet_type_readback = ("soffice.bin", "42")
    checks = []
    monkeypatch.setattr(outcome, "wait_until_dead", lambda pid, _timeout: checks.append(pid) or False)
    runtime._judge_mutation("soffice.bin", before, None, "42", previous="")
    assert runtime._capture("soffice.bin", targeted=True) is None
    assert runtime._process_died(None) is False
    assert checks == [1234, 1234]
    reads = []
    fresh = runtime.driver.snapshot(Scope.WINDOW, "soffice.bin")
    monkeypatch.setattr(runtime, "_probe_snapshot", lambda *args: reads.append(args) or fresh)
    assert runtime._capture("soffice.bin")["snap"] is fresh
    assert len(reads) == 1


@pytest.mark.parametrize("after_cell,fast", [("A2", True), ("A1", False), (None, False)])
def test_calc_key_empty_selection_uses_visible_focus(monkeypatch, after_cell, fast):
    """Exercise the native evidence producer, not a fabricated five-item tuple."""
    from a11y_computer_use.drivers import _atspi

    runtime = server.Runtime.__new__(server.Runtime)
    runtime.driver = TreeDriver()
    runtime.driver.name = "linux"
    runtime._current = None
    cell = object()
    address = ["A1"]
    monkeypatch.setattr(_atspi, "_focused_node", lambda _app: (None, False))
    monkeypatch.setattr(_atspi, "sheet_editor_text", lambda _app: None)
    monkeypatch.setattr(_atspi, "_selected_sheet_cell", lambda _app: None)
    monkeypatch.setattr(_atspi, "find_root", lambda *_args: object())
    monkeypatch.setattr(_atspi, "_find_spreadsheet", lambda *_args: object())
    monkeypatch.setattr(_atspi, "_sheet_window_cells", lambda _table: [cell])
    monkeypatch.setattr(_atspi, "_node_name", lambda _cell: address[0])
    monkeypatch.setattr(_atspi, "_state_flags", lambda _cell: (True, True, False, False))
    monkeypatch.setattr(runtime, "_process_died", lambda _before: False)
    reads = []
    monkeypatch.setattr(runtime, "_reread", lambda *args: reads.append(args))

    before = runtime._key_focus_probe("soffice.bin")
    assert before[4] == "A1"
    address[0] = after_cell
    judged, _ = runtime._judge_mutation(
        "soffice.bin", None, None, None, key_focus=True,
        focus_before=before, key_chord="Return",
    )
    assert (judged == "confirmed") is fast
    assert len(reads) == (0 if fast else 1)


@pytest.mark.parametrize("selected", ["A3", None, "error"])
def test_calc_key_cell_probe_preserves_selection_and_handles_failure(monkeypatch, selected):
    from a11y_computer_use.drivers import _atspi

    monkeypatch.setattr(_atspi, "_focused_node", lambda _app: (None, False))
    monkeypatch.setattr(_atspi, "sheet_editor_text", lambda _app: None)

    def selection(_app):
        if selected == "error":
            raise RuntimeError("Selection unavailable")
        return selected

    def focused(_app):
        if selected == "A3":
            pytest.fail("a published Selection must take precedence")
        raise RuntimeError("visible cell unavailable")

    monkeypatch.setattr(_atspi, "selected_sheet_address", selection)
    monkeypatch.setattr(_atspi, "_focused_sheet_address", focused)
    assert _atspi.focused_key_evidence("soffice.bin")[4] == (
        "A3" if selected == "A3" else None
    )


def _calc_cell_snapshot(**changes) -> Snapshot:
    cell = _element(
        "e26", "AXCell", "A1", bounds=Bounds(0, 100, 200, 80, 20), clickable=True,
    )
    cell = dataclasses.replace(cell, snapshot_id="calc-snap", **changes)
    return Snapshot(
        snapshot_id="calc-snap", scope=Scope.WINDOW, app="soffice.bin", pid=777,
        created_at=0.0, displays=(Display(0, 800, 600, 1.0, True),),
        elements=(cell,),
    )


@pytest.mark.parametrize("case,vouched", [
    ("same", True),
    ("moved", False),
    ("renamed", False),
    ("other_pid", False),
    ("disabled", False),
    ("not_libreoffice", False),
    ("not_a_cell", False),
    ("popover", False),
    ("no_handle", False),
    ("read_failed", False),
])
def test_calc_cell_click_targeted_resolution_or_full_read(monkeypatch, case, vouched):
    """#775: one read of the cell's own handle replaces the full-window rematch.

    Anything the targeted read cannot vouch for is None, and the caller keeps
    the full resolution.
    """
    from a11y_computer_use import observe
    from a11y_computer_use.drivers import _atspi
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.observe import RawNode

    changes = {}
    if case == "not_a_cell":
        changes["role"] = "AXStaticText"
    snap = _calc_cell_snapshot(**changes)
    if case == "not_libreoffice":
        snap = dataclasses.replace(snap, app="gnumeric")
    if case == "popover":
        snap = dataclasses.replace(snap, elements=snap.elements + (
            dataclasses.replace(_element("p1", "AXPopover", "Autocomplete"), snapshot_id="calc-snap"),
        ))
    geometry = (observe.DisplayGeometry(Display(0, 800, 600, 1.0, True), (0.0, 0.0)),)
    handle = object()
    position = (100.0, 200.0) if case != "moved" else (100.0, 180.0)
    raw = RawNode(
        role="AXCell", title="A1", position=position, size=(80.0, 20.0),
        enabled=case != "disabled",
    )
    monkeypatch.setattr(
        observe, "ax_handle_for",
        lambda sid, ref: None if case == "no_handle" else (handle if (sid, ref) == ("calc-snap", "e26") else None),
    )

    def live_cell(acc, address):
        assert acc is handle and address == "A1"
        if case == "renamed":
            return None
        if case == "read_failed":
            raise RuntimeError("bus dropped")
        return raw

    monkeypatch.setattr(_atspi, "live_sheet_cell", live_cell)
    monkeypatch.setattr(_atspi, "pid_of", lambda acc: 778 if case == "other_pid" else 777)
    monkeypatch.setattr(_atspi, "primary_geometry", lambda: geometry)
    driver = LinuxDriver.__new__(LinuxDriver)
    driver._run = lambda fn: fn()

    def full_walk(*args):
        pytest.fail("the targeted resolution never walks the window")

    driver.snapshot = full_walk
    found = driver.resolve_sheet_cell(snap, "e26")
    if vouched:
        assert found is snap.element("e26")
        # No popover in the tree: the click guard consumes this evidence and
        # does not walk the window either.
        assert driver.take_resolved_popover_cover(found) == (True, None)
    else:
        assert found is None


@pytest.mark.parametrize("vouched", [True, False])
def test_calc_cell_click_uses_targeted_resolution_before_full_rematch(tmp_path, vouched):
    """The full rematch runs only when the targeted check cannot vouch."""
    driver = TreeDriver()
    full: list[str] = []
    targeted: list[str] = []
    original = driver.resolve_ref

    def resolve_ref(snap, ref, *, live=None):
        full.append(ref)
        return original(snap, ref, live=live)

    def resolve_sheet_cell(snap, ref):
        targeted.append(ref)
        return snap.element(ref) if vouched else None

    driver.resolve_ref = resolve_ref
    driver.resolve_sheet_cell = resolve_sheet_cell
    runtime = _runtime(tmp_path, driver)
    runtime.click("e4")
    assert targeted == ["e4"]
    assert full == ([] if vouched else ["e4"])
    assert ("press", "e4") in driver.calls


def test_calc_cell_targeted_resolution_is_click_only(tmp_path):
    driver = TreeDriver()
    asked: list[str] = []
    driver.resolve_sheet_cell = lambda snap, ref: asked.append(ref) or snap.element(ref)
    runtime = _runtime(tmp_path, driver)
    for kind in ("hover", "scroll", "drag", "typetext"):
        runtime._resolve("e4", kind)
    assert asked == []


@pytest.mark.parametrize("case", ["live", "gone", "hidden", "renamed", "not_grid", "title"])
def test_live_sheet_cell_requires_the_same_showing_grid_cell(monkeypatch, case):
    from a11y_computer_use.drivers import _atspi
    from a11y_computer_use.observe import RawNode

    cell, grid = object(), object()
    monkeypatch.setattr(_atspi, "accessible_gone", lambda acc: case == "gone")
    monkeypatch.setattr(_atspi, "_state_has", lambda acc, name: case != "hidden")
    monkeypatch.setattr(_atspi, "_node_name", lambda acc: "B1" if case == "renamed" else "A1")
    monkeypatch.setattr(_atspi, "_parent_of", lambda acc: grid)
    monkeypatch.setattr(_atspi, "_spreadsheet_table", lambda node: case != "not_grid" and node is grid)
    title = "A1 " if case == "title" else "A1"
    monkeypatch.setattr(
        _atspi.ATSPIAccessor, "read",
        lambda self, acc: RawNode(role="AXCell", title=title, position=(1.0, 2.0), size=(3.0, 4.0)),
    )
    found = _atspi.live_sheet_cell(cell, "A1")
    if case == "live":
        assert found is not None and found.position == (1.0, 2.0)
    else:
        assert found is None


class _CoveredLinuxDriver(TreeDriver):
    """A Linux pointer click whose point another app's window covers (#815).

    ``activate_app`` raises the gated app. ``raise_clears`` decides whether
    that uncovers the ref. ``moves`` gives the ref a new box after the raise,
    and ``raise_churn`` changes an unrelated node when the window is raised.
    """

    name = "linux"
    resolves_apps = False

    def __init__(self, *, raise_clears: bool = True, moves: bool = False,
                 raise_churn: bool = False, activate_fails: bool = False,
                 click_lands: bool = True) -> None:
        super().__init__()
        self.covered = True
        self.raise_clears = raise_clears
        self.moves = moves
        self.raise_churn = raise_churn
        self.activate_fails = activate_fails
        self.click_lands = click_lands

    def resolve_ref(self, snap: Snapshot, ref: str, *, live: Snapshot | None = None) -> Element:
        del snap, live
        return next(item for item in self.elements if item.ref == ref)

    def press_element(self, element: Element) -> bool:
        self.calls.append(("press", element.ref))
        return False  # pointer path, where the hit-test runs

    def activate_app(self, identifier):
        self.calls.append(("activate", identifier))
        if self.activate_fails:
            raise ComputerUseError(ErrorCode.FOCUS_CHANGED, "focus did not land", detail={})
        if self.raise_clears:
            self.covered = False
        if self.moves:
            self._replace("e2", bounds=Bounds(0, 300, 200, 80, 24))
        if self.raise_churn:
            self._replace("e4", title="Window raised")
        return identifier

    def click(self, target, **kwargs) -> None:
        del kwargs
        box = target.bounds
        self.calls.append(("click", target.ref, (box.x, box.y)))
        if not self.covered and self.click_lands:
            self._replace("e5", title="saved")


def _covered_runtime(tmp_path, monkeypatch, driver: _CoveredLinuxDriver):
    from a11y_computer_use.drivers import _linux_system

    # This driver uses native point guards, but its app is synthetic on every
    # host (including macOS, where app resolution consults NSWorkspace).
    monkeypatch.setattr(server, "_running_app", lambda identifier: (None, APP))
    monkeypatch.setattr(_linux_system, "pid_at_point",
                        lambda x, y: 777 if driver.covered else driver.pid)
    monkeypatch.setattr(server, "_app_at_point",
                        lambda point: "mousepad" if driver.covered else APP)
    return _runtime(tmp_path, driver)


def _clicks(driver: TreeDriver) -> list[tuple]:
    return [call for call in driver.calls if call[0] == "click"]


def test_linux_click_raises_the_app_over_another_apps_window_and_clicks_once(
    tmp_path, monkeypatch,
) -> None:
    """#815: a covered ref click raises the app, re-resolves, and lands once.

    A plain retry got ``focus_changed`` every time. The click now raises the
    gated app before the pointer, so one call lands.
    """
    driver = _CoveredLinuxDriver()
    runtime = _covered_runtime(tmp_path, monkeypatch, driver)

    result = runtime.click("e2")

    assert result.startswith("clicked e2")
    assert result.outcome == "confirmed"
    assert _clicks(driver) == [("click", "e2", (10, 20))]
    order = [call[0] for call in driver.calls if call[0] in {"activate", "click"}]
    assert order == ["activate", "click"]


def test_linux_covered_click_uses_the_box_resolved_after_the_raise(
    tmp_path, monkeypatch,
) -> None:
    """A raise can move the window. The pointer goes to the fresh box."""
    driver = _CoveredLinuxDriver(moves=True)
    runtime = _covered_runtime(tmp_path, monkeypatch, driver)

    runtime.click("e2")

    assert _clicks(driver) == [("click", "e2", (300, 200))]


def test_linux_click_still_covered_after_the_raise_is_refused_as_covered(
    tmp_path, monkeypatch,
) -> None:
    """No pointer event, and no "re-observe and retry" that cannot land.

    The refusal matches ``type`` on a covered control.
    """
    driver = _CoveredLinuxDriver(raise_clears=False)
    runtime = _covered_runtime(tmp_path, monkeypatch, driver)

    with pytest.raises(ComputerUseError) as caught:
        runtime.click("e2")

    err = caught.value
    assert err.code is ErrorCode.UNSUPPORTED
    assert err.message == "e2 is covered by mousepad"
    assert err.detail["reason"] == "covered"
    assert err.detail["outcome"] == "refused"
    assert err.detail["next"] == ["foreground", "ref"]
    assert err.detail["evidence"] == "the window is covered by mousepad"
    assert "retry" not in err.message
    assert _clicks(driver) == []
    assert [call for call in driver.calls if call[0] == "activate"] == [("activate", APP)]


def test_linux_covered_click_whose_raise_fails_is_refused_as_covered(
    tmp_path, monkeypatch,
) -> None:
    driver = _CoveredLinuxDriver(activate_fails=True)
    runtime = _covered_runtime(tmp_path, monkeypatch, driver)

    with pytest.raises(ComputerUseError) as caught:
        runtime.click("e2")

    assert caught.value.detail["reason"] == "covered"
    assert caught.value.code is not ErrorCode.FOCUS_CHANGED
    assert _clicks(driver) == []


def test_linux_window_raise_alone_does_not_confirm_a_click(tmp_path, monkeypatch) -> None:
    """The tree change from raising the window is not evidence the click landed."""
    driver = _CoveredLinuxDriver(raise_churn=True, click_lands=False)
    runtime = _covered_runtime(tmp_path, monkeypatch, driver)

    result = runtime.click("e2")

    assert _clicks(driver) == [("click", "e2", (10, 20))]
    assert result.outcome != "confirmed"


def test_linux_uncovered_click_does_not_raise_the_app(tmp_path, monkeypatch) -> None:
    driver = _CoveredLinuxDriver()
    driver.covered = False
    runtime = _covered_runtime(tmp_path, monkeypatch, driver)

    runtime.click("e2")

    assert not [call for call in driver.calls if call[0] == "activate"]
    assert _clicks(driver) == [("click", "e2", (10, 20))]
