"""Hermetic tests for crop(ref): pixels, padding, scale, and visibility errors.

No screen is captured. A fake driver serves one PNG and one accessibility tree.
"""

from __future__ import annotations

import io

from PIL import Image

from a11y_computer_use import capture, safety, server
from a11y_computer_use.agent.actions import Action, validate_action
from a11y_computer_use.agent.core import forced_method
from a11y_computer_use.capture import Screenshot
from a11y_computer_use.schema import (
    Bounds,
    ComputerUseError,
    Display,
    Element,
    ErrorCode,
    Scope,
    Snapshot,
)


def _png(width: int, height: int, color: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _element(ref: str, bounds: Bounds, title: str = "Swatch") -> Element:
    return Element(
        ref=ref, role="AXButton", title=title, value=None,
        bounds=bounds, snapshot_id="snap", clickable=True,
    )


class CropDriver:
    """One tree, one PNG, and a name for whatever owns the element's center."""

    name = "fake"
    resolves_apps = True

    def __init__(self, elements, png, display, owner="demo") -> None:
        self.elements = tuple(elements)
        self.png = png
        self.display = display
        self.owner = owner
        self.shots = 0
        self.app = "demo"

    def ensure_trusted(self) -> None:
        return None

    def frontmost_app(self):
        return self.app, 1

    def displays(self):
        return (self.display,)

    def snapshot(self, scope, app):
        return Snapshot(
            "snap", scope if isinstance(scope, Scope) else Scope(scope),
            app, 1, 0.0, (self.display,), self.elements,
        )

    def resolve_ref(self, snap, ref, live=None):
        return snap.element(ref)

    def screenshot(self, display_id=None):
        self.shots += 1
        return Screenshot(png=self.png, display=self.display)

    def app_at_point(self, point):
        return self.owner


def _runtime(tmp_path, driver):
    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier("demo", safety.Tier.READ)
    runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)
    runtime.desktop_snapshot("demo")
    return runtime


def _red_count(png: bytes) -> tuple[int, int]:
    image = Image.open(io.BytesIO(png)).convert("RGB")
    pixels = list(image.getdata())
    red = sum(1 for r, g, b in pixels if r > 200 and g < 50 and b < 50)
    return red, len(pixels)


def test_crop_png_integer_scale_keeps_a_solid_color() -> None:
    png, width, height = capture.crop_png(_png(40, 20), (5, 4, 10, 8), scale=2)
    assert (width, height) == (20, 16)
    red, total = _red_count(png)
    assert red == total


def test_crop_png_rejects_a_box_outside_the_image() -> None:
    try:
        capture.crop_png(_png(10, 10), (8, 8, 4, 4))
    except ValueError as exc:
        assert "outside" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_runtime_crop_matches_bounds_and_dominant_color(tmp_path) -> None:
    display = Display(0, 100, 80, 1.0, True)
    element = _element("e2", Bounds(0, 10, 12, 20, 16))
    driver = CropDriver([element], _png(100, 80), display)
    runtime = _runtime(tmp_path, driver)
    text, image = runtime.crop("e2")
    assert image.width == 20 and image.height == 16
    assert "at (10, 12) 20x16" in text
    assert "No text was read" in text
    red, total = _red_count(image.png)
    assert red == total
    assert driver.shots == 1


def test_runtime_crop_padding_and_integer_scale(tmp_path) -> None:
    display = Display(0, 100, 80, 1.0, True)
    element = _element("e2", Bounds(0, 10, 12, 20, 16))
    driver = CropDriver([element], _png(100, 80), display)
    runtime = _runtime(tmp_path, driver)
    text, image = runtime.crop("e2", padding=4, scale=2)
    # 20+8 by 16+8, then doubled. The whole frame is red, so the pad stays red.
    assert image.width == 56 and image.height == 48
    assert "padding 4, scale 2" in text
    red, total = _red_count(image.png)
    assert red == total


def test_runtime_crop_clips_a_partial_element_and_refuses_a_miss(tmp_path) -> None:
    display = Display(0, 100, 80, 1.0, True)
    partial = _element("e2", Bounds(0, -10, 5, 30, 10))
    driver = CropDriver([partial], _png(100, 80), display)
    runtime = _runtime(tmp_path, driver)
    _text, image = runtime.crop("e2")
    assert image.width == 20 and image.height == 10  # clipped to x=0

    missed = _element("e3", Bounds(0, -80, 5, 20, 10))
    driver.elements = (missed,)
    runtime.desktop_snapshot("demo")
    shots = driver.shots
    try:
        runtime.crop("e3", padding=100)
    except ComputerUseError as exc:
        assert exc.code is ErrorCode.NOT_VISIBLE
        assert exc.detail["reason"] == "off_screen"
        assert "e3 is off-screen" in exc.message
    else:
        raise AssertionError("padding must not pull an off-screen ref on screen")
    assert driver.shots == shots


def test_runtime_crop_refuses_a_cover_without_taking_a_screenshot(tmp_path) -> None:
    display = Display(0, 100, 80, 1.0, True)
    element = _element("e2", Bounds(0, 10, 12, 20, 16))
    driver = CropDriver([element], _png(100, 80), display, owner="other-app")
    runtime = _runtime(tmp_path, driver)
    try:
        runtime.crop("e2")
    except ComputerUseError as exc:
        assert exc.code is ErrorCode.NOT_VISIBLE
        assert exc.detail["reason"] == "covered"
        assert "covered by other-app" in exc.message
        assert server.error_text(exc).startswith("not_visible:")
    else:
        raise AssertionError("expected not_visible")
    assert driver.shots == 0


def test_runtime_crop_rejects_bad_padding_scale_and_a_huge_edge(tmp_path) -> None:
    display = Display(0, 5000, 100, 1.0, True)
    element = _element("e2", Bounds(0, 0, 0, 3000, 10))
    driver = CropDriver([element], _png(8, 8), display)
    runtime = _runtime(tmp_path, driver)
    for bad in (True, -1, 513, 1.5):
        try:
            runtime.crop("e2", padding=bad)
        except ValueError as exc:
            assert "padding" in str(exc)
        else:
            raise AssertionError(f"padding {bad!r} should be rejected")
    try:
        runtime.crop("e2", scale=0)
    except ValueError as exc:
        assert "scale" in str(exc)
    else:
        raise AssertionError("scale 0 should be rejected")
    shots = driver.shots
    try:
        runtime.crop("e2", scale=2)
    except ValueError as exc:
        assert "4096" in str(exc)
    else:
        raise AssertionError("expected the long-edge limit")
    assert driver.shots == shots


def test_crop_action_is_not_turned_into_a_click() -> None:
    action = Action("crop", {"ref": "e2"})
    forced, label = forced_method(action, 2, None)
    assert forced.name == "crop" and forced.args == {"ref": "e2"} and label is None
    assert validate_action(Action("crop", {})) == "crop requires ref"
    assert validate_action(action) is None


def test_linux_occlusion_matches_pid_before_comm(monkeypatch) -> None:
    from a11y_computer_use.drivers import _atspi, _linux_system
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    element = _element("e2", Bounds(0, 4, 6, 20, 10))
    monkeypatch.setattr(_linux_system, "pid_at_point", lambda x, y: 42)
    monkeypatch.setattr(_atspi, "find_root", lambda app, scope: object())
    monkeypatch.setattr(_atspi, "pid_of", lambda root: 42)
    assert driver.occlusion(element, "cuaswatch") is None

    monkeypatch.setattr(_linux_system, "pid_at_point", lambda x, y: 7)
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: "other")
    assert driver.occlusion(element, "cuaswatch") == "other"

    monkeypatch.setattr(_linux_system, "pid_at_point", lambda x, y: None)
    assert driver.occlusion(element, "cuaswatch") is None


class _ScrolledDriver(CropDriver):
    """resolve_ref raises stale_ref for refs listed in ``offscreen``.

    ``offscreen[ref]`` is the live box when the node is still valid, or None
    when the node is gone. Click stays on the stale path. Crop asks
    ``alive_offscreen``.
    """

    def __init__(self, elements, png, display) -> None:
        super().__init__(elements, png, display)
        self.offscreen: dict[str, Bounds | None] = {}
        self.resolved: list[str] = []
        self.revealed: list[str] = []
        self.reveal_ok = True

    def resolve_ref(self, snap, ref, live=None):
        self.resolved.append(ref)
        if ref in self.offscreen:
            raise ComputerUseError(
                ErrorCode.STALE_REF,
                f"{ref} (AXButton 'Red swatch') is no longer in the tree under that title; "
                "the AXButton at that position is now 'Reload'. The list may have reordered: "
                "use find(text=...) or scroll_to_find to locate it again rather than clicking the slot",
                detail={"reason": "title_changed", "ref": ref},
            )
        return snap.element(ref)

    def alive_offscreen(self, snap, ref):
        if ref not in self.offscreen:
            return None
        return self.offscreen[ref]

    def scroll_into_view(self, element) -> bool:
        self.revealed.append(element.ref)
        return self.reveal_ok


def test_crop_of_a_scrolled_off_ref_is_off_screen_and_click_stays_stale(tmp_path) -> None:
    """A still-valid ref that left the viewport is not_visible, not stale_ref.

    The driver reports the Reload occupant the way a live browser does. Crop
    names off_screen and scroll(into_view=true), and it does not take a
    screenshot, including with padding. A gone ref stays stale_ref. Click and
    a wheel scroll stay stale_ref. into_view uses the original handle.
    """
    display = Display(0, 100, 80, 1.0, True)
    visible = _element("e2", Bounds(0, 10, 12, 20, 16), title="Stay")
    scrolled = _element("e3", Bounds(0, 8, 40, 200, 60), title="Red swatch")
    gone = _element("e9", Bounds(0, 8, 40, 200, 60), title="Red swatch")
    driver = _ScrolledDriver([visible, scrolled, gone], _png(100, 80), display)
    driver.offscreen["e3"] = Bounds(0, 8, -80, 200, 60)
    driver.offscreen["e9"] = None
    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier("demo", safety.Tier.FULL)
    runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)
    runtime.desktop_snapshot("demo")
    shots = driver.shots

    try:
        runtime.crop("e3", padding=512)
    except ComputerUseError as exc:
        assert exc.code is ErrorCode.NOT_VISIBLE
        assert exc.detail["reason"] == "off_screen"
        assert exc.detail["hint"] == "scroll(ref='e3', into_view=true)"
        assert "e3 is off-screen" in exc.message
        assert "still valid" in exc.message
        assert "Reload" not in exc.message
        text = server.error_text(exc)
        assert text.startswith("not_visible:")
        assert "hint: scroll(ref='e3', into_view=true)" in text
    else:
        raise AssertionError("a scrolled-off ref must be not_visible")
    assert driver.shots == shots

    try:
        runtime.click("e3")
    except ComputerUseError as exc:
        assert exc.code is ErrorCode.STALE_REF
        assert exc.detail["reason"] == "title_changed"
        assert "Reload" in exc.message
    else:
        raise AssertionError("click on a reordered slot stays stale_ref")

    try:
        runtime.scroll("e3", dy=4)
    except ComputerUseError as exc:
        assert exc.code is ErrorCode.STALE_REF
    else:
        raise AssertionError("a wheel scroll must not land on the old slot")

    driver.resolved.clear()
    revealed = runtime.scroll("e3", into_view=True)
    assert "into view" in revealed
    assert driver.revealed == ["e3"]
    assert driver.resolved == []

    driver.reveal_ok = False
    try:
        runtime.scroll("e3", into_view=True)
    except ComputerUseError as exc:
        assert exc.code is ErrorCode.NOT_VISIBLE
        assert exc.detail["reason"] == "off_screen"
    else:
        raise AssertionError("a failed reveal must not wheel the old point")

    try:
        runtime.crop("e9")
    except ComputerUseError as exc:
        assert exc.code is ErrorCode.STALE_REF
        assert "Reload" in exc.message
    else:
        raise AssertionError("a gone ref stays stale_ref")
    assert driver.shots == shots
