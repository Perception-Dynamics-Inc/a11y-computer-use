"""cu-arena — the a11y-vs-vision observation-cost benchmark.

Hermetic tests pin the token math and the driver-agnostic measurement (a fake
driver returns a scripted snapshot + a PNG of known dimensions, so both costs are
deterministic). One opt-in live test runs it against real headless Chromium and
just asserts both costs are real and positive — the ratio is the artifact, never
an assertion, so the benchmark stays honest.
"""

from __future__ import annotations

import os
import types

import pytest

import computeruse.observe as _observe
from computeruse import arena
from computeruse.schema import Bounds, Display, Element, Scope, Snapshot


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
    monkeypatch.setattr(_observe, "render_text", lambda snap: "y" * 400)
    d = _FakeDriver(elements=12, render_chars=400, png=_png(1600, 1200), scale=2.0)
    cost = arena.measure_observation(d, "TAB")
    assert cost.a11y_tokens == 100  # 400 chars / 4
    # physical 1600x1200 at scale 2.0 -> 800x600 CSS px -> (800*600)/750
    assert cost.screenshot_tokens == arena.image_tokens(800, 600)
    assert cost.element_count == 12 and cost.image_width == 1600
    assert cost.ratio == cost.screenshot_tokens / cost.a11y_tokens


def test_run_web_task_navigates_and_measures_each_round(monkeypatch) -> None:
    monkeypatch.setattr(_observe, "render_text", lambda snap: "z" * 40)
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
    from computeruse.drivers import _cdp

    endpoint = os.environ.get("COMPUTERUSE_CDP_ENDPOINT", "http://127.0.0.1:9222")
    try:
        _cdp.page_targets(endpoint)
        return endpoint
    except Exception:
        return None


@pytest.mark.skipif(_live_endpoint() is None,
                    reason="no live CDP endpoint (set COMPUTERUSE_CDP_ENDPOINT / run Chrome "
                           "--remote-debugging-port=9222)")
def test_live_arena_measures_real_costs() -> None:
    from computeruse.drivers.browser import BrowserDriver

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
