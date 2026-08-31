"""cu-arena — the a11y-vs-vision observation-cost benchmark.

The moat claim is that an accessibility-first snapshot is far cheaper per
observation than the screenshot a vision agent must send, and — being text — it
*diffs* (re-observe after an action costs a tiny delta) where a screenshot pays
its full image-token cost every single step. This module turns that claim into a
reproducible **number**, measured on whatever `Driver` it is handed: for the live
page it records the a11y snapshot's token cost and the token cost of the exact
screenshot the driver captures at the same moment.

It is honest by construction — both raw numbers are reported, the methodology is
stated, and nothing is rigged: the a11y cost is the real rendered snapshot and
the image cost is the real captured frame's dimensions. Backend-agnostic (works
on macOS/Windows/Linux/browser); the browser backend makes it container-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from computeruse.schema import Scope

#: Chars per token for the a11y text cost — the same 4:1 basis cu-meter uses in
#: ``server.Runtime._run_gated`` (``tokens_est = (len+3)//4``), so arena and the
#: audit report speak the same units.
_CHARS_PER_TOKEN = 4

#: Claude vision cost: tokens ≈ (width × height) / 750 CSS px (per Anthropic's
#: published image-token estimate). This is the cost a screenshot-driven agent
#: pays for ONE observation frame.
_VISION_PX_PER_TOKEN = 750


def a11y_tokens(text: str) -> int:
    """Token estimate for an a11y snapshot's rendered text (cu-meter's basis)."""
    return (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def image_tokens(width: int, height: int) -> int:
    """Token estimate for one screenshot of ``width`` × ``height`` CSS pixels.

    Uses Anthropic's documented ``(w·h)/750`` vision estimate — the per-frame
    cost a vision agent pays that an a11y snapshot avoids.
    """
    if width <= 0 or height <= 0:
        return 0
    return round((width * height) / _VISION_PX_PER_TOKEN)


@dataclass(frozen=True, slots=True)
class ObsCost:
    """The two costs of a single observation of the same UI state."""

    a11y_tokens: int
    screenshot_tokens: int
    a11y_chars: int
    image_width: int
    image_height: int
    element_count: int

    @property
    def ratio(self) -> float:
        """How many times cheaper the a11y observation is (screenshot / a11y)."""
        return self.screenshot_tokens / self.a11y_tokens if self.a11y_tokens else 0.0


@dataclass(slots=True)
class Report:
    observations: list[ObsCost] = field(default_factory=list)
    #: a11y token cost of each RE-observation as a diff of the prior snapshot —
    #: the non-accumulation moat: ~0 when nothing changed, where a screenshot
    #: still pays its full image cost every step.
    reobserve_a11y_tokens: list[int] = field(default_factory=list)

    @property
    def total_a11y_tokens(self) -> int:
        return sum(o.a11y_tokens for o in self.observations)

    @property
    def total_screenshot_tokens(self) -> int:
        return sum(o.screenshot_tokens for o in self.observations)

    @property
    def ratio(self) -> float:
        a = self.total_a11y_tokens
        return self.total_screenshot_tokens / a if a else 0.0

    @property
    def avg_reobserve_tokens(self) -> float:
        r = self.reobserve_a11y_tokens
        return sum(r) / len(r) if r else 0.0


def _png_size(png: bytes) -> tuple[int, int]:
    """(width, height) from a PNG header — no PIL, no decode."""
    if len(png) >= 24 and png[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
    return 0, 0


def _measure(driver, app: str, scope: Scope) -> tuple["object", ObsCost]:
    """(snapshot, ObsCost) for one look — the real a11y snapshot and the real
    screenshot the driver captures for the SAME state, so the comparison is
    apples-to-apples and un-rigged. Returns the snapshot too, for diffing."""
    from computeruse import observe

    snap = driver.snapshot(scope, app)
    text = observe.render_text(snap)
    shot = driver.screenshot()
    width, height = _png_size(getattr(shot, "png", b""))
    # Screenshots are captured in physical px; the vision formula is CSS px, so
    # divide out the backing scale to count the pixels the model actually bills.
    scale = getattr(getattr(shot, "display", None), "scale", 1.0) or 1.0
    cost = ObsCost(
        a11y_tokens=a11y_tokens(text),
        screenshot_tokens=image_tokens(round(width / scale), round(height / scale)),
        a11y_chars=len(text),
        image_width=width,
        image_height=height,
        element_count=len(snap.elements),
    )
    return snap, cost


def measure_observation(driver, app: str | None = None, *, scope: Scope = Scope.WINDOW) -> ObsCost:
    """Cost of observing the current UI once, both ways, on ``driver``."""
    if app is None:
        app, _ = driver.frontmost_app()
    return _measure(driver, app, scope)[1]


def run_web_task(driver, url: str, *, rounds: int = 3) -> Report:
    """Navigate ``driver`` to ``url`` and measure observation cost ``rounds`` times.

    Beyond the per-observation cost, each RE-observation is also scored as a diff
    of the prior snapshot — the non-accumulation moat: a stable page re-observes
    for ~0 a11y tokens, while a screenshot loop pays its full image cost again.
    Requires a driver with `navigate` (the browser backend); the measurement is
    backend-agnostic.
    """
    from computeruse import observe

    driver.navigate(url)
    app, _ = driver.frontmost_app()
    report = Report()
    prev = None
    for _ in range(max(1, rounds)):
        snap, cost = _measure(driver, app, Scope.WINDOW)
        report.observations.append(cost)
        if prev is not None:
            diff_text = observe.render_diff(observe.diff_snapshots(prev, snap))
            report.reobserve_a11y_tokens.append(a11y_tokens(diff_text))
        prev = snap
    return report


def format_report(report: Report) -> str:
    """A compact, honest rendering: both raw costs, the ratio, and the method."""
    if not report.observations:
        return "cu-arena: no observations"
    o0 = report.observations[0]
    lines = [
        f"cu-arena  observations={len(report.observations)}  "
        f"elements={o0.element_count}  frame={o0.image_width}x{o0.image_height}px",
        f"  a11y (text)      {report.total_a11y_tokens:>8} tok  "
        f"({o0.a11y_tokens}/obs, {o0.a11y_chars} chars)",
        f"  screenshot (img) {report.total_screenshot_tokens:>8} tok  "
        f"({o0.screenshot_tokens}/obs, ~wxh/750)",
        f"  → a11y-first is {report.ratio:.1f}x cheaper per observation.",
    ]
    if report.reobserve_a11y_tokens:
        lines.append(
            f"  re-observe (a11y diff) {report.avg_reobserve_tokens:.0f} tok avg vs "
            f"{o0.screenshot_tokens} tok/screenshot — the non-accumulation moat "
            f"(a screenshot cannot diff)."
        )
    return "\n".join(lines)


__all__ = ["ObsCost", "Report", "a11y_tokens", "image_tokens",
           "measure_observation", "run_web_task", "format_report"]
