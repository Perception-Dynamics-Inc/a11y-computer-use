"""Off-screen points and unknown displays are invalid_arguments.

On 0.4.38 a point past the display was clamped to the edge and reported as
the point that was asked for, an unknown display_id was treated as display 0,
and zoom of a region that missed the display returned a black image. These
tests use a fake driver. "The pointer did not move" means that driver's input
method was not called.
"""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from a11y_computer_use import cli, drivers, safety, server
from a11y_computer_use.drivers._uia import displays_from_monitors
from a11y_computer_use.schema import Bounds, Display

APP = "com.test.front"
_SCREEN = (Display(display_id=0, width=1280, height=800, scale=1.0, is_main=True),)
_OFFSCREEN = ((1280, 800), (-1, 0), (5000, 5000), (99999, 5))


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(buffer, format="PNG")
    return buffer.getvalue()


class _Driver:
    """One or more displays, and a record of every input and capture call."""

    def __init__(self, displays: tuple[Display, ...]) -> None:
        self.name = "linux"
        self._displays = displays
        self.calls: dict[str, list] = {
            "click": [], "hover": [], "scroll": [], "drag": [],
            "screenshot": [], "zoom": [], "type": [],
        }

    def displays(self):
        return self._displays

    def main_display_id(self) -> int:
        return self._displays[0].display_id

    def click(self, target, **kwargs) -> None:
        self.calls["click"].append(target)

    def hover(self, target, **kwargs) -> None:
        self.calls["hover"].append(target)

    def scroll(self, target, **kwargs) -> None:
        self.calls["scroll"].append(target)

    def drag(self, start, end, **kwargs) -> None:
        self.calls["drag"].append((start, end, kwargs.get("path")))

    def type_text(self, text, **kwargs) -> None:
        self.calls["type"].append(text)

    def screenshot(self, display_id=None):
        self.calls["screenshot"].append(display_id)
        from a11y_computer_use import capture

        return capture.Screenshot(png=_png(), display=self._displays[0])

    def zoom_region(self, region: Bounds) -> bytes:
        self.calls["zoom"].append(region)
        return _png()


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    driver = _Driver(_SCREEN)
    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier(APP, safety.Tier.FULL)
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: APP)
    monkeypatch.setattr(server, "_app_at_point", lambda point: None)
    monkeypatch.setattr(safety, "seconds_since_user_input", lambda: None)
    runtime = server.Runtime(
        store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver,
    )
    return runtime, driver


@pytest.mark.parametrize("x,y", _OFFSCREEN)
@pytest.mark.parametrize("tool", ("click", "hover", "scroll"))
def test_pointer_tools_reject_a_point_outside_the_display(desktop, tool, x, y) -> None:
    runtime, driver = desktop
    kwargs = {"x": x, "y": y}
    if tool == "scroll":
        kwargs["dy"] = 3
    with pytest.raises(ValueError, match="outside"):
        getattr(runtime, tool)(**kwargs)
    assert driver.calls[tool] == []


@pytest.mark.parametrize("x,y", _OFFSCREEN)
def test_drag_rejects_an_endpoint_outside_the_display(desktop, x, y) -> None:
    runtime, driver = desktop
    with pytest.raises(ValueError, match="outside"):
        runtime.drag(start_x=10, start_y=10, end_x=x, end_y=y)
    assert driver.calls["drag"] == []


def test_drag_rejects_a_waypoint_outside_the_display(desktop) -> None:
    runtime, driver = desktop
    with pytest.raises(ValueError, match=r"path\[0\].*outside"):
        runtime.drag(start_x=10, start_y=10, end_x=20, end_y=20, path=[[99999, 5]])
    assert driver.calls["drag"] == []


@pytest.mark.parametrize("x,y", _OFFSCREEN)
def test_act_step_rejects_an_offscreen_point_before_any_step_runs(desktop, x, y) -> None:
    runtime, driver = desktop
    steps = json.loads(runtime.act_batch([
        {"do": "type", "text": "earlier"},
        {"do": "hover", "x": x, "y": y},
    ]))
    assert steps == [{
        "i": 1,
        "do": "hover",
        "ok": False,
        "error": steps[0]["error"],
    }]
    assert steps[0]["error"].startswith("invalid_arguments: hover: step 1:")
    assert "outside" in steps[0]["error"]
    assert driver.calls["type"] == []
    assert driver.calls["hover"] == []


def test_last_pixel_is_inside_and_the_far_corner_is_not(desktop) -> None:
    runtime, driver = desktop
    assert runtime.click(x=1279, y=799) == "clicked (1279, 799) on display 0"
    assert (driver.calls["click"][-1].x, driver.calls["click"][-1].y) == (1279, 799)
    with pytest.raises(ValueError, match="outside"):
        runtime.click(x=1280, y=799)
    assert len(driver.calls["click"]) == 1


def test_unknown_display_id_names_the_valid_ids(desktop) -> None:
    runtime, driver = desktop
    with pytest.raises(ValueError, match=r"unknown display_id 7; valid ids: 0"):
        runtime.click(x=600, y=400, display_id=7)
    with pytest.raises(ValueError, match=r"unknown display_id 7; valid ids: 0"):
        runtime.screenshot(display_id=7)
    with pytest.raises(ValueError, match=r"unknown display_id 7; valid ids: 0"):
        runtime.zoom(7, 0, 0, 40, 20)
    assert driver.calls["click"] == []
    assert driver.calls["screenshot"] == []
    assert driver.calls["zoom"] == []


def test_zoom_rejects_a_region_that_misses_the_display_and_clips_a_partial_one(desktop) -> None:
    runtime, driver = desktop
    with pytest.raises(ValueError, match="entirely outside"):
        runtime.zoom(0, 5000, 5000, 40, 20)
    assert driver.calls["zoom"] == []

    _png_bytes, region = runtime.zoom(0, 1270, 790, 40, 20)
    assert region == Bounds(0, 1270, 790, 10, 10)
    assert driver.calls["zoom"] == [region]
    assert server.format_zoom(region) == "zoom of display 0 at (1270, 790) 10x10"

    _png_bytes, full = runtime.zoom(0, 0, 0, 99999, 20)
    assert full == Bounds(0, 0, 0, 1280, 20)


def test_local_origin_stays_valid_on_a_monitor_with_a_negative_global_origin(tmp_path, monkeypatch) -> None:
    """A monitor at virtual left -1920 is still addressed as local (0, 0)."""
    found = displays_from_monitors([
        (False, -1920, 0, 1920, 1080),
        (True, 0, 0, 1280, 800),
    ])
    assert [(item.display_id, item.width, item.height, item.is_main) for item in found] == [
        (0, 1280, 800, True),
        (1, 1920, 1080, False),
    ]
    driver = _Driver(found)
    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier(APP, safety.Tier.FULL)
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: APP)
    monkeypatch.setattr(server, "_app_at_point", lambda point: None)
    monkeypatch.setattr(safety, "seconds_since_user_input", lambda: None)
    runtime = server.Runtime(
        store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver,
    )
    assert runtime.click(x=0, y=0, display_id=1) == "clicked (0, 0) on display 1"
    assert driver.calls["click"][-1].display_id == 1
    assert (driver.calls["click"][-1].x, driver.calls["click"][-1].y) == (0, 0)
    with pytest.raises(ValueError, match="outside"):
        runtime.click(x=-1, y=0, display_id=1)
    with pytest.raises(ValueError, match="outside"):
        runtime.click(x=-10, y=0, display_id=1)
    assert len(driver.calls["click"]) == 1


def test_run_once_offscreen_exits_cleanly(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    driver = _Driver(_SCREEN)
    monkeypatch.setattr(drivers, "get_driver", lambda *a, **k: driver)
    assert cli.main(["run-once", json.dumps({"tool": "hover", "x": 99999, "y": 5})]) == 2
    err = capsys.readouterr().err
    assert err.startswith("invalid action:")
    assert "outside" in err
    assert "Traceback" not in err
    assert driver.calls["hover"] == []

    assert cli.main(["run-once", json.dumps({"tool": "click", "x": -10, "y": -10})]) == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "outside" in err
    assert driver.calls["click"] == []


def test_x_motion_rejects_a_coordinate_xlib_cannot_pack() -> None:
    from a11y_computer_use.drivers import _linux_input

    with pytest.raises(ValueError, match="X motion"):
        _linux_input._move(99999, 5)
    with pytest.raises(ValueError, match="X motion"):
        _linux_input._move(-40000, 0)
