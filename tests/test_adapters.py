"""Provider executor adapters: Anthropic and OpenAI computer-use actions run
through the gated Runtime on a recording fake driver (no OS, no permissions).

Covers: coordinate scaling image<->physical, snap-to-ref (smallest actionable
element, size cap, fallback to coordinates), every action of both providers,
key-name conversion, refusals and structured errors surfacing as text, the tool
definitions' exact keys, and a hermetic browser run over the scripted CDP
transport. A live headless-Chromium check runs only when a CDP endpoint is up.
"""

from __future__ import annotations

import base64
import dataclasses
import io
import json
import os
from pathlib import Path

import pytest
from PIL import Image

from computeruse import capture, safety, server
from computeruse.adapters import (
    AnthropicComputerAdapter,
    OpenAIComputerAdapter,
    Result,
    openai_keys_to_chords,
    xdotool_to_chord,
)
from computeruse.adapters import anthropic_computer, openai_computer
from computeruse.schema import (
    Bounds,
    ComputerUseError,
    Display,
    Element,
    ErrorCode,
    Point,
    Scope,
    Snapshot,
)

APP = "com.example.Editor"
DISPLAY = Display(display_id=1, width=1600, height=1000, scale=1.0, is_main=True)


def _png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (20, 20, 40)).save(buf, format="PNG")
    return buf.getvalue()


def _snapshot(snapshot_id: str = "snap-1") -> Snapshot:
    """Window (not actionable), a big clickable group (too large to snap), a
    button and an editable field inside it, a secure field, a disabled button."""
    els = (
        Element("e1", "AXWindow", "Editor", None, Bounds(1, 0, 0, 1600, 1000), snapshot_id,
                path=("AXWindow",)),
        Element("e2", "AXGroup", "Canvas", None, Bounds(1, 0, 0, 1000, 700), snapshot_id,
                parent="e1", path=("AXWindow", "AXGroup"), clickable=True),
        Element("e3", "AXButton", "Save", None, Bounds(1, 100, 100, 200, 60), snapshot_id,
                parent="e2", path=("AXWindow", "AXGroup", "AXButton"), clickable=True),
        Element("e4", "AXTextField", "Name", "", Bounds(1, 100, 300, 400, 50), snapshot_id,
                parent="e2", path=("AXWindow", "AXGroup", "AXTextField"), clickable=True,
                editable=True),
        Element("e5", "AXTextField", "Password", None, Bounds(1, 100, 400, 400, 50), snapshot_id,
                parent="e2", path=("AXWindow", "AXGroup", "AXTextField"), clickable=True,
                editable=True, secure=True),
        Element("e6", "AXButton", "Publish", None, Bounds(1, 1200, 800, 200, 60), snapshot_id,
                parent="e1", path=("AXWindow", "AXButton"), clickable=True, enabled=False),
    )
    return Snapshot(snapshot_id, Scope.WINDOW, APP, 77, 0.0, (DISPLAY,), els)


class FakeDriver:
    """Records every call; resolves its own apps (like the browser backend) so
    the whole gated Runtime runs without OS system-ops."""

    name = "fake"
    resolves_apps = True

    def __init__(self) -> None:
        self.calls: dict[str, list] = {k: [] for k in
                                       ("press", "click", "drag", "scroll", "type", "key", "zoom")}
        self.secure_focus = False
        self.snapshots = 0

    def ensure_trusted(self) -> None:
        pass

    def snapshot(self, scope, app):
        self.snapshots += 1
        return _snapshot(f"snap-{self.snapshots}")

    def resolve_ref(self, snap, ref, *, live=None):
        return snap.element(ref)

    def press_element(self, element) -> bool:
        self.calls["press"].append(element.ref)
        return True

    def scroll_into_view(self, element) -> bool:
        return True

    def set_value(self, element, value) -> bool:
        return True

    def click(self, target, *, button, count, modifiers, pre_check=None, dry_run=False):
        self.calls["click"].append((target, button.value, count, tuple(modifiers)))

    def drag(self, start, end, *, button=None, pre_check=None, dry_run=False):
        self.calls["drag"].append((start, end))

    def scroll(self, target, *, dx=0, dy=0, unit=None, pre_check=None, dry_run=False):
        self.calls["scroll"].append((target, dx, dy, unit.value))

    def type_text(self, text, *, pre_check=None, dry_run=False):
        if self.secure_focus:
            raise ComputerUseError(ErrorCode.SECURE_FIELD, "focused field is secure")
        self.calls["type"].append(text)

    def key_chord(self, chord, *, pre_check=None, dry_run=False):
        self.calls["key"].append(chord)

    def wait_for(self, target, *, condition, timeout_s, checker=None):
        return target

    def screenshot(self, display_id=None):
        return capture.Screenshot(png=_png(DISPLAY.width, DISPLAY.height), display=DISPLAY)

    def zoom_region(self, region):
        self.calls["zoom"].append(region)
        return _png(region.width, region.height)

    def frontmost_app(self):
        return APP, 77

    def app_at_point(self, point):
        return APP

    def running_apps(self):
        return [{"id": APP, "name": "Editor", "pid": 77, "frontmost": True}]

    def launch_app(self, identifier):
        pass

    def activate_app(self, identifier):
        return identifier

    def windows(self):
        return []

    def read_clipboard(self):
        return ""

    def write_clipboard(self, text):
        pass


@pytest.fixture
def fake() -> FakeDriver:
    return FakeDriver()


@pytest.fixture
def runtime(tmp_path: Path, fake: FakeDriver) -> server.Runtime:
    store = safety.PermissionStore(tmp_path / "perm.json")
    store.set_tier(APP, safety.Tier.FULL)
    return server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=fake)


@pytest.fixture
def anthropic(runtime) -> AnthropicComputerAdapter:
    return AnthropicComputerAdapter(runtime, app=APP)


@pytest.fixture
def openai(runtime) -> OpenAIComputerAdapter:
    return OpenAIComputerAdapter(runtime, app=APP, environment="mac")


def _image_size(png: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(png)).size


# --- screenshots and scaling ---------------------------------------------------


def test_screenshot_is_downscaled_and_records_the_mapping(anthropic) -> None:
    r = anthropic.handle({"action": "screenshot"})
    assert r.ok and r.png is not None and r.text == ""
    assert _image_size(r.png) == (1280, 800)  # 1600x1000 at a 1280 long edge
    assert anthropic.display_id == 1
    assert anthropic.screen.source_width == 1600 and anthropic.screen.scale == 0.8


def test_anthropic_content_and_tool_result_shapes(anthropic) -> None:
    shot = anthropic.handle({"action": "screenshot"})
    block = shot.to_anthropic_tool_result("toolu_1", toolset_name="computer")
    assert block["type"] == "tool_result" and block["toolset_name"] == "computer"
    assert block["content"][0]["type"] == "image"
    assert block["content"][0]["source"]["media_type"] == "image/png"
    assert base64.b64decode(block["content"][0]["source"]["data"]) == shot.png
    err = Result("key", "invalid key: nope", error="invalid")
    assert err.to_anthropic_tool_result("toolu_2") == {
        "type": "tool_result", "tool_use_id": "toolu_2", "is_error": True,
        "content": "invalid key: nope",
    }
    assert Result("wait", "").anthropic_content() == [{"type": "text", "text": "OK"}]


def test_coordinates_scale_from_image_space_to_physical(anthropic, fake) -> None:
    anthropic.snap_to_refs = False
    r = anthropic.handle({"action": "left_click", "coordinate": [640, 400]})
    assert r.ok and r.snapped_ref is None
    target, button, count, mods = fake.calls["click"][0]
    assert target == Point(1, 800, 500) and button == "left" and count == 1 and mods == ()
    assert "(640, 400) -> physical (800, 500)" in r.text


def test_click_without_prior_screenshot_probes_one_first(anthropic, fake) -> None:
    anthropic.snap_to_refs = False
    assert anthropic.screen is None
    r = anthropic.handle({"action": "right_click", "coordinate": [10, 10]})
    assert r.ok and anthropic.screen is not None
    assert fake.calls["click"][0][1] == "right"


# --- snap-to-ref ----------------------------------------------------------------


def test_click_inside_a_button_snaps_to_its_ref_and_uses_the_a11y_press(anthropic, fake) -> None:
    anthropic.handle({"action": "screenshot"})
    # button e3 spans physical (100..300, 100..160) => image (80..240, 80..128)
    r = anthropic.handle({"action": "left_click", "coordinate": [160, 104]})
    assert r.ok and r.snapped_ref == "e3"
    assert "snapped from image point (160, 104) to e3 AXButton 'Save'" in r.text
    assert fake.calls["press"] == ["e3"]       # activated via the accessibility API
    assert fake.calls["click"] == []           # no synthetic mouse event


def test_snap_picks_the_smallest_containing_element(anthropic, fake) -> None:
    # (160, 260) image => physical (200, 325): inside the group e2 AND the field e4
    r = anthropic.handle({"action": "left_click", "coordinate": [160, 260]})
    assert r.snapped_ref == "e4"


def test_snap_skips_oversized_elements_and_falls_back_to_coordinates(anthropic, fake) -> None:
    # (640, 480) image => physical (800, 600): inside the 1000x700 group e2 only,
    # which covers 44% of the display (> the 25% cap) => raw coordinate click.
    r = anthropic.handle({"action": "left_click", "coordinate": [640, 480]})
    assert r.ok and r.snapped_ref is None
    assert fake.calls["click"][0][0] == Point(1, 800, 600)


def test_snap_ignores_disabled_elements(anthropic, fake) -> None:
    # e6 "Publish" is disabled at physical (1200..1400, 800..860) => image (960.., 640..)
    r = anthropic.handle({"action": "left_click", "coordinate": [1040, 664]})
    assert r.ok and r.snapped_ref is None
    assert fake.calls["click"][0][0] == Point(1, 1300, 830)


def test_only_a_plain_left_click_snaps(anthropic, fake) -> None:
    # button, count and modifiers carry pointer semantics (context menu, word or
    # paragraph selection at the pointer): the model's exact point is kept and no
    # snapshot is taken for the click.
    r = anthropic.handle({"action": "double_click", "coordinate": [160, 104]})
    assert r.ok and r.snapped_ref is None
    assert fake.calls["click"][0] == (Point(1, 200, 130), "left", 2, ())
    r = anthropic.handle({"action": "right_click", "coordinate": [160, 104]})
    assert r.ok and r.snapped_ref is None and fake.calls["click"][1][:2] == (Point(1, 200, 130), "right")
    r = anthropic.handle({"action": "left_click", "coordinate": [160, 104], "text": "shift"})
    assert r.ok and r.snapped_ref is None and fake.calls["click"][2][3] == ("shift",)
    assert fake.calls["press"] == [] and fake.snapshots == 0


def test_clicks_inside_a_populated_field_or_a_slider_keep_the_point(runtime, fake) -> None:
    a = AnthropicComputerAdapter(runtime, app=APP)
    # an EMPTY field still snaps (coordinate-free focus-then-type stays available)
    assert a.handle({"action": "left_click", "coordinate": [160, 260]}).snapped_ref == "e4"
    assert fake.calls["press"] == ["e4"]
    # the same field with content: the point is a caret position, an AX focus
    # would select-all and a following type would replace what the model meant to append to
    base = _snapshot()
    slider = Element("e7", "AXSlider", "Volume", "50", Bounds(1, 600, 100, 300, 20), base.snapshot_id,
                     parent="e1", path=("AXWindow", "AXSlider"), clickable=True)
    populated = dataclasses.replace(base, elements=tuple(
        dataclasses.replace(e, value="Alice") if e.ref == "e4" else e for e in base.elements) + (slider,))
    fake.snapshot = lambda scope, app: populated
    # (380, 260) image -> (475, 325) physical: near the right end of the Name field
    r = a.handle({"action": "left_click", "coordinate": [380, 260]})
    assert r.ok and r.snapped_ref is None and fake.calls["click"][0][0] == Point(1, 475, 325)
    # a slider thumb: the position IS the value
    r = a.handle({"action": "left_click", "coordinate": [600, 88]})
    assert r.ok and r.snapped_ref is None and fake.calls["click"][1][0] == Point(1, 750, 110)
    assert fake.calls["press"] == ["e4"]  # no further AX presses


def test_snap_disabled_uses_raw_coordinates(runtime, fake) -> None:
    a = AnthropicComputerAdapter(runtime, app=APP, snap_to_refs=False)
    r = a.handle({"action": "left_click", "coordinate": [160, 104]})
    assert r.ok and r.snapped_ref is None and fake.calls["press"] == []
    assert fake.snapshots == 0  # no snapshot is taken when snapping is off


def test_click_does_not_snap_against_a_stale_snapshot_when_the_refresh_fails(tmp_path, fake, monkeypatch) -> None:
    """app=None gates against the frontmost app. When an ungranted dialog becomes
    frontmost the refresh is refused; the Editor's old snapshot must not redirect
    the click into an AX press on the Editor's button hidden under the dialog."""
    fake.resolves_apps = False  # OS-driver path: module-level frontmost / hit-test
    front = {"app": APP}
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: front["app"])
    monkeypatch.setattr(server, "_running_app", lambda ident: (None, ident))
    monkeypatch.setattr(server, "_app_at_point", lambda point: APP)
    store = safety.PermissionStore(tmp_path / "perm.json")
    store.set_tier(APP, safety.Tier.FULL)
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=fake)
    a = AnthropicComputerAdapter(rt, app=None)
    assert a.handle({"action": "left_click", "coordinate": [160, 104]}).snapped_ref == "e3"
    front["app"] = "com.example.Dialog"  # ungranted app now frontmost; the refresh is refused
    r = a.handle({"action": "left_click", "coordinate": [160, 104]})
    assert fake.snapshots == 1 and rt._current.snapshot_id == "snap-1"  # still the Editor's tree
    assert r.snapped_ref is None and fake.calls["press"] == ["e3"]  # no second AX press
    assert r.error == "refused"  # the raw coordinate click is gated against the frontmost app
    # Set-of-Mark labels are likewise only drawn from a fresh tree: an adapter bound
    # to an ungranted app cannot refresh, so no labels from the Editor's stale tree
    front["app"] = APP  # screenshots gate against the frontmost app again
    marked = AnthropicComputerAdapter(rt, app="com.example.Other", marks=True).handle({"action": "screenshot"})
    assert marked.ok and fake.snapshots == 1 and rt._current.snapshot_id == "snap-1"
    plain = AnthropicComputerAdapter(rt, app="com.example.Other").handle({"action": "screenshot"})
    assert marked.png == plain.png


def test_screenshot_mapping_is_normalised_to_the_display_not_the_png(runtime, fake) -> None:
    """A driver whose PNG is larger than its Display (a DPR-2 browser capture) must
    not shift every click by that ratio: the coordinate contract is the Display,
    which is what the Runtime's screenshot text tells the model to multiply by."""
    fake.screenshot = lambda display_id=None: capture.Screenshot(png=_png(3200, 2000), display=DISPLAY)
    a = AnthropicComputerAdapter(runtime, app=APP)
    shot = a.handle({"action": "screenshot"})
    assert shot.ok and _image_size(shot.png) == (1280, 800)
    assert (a.screen.source_width, a.screen.source_height) == (1600, 1000)
    r = a.handle({"action": "left_click", "coordinate": [160, 104]})
    assert r.snapped_ref == "e3" and fake.calls["press"] == ["e3"]
    a.snap_to_refs = False
    a.handle({"action": "left_click", "coordinate": [160, 104]})
    assert fake.calls["click"][0][0] == Point(1, 200, 130)  # not (400, 260)


def test_marks_draw_refs_on_the_screenshot(runtime) -> None:
    plain = AnthropicComputerAdapter(runtime, app=APP).handle({"action": "screenshot"}).png
    marked = AnthropicComputerAdapter(runtime, app=APP, marks=True).handle({"action": "screenshot"}).png
    assert plain != marked and _image_size(marked) == (1280, 800)
    colors = Image.open(io.BytesIO(marked)).convert("RGB").getcolors(maxcolors=100000)
    assert any(px == (255, 40, 40) for _n, px in colors)  # Set-of-Mark ink


# --- every Anthropic action -----------------------------------------------------


@pytest.mark.parametrize("action,button,count", [
    ("left_click", "left", 1), ("right_click", "right", 1), ("middle_click", "middle", 1),
    ("double_click", "left", 2), ("triple_click", "left", 3),
])
def test_click_variants_map_button_and_count(anthropic, fake, action, button, count) -> None:
    anthropic.snap_to_refs = False
    r = anthropic.handle({"action": action, "coordinate": [10, 20], "text": "ctrl+shift"})
    assert r.ok, r.text
    target, b, c, mods = fake.calls["click"][0]
    assert (b, c) == (button, count) and set(mods) == {"ctrl", "shift"}
    assert target == Point(1, 12, 25)


def test_click_without_coordinate_uses_the_last_pointer_position(anthropic, fake) -> None:
    anthropic.snap_to_refs = False
    assert anthropic.handle({"action": "mouse_move", "coordinate": [100, 50]}).ok
    assert anthropic.handle({"action": "cursor_position"}).text == "X=100, Y=50"
    r = anthropic.handle({"action": "left_click"})
    assert r.ok and fake.calls["click"][0][0] == Point(1, 125, 62)


def test_left_click_drag_maps_both_points(anthropic, fake) -> None:
    r = anthropic.handle({"action": "left_click_drag", "start_coordinate": [8, 8],
                          "coordinate": [808, 408]})
    assert r.ok
    start, end = fake.calls["drag"][0]
    assert start == Point(1, 10, 10) and end == Point(1, 1010, 510)


def test_mouse_down_up_becomes_a_drag_or_a_click(anthropic, fake) -> None:
    anthropic.snap_to_refs = False
    assert anthropic.handle({"action": "left_mouse_up"}).error == "invalid"  # no down
    assert anthropic.handle({"action": "left_mouse_down"}).error == "invalid"  # no position
    anthropic.handle({"action": "mouse_move", "coordinate": [8, 8]})
    assert anthropic.handle({"action": "left_mouse_down"}).ok
    anthropic.handle({"action": "mouse_move", "coordinate": [408, 208]})
    assert anthropic.handle({"action": "left_mouse_up"}).ok
    assert fake.calls["drag"] == [(Point(1, 10, 10), Point(1, 510, 260))]
    anthropic.handle({"action": "left_mouse_down", "coordinate": [16, 16]})
    assert anthropic.handle({"action": "left_mouse_up"}).ok
    assert fake.calls["click"][-1][0] == Point(1, 20, 20)


@pytest.mark.parametrize("direction,dx,dy", [
    ("down", 0, 3), ("up", 0, -3), ("right", 3, 0), ("left", -3, 0),
])
def test_scroll_direction_maps_to_runtime_sign_convention(anthropic, fake, direction, dx, dy) -> None:
    r = anthropic.handle({"action": "scroll", "coordinate": [640, 400],
                          "scroll_direction": direction, "scroll_amount": 3})
    assert r.ok, r.text
    target, got_dx, got_dy, unit = fake.calls["scroll"][0]
    assert (target, got_dx, got_dy, unit) == (Point(1, 800, 500), dx, dy, "lines")


def test_scroll_without_coordinate_uses_cursor_then_center(anthropic, fake) -> None:
    anthropic.handle({"action": "scroll", "scroll_direction": "down", "scroll_amount": 1})
    assert fake.calls["scroll"][0][0] == Point(1, 800, 500)  # image center 640,400 -> physical
    anthropic.handle({"action": "mouse_move", "coordinate": [0, 0]})
    anthropic.handle({"action": "scroll", "scroll_direction": "down", "scroll_amount": 1})
    assert fake.calls["scroll"][1][0] == Point(1, 0, 0)


def test_type_and_key_and_repeat(anthropic, fake) -> None:
    assert anthropic.handle({"action": "type", "text": "hello"}).ok
    assert fake.calls["type"] == ["hello"]
    r = anthropic.handle({"action": "key", "text": "ctrl+s"})
    assert r.ok and fake.calls["key"] == ["ctrl+s"]
    r = anthropic.handle({"action": "key", "text": "Return", "repeat": 3})
    assert r.ok and "(x3)" in r.text and fake.calls["key"][1:] == ["return"] * 3


def test_hold_key_is_an_honest_approximation(anthropic, fake) -> None:
    r = anthropic.handle({"action": "hold_key", "text": "shift", "duration": 2})
    assert r.error == "invalid"  # a lone modifier is not a chord
    r = anthropic.handle({"action": "hold_key", "text": "alt+Tab", "duration": 2})
    assert r.ok and "approximation" in r.text and fake.calls["key"] == ["alt+tab"]


def test_wait_is_capped(runtime) -> None:
    a = AnthropicComputerAdapter(runtime, app=APP, max_wait_s=0.01)
    r = a.handle({"action": "wait", "duration": 60})
    assert r.ok and r.text == "waited 0.01s"


def test_zoom_maps_region_to_physical_and_fits_the_screenshot(anthropic, fake) -> None:
    r = anthropic.handle({"action": "zoom", "region": [80, 80, 240, 128]})
    assert r.ok and r.png is not None
    region = fake.calls["zoom"][0]
    assert region == Bounds(1, 100, 100, 200, 60)
    assert _image_size(r.png) == (200, 60)  # already fits: returned at native resolution
    big = anthropic.handle({"action": "zoom", "region": [0, 0, 1280, 800]})
    w, h = _image_size(big.png)
    assert w <= 1280 and h <= 800 and (w, h) == (1280, 800)
    assert anthropic.handle({"action": "zoom", "region": [5, 5, 5, 9]}).error == "invalid"


def test_unknown_action_and_missing_fields_are_readable_errors(anthropic) -> None:
    r = anthropic.handle({"action": "teleport"})
    assert r.error == "invalid" and "unknown computer action 'teleport'" in r.text
    assert anthropic.handle({}).error == "invalid"
    r = anthropic.handle({"action": "left_click_drag", "coordinate": [1, 2]})
    assert r.error == "invalid" and "start_coordinate" in r.text
    assert anthropic.handle({"action": "key", "text": "hyperkey+q"}).error == "invalid"


def test_handle_tool_use_dispatches_toolset_and_legacy_blocks(anthropic, fake) -> None:
    toolset = {"type": "tool_use", "id": "toolu_a", "name": "type", "toolset_name": "computer",
               "input": {"text": "hi"}}
    block = anthropic.handle_tool_use(toolset)
    assert block["tool_use_id"] == "toolu_a" and block["toolset_name"] == "computer"
    assert block["content"] == [{"type": "text", "text": "typed 2 characters"}]
    legacy = {"type": "tool_use", "id": "toolu_b", "name": "computer",
              "input": {"action": "key", "text": "Escape"}}
    block = anthropic.handle_tool_use(legacy)
    assert "toolset_name" not in block and block["content"][0]["text"] == "pressed escape"
    assert fake.calls["key"] == ["escape"]


# --- safety surfaces as text ----------------------------------------------------


def test_secure_field_refusal_surfaces_as_text(anthropic, fake) -> None:
    fake.secure_focus = True
    r = anthropic.handle({"action": "type", "text": "hunter2"})
    assert r.error == "secure_field" and r.text.startswith("secure_field:")
    assert fake.calls["type"] == []


def test_missing_grant_is_a_refusal_not_an_exception(tmp_path, fake) -> None:
    rt = server.Runtime(store=safety.PermissionStore(tmp_path / "p.json"),
                        audit=safety.AuditLog(tmp_path / "a"), driver=fake)
    a = AnthropicComputerAdapter(rt, app=APP)
    r = a.handle({"action": "screenshot"})  # READ is refused for an ungranted app
    assert r.error == "refused" and r.text.startswith("needs_permission")
    r = a.handle({"action": "left_click", "coordinate": [1, 1]})
    assert r.error == "refused"


def test_structured_error_text_carries_the_code_exactly_once(anthropic) -> None:
    def boom() -> None:
        raise ComputerUseError(ErrorCode.APP_NOT_FOUND, "no page targets at the CDP endpoint")

    r = anthropic._guard("screenshot", boom)
    assert r.error == "app_not_found"
    assert r.text.startswith("app_not_found: no page targets")
    assert r.text.count("app_not_found") == 1  # `Result.text` is the full line; never re-prefix it


def test_irreversible_click_without_confirmer_is_blocked(runtime, fake, monkeypatch) -> None:
    monkeypatch.setattr(server, "CONFIRMATION_GATE", True)

    def snapshot(scope, app):
        snap = _snapshot("snap-del")
        trash = Element("e9", "AXButton", "Delete", None, Bounds(1, 100, 100, 200, 60), "snap-del",
                        parent="e1", path=("AXWindow", "AXButton"), clickable=True)
        return Snapshot("snap-del", snap.scope, snap.app, snap.pid, 0.0, snap.displays,
                        snap.elements[:2] + (trash,) + snap.elements[3:])

    fake.snapshot = snapshot  # type: ignore[method-assign]
    a = AnthropicComputerAdapter(runtime, app=APP)
    r = a.handle({"action": "left_click", "coordinate": [160, 104]})
    assert r.error == "confirmation_declined" and fake.calls["press"] == []
    confirmed = AnthropicComputerAdapter(runtime, app=APP, confirm=lambda prompt: True)
    assert confirmed.handle({"action": "left_click", "coordinate": [160, 104]}).snapped_ref == "e9"


def test_every_action_is_audited(anthropic, tmp_path) -> None:
    anthropic.handle({"action": "screenshot"})
    anthropic.handle({"action": "type", "text": "x"})
    lines = []
    for p in sorted((tmp_path / "audit").glob("*.jsonl")):
        lines += [json.loads(line) for line in p.read_text().splitlines()]
    kinds = [e.get("action") for e in lines]
    assert "observeop" in kinds and "typetext" in kinds


# --- key names -----------------------------------------------------------------


@pytest.mark.parametrize("text,driver,chord", [
    ("Return", "macos", "return"), ("KP_Enter", "browser", "return"),
    ("ctrl+s", "macos", "ctrl+s"), ("Control_L+S", "macos", "ctrl+s"),
    ("super+shift+4", "macos", "cmd+shift+4"), ("cmd+shift+4", "linux", "cmd+shift+4"),
    ("alt+Tab", "windows", "alt+tab"), ("BackSpace", "macos", "backspace"),
    ("Delete", "macos", "forward_delete"), ("Delete", "browser", "delete"),
    ("Escape", "macos", "escape"), ("Page_Down", "linux", "pagedown"),
    ("Prior", "linux", "pageup"), ("Left", "browser", "left"), ("F5", "macos", "f5"),
    ("ctrl+minus", "macos", "ctrl+minus"), ("ctrl+minus", "browser", "ctrl+-"),
    ("ctrl+-", "macos", "ctrl+minus"), ("space", "macos", "space"),
    ("ctrl+ctrl+a", "macos", "ctrl+a"),
])
def test_xdotool_to_chord(text, driver, chord) -> None:
    assert xdotool_to_chord(text, driver) == chord


@pytest.mark.parametrize("bad", ["", "ctrl", "ctrl+", "hyper+a", "ctrl+Bogus", "F13"])
def test_xdotool_to_chord_rejects(bad) -> None:
    with pytest.raises(ValueError):
        xdotool_to_chord(bad, "macos")


@pytest.mark.parametrize("keys,driver,chords", [
    (["CTRL", "A"], "macos", ["ctrl+a"]), (["ENTER"], "macos", ["return"]),
    (["META", "SHIFT", "ARROWLEFT"], "windows", ["cmd+shift+left"]),
    (["ESCAPE"], "browser", ["escape"]), (["ALT", "F4"], "windows", ["alt+f4"]),
    (["CTRL", "C", "V"], "linux", ["ctrl+c", "ctrl+v"]),
    (["DELETE"], "macos", ["forward_delete"]), (["BACKSPACE"], "browser", ["backspace"]),
    (["PAGEDOWN"], "macos", ["pagedown"]), (["SPACE"], "linux", ["space"]),
])
def test_openai_keys_to_chords(keys, driver, chords) -> None:
    assert openai_keys_to_chords(keys, driver) == chords


def test_openai_keys_to_chords_rejects_modifier_only() -> None:
    with pytest.raises(ValueError):
        openai_keys_to_chords(["CTRL", "SHIFT"], "macos")


# --- tool definitions -------------------------------------------------------------


def test_anthropic_tool_definitions_match_the_documented_shapes(anthropic) -> None:
    assert anthropic.tool_definition() == {"type": "computer_toolset_20260801"}
    assert AnthropicComputerAdapter.beta_header() is None
    legacy = anthropic.tool_definition("computer_20251124")
    assert legacy == {"type": "computer_20251124", "name": "computer",
                      "display_width_px": 1280, "display_height_px": 800, "enable_zoom": True}
    assert AnthropicComputerAdapter.beta_header("computer_20251124") == "computer-use-2025-11-24"
    older = anthropic.tool_definition("computer_20250124")
    assert set(older) == {"type", "name", "display_width_px", "display_height_px"}
    assert AnthropicComputerAdapter.beta_header("computer_20250124") == "computer-use-2025-01-24"
    with pytest.raises(ValueError):
        anthropic.tool_definition("computer_20241022")


def test_anthropic_definition_options(runtime) -> None:
    a = AnthropicComputerAdapter(runtime, app=APP, display_width_px=1024, display_height_px=768,
                                 display_number=1, enable_zoom=False)
    assert a.max_long_edge == 1024
    assert a.tool_definition() == {"type": "computer_toolset_20260801",
                                   "configs": {"zoom": {"enabled": False}}}
    legacy = a.tool_definition("computer_20251124")
    # the declared size is the image Claude actually receives (1600x1000 at a 1024 long edge)
    assert legacy["display_width_px"] == 1024 and legacy["display_height_px"] == 640
    assert legacy["display_number"] == "1" and "enable_zoom" not in legacy
    assert anthropic_computer.ACTIONS >= {"screenshot", "zoom", "left_click", "key", "wait"}
    assert len(anthropic_computer.ACTIONS) == 17


def test_openai_tool_definitions(openai) -> None:
    assert openai.tool_definition() == {"type": "computer"}
    preview = openai.tool_definition(preview=True)
    assert preview == {"type": "computer_use_preview", "display_width": 1280,
                       "display_height": 800, "environment": "mac"}
    assert OpenAIComputerAdapter(openai.runtime).environment() == "ubuntu"  # unknown driver
    assert openai_computer.ACTIONS == {"click", "double_click", "drag", "keypress", "move",
                                       "screenshot", "scroll", "type", "wait"}


# --- OpenAI actions -----------------------------------------------------------------


def test_openai_click_variants(openai, fake) -> None:
    openai.snap_to_refs = False
    assert openai.handle({"type": "click", "x": 640, "y": 400, "button": "wheel"}).ok
    assert fake.calls["click"][0] == (Point(1, 800, 500), "middle", 1, ())
    assert openai.handle({"type": "click", "x": 8, "y": 8, "button": "left", "keys": ["SHIFT"]}).ok
    assert fake.calls["click"][1] == (Point(1, 10, 10), "left", 1, ("shift",))
    assert openai.handle({"type": "double_click", "x": 8, "y": 8}).ok
    assert fake.calls["click"][2][2] == 2
    r = openai.handle({"type": "click", "x": 8, "y": 8, "button": "back"})
    assert r.ok and "history shortcut" in r.text and fake.calls["key"] == ["alt+left"]
    assert openai.handle({"type": "click", "x": 8, "y": 8, "button": "sideways"}).error == "invalid"
    assert openai.handle({"type": "click", "x": 8}).error == "invalid"


def test_openai_click_snaps_like_anthropic(openai, fake) -> None:
    r = openai.handle({"type": "click", "x": 160, "y": 104, "button": "left"})
    assert r.ok and r.snapped_ref == "e3" and fake.calls["press"] == ["e3"]


def test_openai_drag_keypress_move_scroll_type_wait(openai, fake) -> None:
    r = openai.handle({"type": "drag", "path": [{"x": 8, "y": 8}, [100, 100], {"x": 808, "y": 408}]})
    assert r.ok and "1 intermediate path points dropped" in r.text
    assert fake.calls["drag"][0] == (Point(1, 10, 10), Point(1, 1010, 510))
    assert openai.handle({"type": "drag", "path": [{"x": 1, "y": 1}]}).error == "invalid"
    r = openai.handle({"type": "keypress", "keys": ["CTRL", "A"]})
    assert r.ok and fake.calls["key"] == ["ctrl+a"]
    r = openai.handle({"type": "keypress", "keys": ["CTRL", "C", "V"]})
    assert r.ok and r.text == "pressed ctrl+c, ctrl+v"
    assert openai.handle({"type": "keypress", "keys": []}).error == "invalid"
    assert openai.handle({"type": "move", "x": 5, "y": 6}).ok and openai.cursor == (5, 6)
    r = openai.handle({"type": "scroll", "x": 640, "y": 400, "scroll_x": 0, "scroll_y": 300})
    assert r.ok and fake.calls["scroll"][0] == (Point(1, 800, 500), 0, 300, "pixels")
    assert openai.handle({"type": "type", "text": "abc"}).ok and fake.calls["type"] == ["abc"]
    openai.default_wait_s = 0.001
    assert openai.handle({"type": "wait"}).ok
    assert openai.handle({"type": "levitate"}).error == "invalid"
    assert openai.handle({}).error == "invalid"


def test_openai_handle_call_builds_the_output_item(openai, fake) -> None:
    call = {
        "type": "computer_call", "call_id": "call_1", "status": "completed",
        "actions": [{"type": "click", "x": 160, "y": 104, "button": "left"},
                    {"type": "type", "text": "penguin"}],
        "pending_safety_checks": [{"id": "cu_sc_1", "code": "malicious_instructions",
                                   "message": "check"}],
    }
    # flagged and not acknowledged: NOTHING runs, every action is a refused Result,
    # the model still gets its screenshot and no acknowledgement is echoed
    output, results = openai.handle_call(call)
    assert output["type"] == "computer_call_output" and output["call_id"] == "call_1"
    assert output["output"]["type"] == "computer_screenshot"
    assert output["output"]["image_url"].startswith("data:image/png;base64,")
    assert "acknowledged_safety_checks" not in output
    assert [r.action for r in results] == ["click", "type", "screenshot"]
    assert [r.error for r in results[:-1]] == ["refused", "refused"]
    assert "malicious_instructions" in results[0].text and results[0].snapped_ref is None
    assert fake.calls["press"] == [] and fake.calls["click"] == [] and fake.calls["type"] == []

    # acknowledging (after a human confirmed) authorises the run and is echoed back
    output, results = openai.handle_call(call, acknowledge_safety_checks=True, preview=True)
    assert output["acknowledged_safety_checks"] == call["pending_safety_checks"]
    assert output["output"]["type"] == "input_image"
    assert results[0].snapped_ref == "e3" and fake.calls["type"] == ["penguin"]


def test_openai_handle_call_stops_at_the_first_failure_and_still_screenshots(openai, fake) -> None:
    fake.secure_focus = True
    call = {"call_id": "c", "actions": [{"type": "type", "text": "secret"}, {"type": "wait"}]}
    output, results = openai.handle_call(call)
    assert [r.action for r in results] == ["type", "screenshot"]
    assert results[0].error == "secure_field"
    assert output["output"]["image_url"].startswith("data:image/png;base64,")
    single = {"call_id": "d", "action": {"type": "screenshot"}}  # preview shape
    output, results = openai.handle_call(single)
    assert [r.action for r in results] == ["screenshot"] and output["call_id"] == "d"


# --- browser backend, hermetic (scripted CDP transport) ------------------------------


def test_anthropic_adapter_on_the_browser_driver_snaps_to_a_dom_click(tmp_path) -> None:
    from computeruse.drivers import _cdp, browser
    from tests.test_browser import ScriptedTransport, _fixture_responder

    real_png = _png(800, 600)

    def responder(method, params):
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(real_png).decode()}
        return _fixture_responder(method, params)

    d = browser.BrowserDriver(endpoint="http://scripted")
    transport = ScriptedTransport(responder)
    d._session = _cdp.CDPSession(transport)
    d._target_id = "TAB1"
    store = safety.PermissionStore(tmp_path / "perm.json")
    store.set_tier("TAB1", safety.Tier.FULL)
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=d)
    a = AnthropicComputerAdapter(rt)  # app=None: the bound tab

    shot = a.handle({"action": "screenshot"})
    assert shot.ok and _image_size(shot.png) == (800, 600) and a.display_id == 0
    # the fixture's Save button is at (8, 8, 80, 30) in page CSS px (scale 1.0)
    transport.sent.clear()
    r = a.handle({"action": "left_click", "coordinate": [48, 23]})
    assert r.ok and r.snapped_ref is not None and "Save" in r.text
    assert any("this.click()" in p.get("functionDeclaration", "")
               for m, p in transport.sent if m == "Runtime.callFunctionOn")
    assert not any(m == "Input.dispatchMouseEvent" for m, _ in transport.sent)
    o = OpenAIComputerAdapter(rt)
    assert o.environment() == "browser"
    assert o.current_url() is None  # target discovery needs a real endpoint; never raises


# --- live: real headless Chromium (opt-in) ---------------------------------------------


def _live_endpoint() -> str | None:
    from computeruse.drivers import _cdp

    endpoint = os.environ.get("COMPUTERUSE_CDP_ENDPOINT", "http://127.0.0.1:9222")
    try:
        _cdp.page_targets(endpoint)
        return endpoint
    except Exception:
        return None


_LIVE_PAGE = (
    "data:text/html,"
    "<html><body style='margin:0;font:16px sans-serif'>"
    "<h1 id=h>Adapter live page</h1>"
    "<input id=name aria-label='Name' style='position:absolute;left:20px;top:80px;width:300px;height:30px'>"
    "<button id=go style='position:absolute;left:20px;top:140px;width:120px;height:40px'"
    " onclick=\"document.getElementById('h').textContent='clicked '+document.getElementById('name').value\">Go</button>"
    "</body></html>"
)


@pytest.mark.skipif(_live_endpoint() is None,
                    reason="no live CDP endpoint (set COMPUTERUSE_CDP_ENDPOINT / run Chrome "
                           "--remote-debugging-port)")
def test_live_anthropic_adapter_snaps_a_pixel_click_to_the_button(tmp_path) -> None:
    from computeruse.drivers import browser

    d = browser.BrowserDriver(endpoint=_live_endpoint())
    tab = d.frontmost_app()[0]
    store = safety.PermissionStore(tmp_path / "perm.json")
    store.set_tier(tab, safety.Tier.FULL)
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=d)
    rt.app("launch", _LIVE_PAGE)
    a = AnthropicComputerAdapter(rt)

    shot = a.handle({"action": "screenshot"})
    assert shot.ok and shot.png
    # locate the field and button through the a11y tree, then click at their
    # pixel centers the way a pixel-loop model would
    rt.desktop_snapshot(tab)
    field = next(e for e in rt._current.elements if e.title == "Name")
    button = next(e for e in rt._current.elements if e.title == "Go")
    fx, fy = a.from_physical(field.bounds.center.x, field.bounds.center.y)
    bx, by = a.from_physical(button.bounds.center.x, button.bounds.center.y)

    r = a.handle({"action": "left_click", "coordinate": [fx, fy]})
    assert r.ok and r.snapped_ref == field.ref, r.text
    assert a.handle({"action": "type", "text": "world"}).ok
    r = a.handle({"action": "left_click", "coordinate": [bx, by]})
    assert r.ok and r.snapped_ref == button.ref, r.text
    after = rt.desktop_snapshot(tab)
    assert "clicked world" in after
