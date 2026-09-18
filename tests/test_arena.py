"""cu-arena — the a11y-vs-vision observation-cost benchmark.

Hermetic tests pin the token math and the driver-agnostic measurement (a fake
driver returns a scripted snapshot + a PNG of known dimensions, so both costs are
deterministic). One opt-in live test runs it against real headless Chromium and
just asserts both costs are real and positive — the ratio is the artifact, never
an assertion, so the benchmark stays honest.
"""

from __future__ import annotations

import json
import os
import types

import pytest

import a11y_computer_use.observe as _observe
from a11y_computer_use import arena
from a11y_computer_use.schema import (
    Bounds, ComputerUseError, Display, Element, ErrorCode, Scope, Snapshot,
)
from tests.conftest import HAS_AX, HAS_DISPLAYS, HAS_SCREEN


def _png(width: int, height: int) -> bytes:
    """A minimal valid PNG header (signature + IHDR w/h) — enough for `_png_size`."""
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR"
            + width.to_bytes(4, "big") + height.to_bytes(4, "big"))


def test_image_tokens_uses_published_vision_formula() -> None:
    assert arena.image_tokens(1000, 750) == 1000  # (w·h)/750
    assert arena.image_tokens(1280, 800) == round(1280 * 800 / 750)
    assert arena.image_tokens(0, 500) == 0 and arena.image_tokens(-1, 10) == 0


def test_a11y_tokens_matches_cu_meter_basis() -> None:
    assert arena.a11y_tokens("") == 0
    assert arena.a11y_tokens("abcd") == 1 and arena.a11y_tokens("abcde") == 2  # ceil(len/4)


def test_png_size_reads_header_without_pil() -> None:
    assert arena._png_size(_png(1234, 567)) == (1234, 567)
    assert arena._png_size(b"not a png") == (0, 0)


class _FakeDriver:
    """A minimal Driver stand-in for the measurement (no browser)."""

    def __init__(self, elements: int, render_chars: int, png: bytes, scale: float = 1.0):
        self._elements = elements
        self._chars = render_chars
        self._png = png
        self._scale = scale
        self.navigated: list[str] = []

    def frontmost_app(self):
        return "TAB", None

    def navigate(self, url, **kw):
        self.navigated.append(url)

    def snapshot(self, scope, app):
        els = tuple(
            Element(ref=f"e{i}", role="AXButton", title="X" * 0, value=None,
                    bounds=Bounds(0, 0, 0, 10, 10), snapshot_id="s", clickable=True)
            for i in range(self._elements)
        )
        return Snapshot(snapshot_id="s", scope=Scope.WINDOW, app=app, pid=1, created_at=0.0,
                        displays=(Display(0, 800, 600, 1.0, True),), elements=els)

    def screenshot(self, display_id=None):
        return types.SimpleNamespace(png=self._png,
                                     display=Display(0, 1600, 1200, self._scale, True))


def test_measure_observation_records_both_costs(monkeypatch) -> None:
    # Pin the a11y text length so the token math is exact, independent of render().
    monkeypatch.setattr(_observe, "render_text", lambda snap, **kw: "y" * 400)
    d = _FakeDriver(elements=12, render_chars=400, png=_png(1600, 1200), scale=2.0)
    cost = arena.measure_observation(d, "TAB")
    assert cost.a11y_tokens == 100  # 400 chars / 4
    # physical 1600x1200 at scale 2.0 -> 800x600 CSS px -> (800*600)/750
    assert cost.screenshot_tokens == arena.image_tokens(800, 600)
    assert cost.element_count == 12 and cost.image_width == 1600
    assert cost.ratio == cost.screenshot_tokens / cost.a11y_tokens


def test_run_web_task_navigates_and_measures_each_round(monkeypatch) -> None:
    monkeypatch.setattr(_observe, "render_text", lambda snap, **kw: "z" * 40)
    d = _FakeDriver(elements=3, render_chars=40, png=_png(1280, 800))
    report = arena.run_web_task(d, "https://example.com", rounds=3)
    assert d.navigated == ["https://example.com"]
    assert len(report.observations) == 3
    assert report.total_a11y_tokens == 3 * arena.a11y_tokens("z" * 40)
    assert report.ratio > 1  # a full 1280x800 frame dwarfs a 40-char a11y snapshot
    # re-observations are scored as diffs of the prior snapshot (rounds-1 of them);
    # an unchanged page diffs cheap — the non-accumulation moat.
    assert len(report.reobserve_a11y_tokens) == 2
    assert report.avg_reobserve_tokens <= report.observations[0].a11y_tokens
    assert "cheaper per observation" in arena.format_report(report)
    assert "non-accumulation moat" in arena.format_report(report)


def test_format_report_handles_empty() -> None:
    assert "no observations" in arena.format_report(arena.Report())


# --------------------------------------------------------------------------- #
# live: real headless Chromium (opt-in)
# --------------------------------------------------------------------------- #
def _live_endpoint() -> str | None:
    from a11y_computer_use.drivers import _cdp

    endpoint = os.environ.get("A11Y_COMPUTER_USE_CDP_ENDPOINT", "http://127.0.0.1:9222")
    try:
        _cdp.page_targets(endpoint)
        return endpoint
    except Exception:
        return None


@pytest.mark.skipif(_live_endpoint() is None,
                    reason="no live CDP endpoint (set A11Y_COMPUTER_USE_CDP_ENDPOINT / run Chrome "
                           "--remote-debugging-port=9222)")
def test_live_arena_measures_real_costs() -> None:
    from a11y_computer_use.drivers.browser import BrowserDriver

    d = BrowserDriver(endpoint=_live_endpoint())
    page = ("data:text/html,<h1>Title</h1><button>A</button><button>B</button>"
            "<input placeholder=Name><a href=x>link</a>")
    report = arena.run_web_task(d, page, rounds=2)
    d._reset()
    assert len(report.observations) == 2
    o = report.observations[0]
    # both costs are real and positive; the ratio is reported, never asserted
    assert o.a11y_tokens > 0 and o.screenshot_tokens > 0 and o.element_count > 0
    print("\n" + arena.format_report(report))


# --------------------------------------------------------------------------- #
# desktop: every view of one snapshot against one screenshot
# --------------------------------------------------------------------------- #
def _mixed_snapshot(app: str, status: str = "Ready") -> Snapshot:
    """A window with two buttons and three titled static texts (``status`` is
    the value of one of them, so a second round can flip it)."""
    def el(ref, role, title, value, bounds, **kw):
        return Element(ref=ref, role=role, title=title, value=value, bounds=bounds,
                       snapshot_id="s", parent="e1", path=("AXWindow", role), **kw)
    els = (
        Element(ref="e1", role="AXWindow", title="W", value=None, bounds=Bounds(0, 0, 0, 800, 600),
                snapshot_id="s", path=("AXWindow",)),
        el("e2", "AXButton", "Save", None, Bounds(0, 10, 10, 80, 30), clickable=True),
        el("e3", "AXStaticText", "greeting",
           "Welcome to the app. Your workspace synced 4 minutes ago and nothing needs attention.",
           Bounds(0, 10, 50, 300, 20)),
        el("e4", "AXStaticText", "status", status, Bounds(0, 10, 80, 300, 20)),
        el("e5", "AXButton", "Cancel", None, Bounds(0, 100, 10, 80, 30), clickable=True),
        el("e6", "AXStaticText", "footer",
           "Copyright notice, version 1.2.3, build 4567, licensed under Apache-2.0 to you.",
           Bounds(0, 10, 500, 300, 20)),
    )
    return Snapshot(snapshot_id="s", scope=Scope.WINDOW, app=app, pid=1, created_at=0.0,
                    displays=(Display(0, 800, 600, 1.0, True),), elements=els)


class _ModesDriver:
    """A Driver stand-in whose second snapshot flips one static label."""

    def __init__(self, *, fail_shot: bool = False) -> None:
        self._fail = fail_shot
        self.calls = 0

    def frontmost_app(self):
        return "APP", None

    def ensure_trusted(self) -> None:
        pass

    def snapshot(self, scope, app):
        self.calls += 1
        return _mixed_snapshot(app, status="Ready" if self.calls == 1 else "Saved")

    def screenshot(self, display_id=None):
        if self._fail:
            raise ComputerUseError(ErrorCode.PERMISSION_DENIED_SCREEN, "no Screen Recording grant")
        return types.SimpleNamespace(png=_png(1600, 1200), display=Display(0, 1600, 1200, 2.0, True))


def test_run_desktop_task_costs_every_view_of_the_same_snapshot() -> None:
    rep = arena.run_desktop_task(_ModesDriver(), rounds=2)  # app defaults to frontmost
    assert rep.app == "APP" and rep.rounds == 2 and rep.element_count == 6
    assert set(rep.modes) == {"full", "interactive"}
    full, inter = rep.modes["full"], rep.modes["interactive"]
    assert full.shown == 6 and inter.shown == 3  # root + the two buttons
    assert inter.avg_tokens < full.avg_tokens
    assert full.avg_tokens == arena.a11y_tokens(_observe.render_text(_mixed_snapshot("APP")))
    assert rep.avg_screenshot_tokens == arena.image_tokens(800, 600)  # 1600x1200 at 2x
    assert rep.ratio("interactive") > rep.ratio("full") > 0
    # the label flip between rounds is a text change: the full diff lists it, the
    # interactive diff folds it; both cost far less than a fresh observation
    assert len(full.reobserve_tokens) == len(inter.reobserve_tokens) == 1
    assert full.reobserve_tokens[0] < full.avg_tokens
    assert inter.reobserve_tokens[0] <= full.reobserve_tokens[0]
    data = rep.to_dict()
    json.dumps(data)
    assert data["modes"]["interactive"]["element_lines"] == 3
    assert data["screenshot"]["error"] == "" and data["rounds"] == 2
    out = arena.format_desktop_report(rep)
    assert "a11y full" in out and "a11y interactive" in out
    assert "re-observe diff" in out and "cheaper than a screenshot" in out


def test_desktop_report_reports_missing_screenshot_honestly() -> None:
    rep = arena.run_desktop_task(_ModesDriver(fail_shot=True), "APP", rounds=1)
    assert rep.screenshot_error == "permission_denied_screen"
    assert rep.avg_screenshot_tokens == 0 and rep.ratio("full") == 0.0
    assert rep.modes["full"].avg_tokens > 0  # the a11y side is still measured
    out = arena.format_desktop_report(rep)
    assert "unavailable (permission_denied_screen)" in out and "no ratio" in out
    assert "cheaper" not in out


def test_run_web_task_passes_mode_through(monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(_observe, "render_text",
                        lambda snap, **kw: seen.append(kw.get("mode")) or "q" * 8)
    d = _FakeDriver(elements=2, render_chars=8, png=_png(800, 600))
    rep = arena.run_web_task(d, "https://x", rounds=2, mode="interactive")
    assert seen == ["interactive", "interactive"] and rep.mode == "interactive"
    assert "interactive" in arena.format_report(rep)
    assert rep.to_dict()["mode"] == "interactive"
    assert rep.to_dict()["screenshot_error"] == ""


def test_web_report_states_missing_screenshot(monkeypatch) -> None:
    class _NoShot(_FakeDriver):
        def screenshot(self, display_id=None):
            raise ComputerUseError(ErrorCode.UNSUPPORTED, "no capture here")
    monkeypatch.setattr(_observe, "render_text", lambda snap, **kw: "q" * 8)
    rep = arena.run_web_task(_NoShot(elements=2, render_chars=8, png=b""), "https://x", rounds=1)
    assert rep.screenshot_error == "unsupported" and rep.ratio == 0.0
    out = arena.format_report(rep)
    assert "unavailable (unsupported)" in out and "cheaper" not in out


def test_format_desktop_report_handles_empty() -> None:
    assert "no observations" in arena.format_desktop_report(arena.DesktopReport(app="x"))


@pytest.mark.skipif(_live_endpoint() is None,
                    reason="no live CDP endpoint (set A11Y_COMPUTER_USE_CDP_ENDPOINT / run Chrome "
                           "--remote-debugging-port=9222)")
def test_live_desktop_task_on_browser_costs_all_views() -> None:
    from a11y_computer_use.drivers.browser import BrowserDriver

    d = BrowserDriver(endpoint=_live_endpoint())
    d.navigate("data:text/html,<h1>Title</h1><p>Some prose to fold</p><button>A</button>"
               "<a href=x>link</a><input placeholder=Name>")
    rep = arena.run_desktop_task(d, rounds=2)
    d._reset()
    full, inter = rep.modes["full"], rep.modes["interactive"]
    assert rep.rounds == 2 and full.avg_tokens > 0 and inter.avg_tokens > 0
    assert inter.avg_tokens <= full.avg_tokens and inter.shown <= full.shown
    print("\n" + arena.format_desktop_report(rep))


@pytest.mark.skipif(not (HAS_AX and HAS_SCREEN),
                    reason="needs the Accessibility and Screen Recording grants")
@pytest.mark.skipif(not HAS_DISPLAYS, reason="needs an unlocked window-server session reporting a display")
def test_live_desktop_task_on_finder_costs_all_views() -> None:
    from a11y_computer_use.drivers import get_driver

    rep = arena.run_desktop_task(get_driver("macos"), "com.apple.finder", rounds=2, scope=Scope.APP)
    assert rep.modes["full"].avg_tokens > 0 and rep.modes["interactive"].avg_tokens > 0
    print("\n" + arena.format_desktop_report(rep))
