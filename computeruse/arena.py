"""cu-arena — the a11y-vs-vision observation-cost benchmark.

The moat claim is that an accessibility-first snapshot is far cheaper per
observation than the screenshot a vision agent must send, and — being text — it
*diffs* (re-observe after an action costs a tiny delta) where a screenshot pays
its full image-token cost every single step. This module turns that claim into a
reproducible **number**, measured on whatever `Driver` it is handed: for the live
UI it records the a11y snapshot's token cost and the token cost of the exact
screenshot the driver captures at the same moment.

Two entry points share one measurement:

* `run_web_task` navigates the browser backend to a URL and measures per round
  (``computeruse bench web``).
* `run_desktop_task` measures whatever app the OS driver (or any driver) shows,
  in every rendering view at once — ``full`` and ``interactive`` — plus the
  re-observe diff in each view (``computeruse bench desktop``), so the desktop
  number is produced by the same estimator and the same screenshot method as the
  web number and the two can be compared.

It is honest by construction — both raw numbers are reported, the methodology is
stated, and nothing is rigged: the a11y cost is the real rendered snapshot and
the image cost is the real captured frame's dimensions. A capture the backend
cannot deliver (no Screen Recording grant, no capture on this backend) is
reported as such, never silently zeroed into a better ratio.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from computeruse.schema import ComputerUseError, Scope

#: Chars per token for the a11y text cost — the same 4:1 basis cu-meter uses in
#: ``server.Runtime._run_gated`` (``tokens_est = (len+3)//4``), so arena and the
#: audit report speak the same units.
_CHARS_PER_TOKEN = 4

#: Claude vision cost: tokens ≈ (width × height) / 750 CSS px (per Anthropic's
#: published image-token estimate). This is the cost a screenshot-driven agent
#: pays for ONE observation frame.
_VISION_PX_PER_TOKEN = 750

#: Rendering views measured by `run_desktop_task` (mirrors observe.RENDER_MODES).
MODES = ("full", "interactive")


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
    #: rendering view the a11y text was produced in ("full" | "interactive")
    mode: str = "full"
    #: structured error code when the screenshot could not be captured (e.g.
    #: ``permission_denied_screen``); the image numbers are then 0 and the
    #: ratio is not meaningful.
    screenshot_error: str = ""

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
    mode: str = "full"

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

    @property
    def screenshot_error(self) -> str:
        return next((o.screenshot_error for o in self.observations if o.screenshot_error), "")

    def to_dict(self) -> dict:
        """JSON-ready form (``computeruse bench web --json``)."""
        return {
            "mode": self.mode,
            "observations": [asdict(o) for o in self.observations],
            "reobserve_a11y_tokens": list(self.reobserve_a11y_tokens),
            "total_a11y_tokens": self.total_a11y_tokens,
            "total_screenshot_tokens": self.total_screenshot_tokens,
            "ratio": round(self.ratio, 2),
            "avg_reobserve_tokens": round(self.avg_reobserve_tokens, 1),
            "screenshot_error": self.screenshot_error,
        }


def _png_size(png: bytes) -> tuple[int, int]:
    """(width, height) from a PNG header — no PIL, no decode."""
    if len(png) >= 24 and png[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
    return 0, 0


def _screenshot(driver) -> tuple[int, int, float, str]:
    """(width, height, backing scale, error) of one capture on ``driver``.

    A structured failure — no Screen Recording grant, a backend without capture
    — is returned as its error code instead of raised, so the a11y numbers still
    land and the report can say the image side was unavailable.
    """
    try:
        shot = driver.screenshot()
    except ComputerUseError as exc:
        return 0, 0, 1.0, exc.code.value
    except NotImplementedError:
        return 0, 0, 1.0, "unsupported"
    width, height = _png_size(getattr(shot, "png", b""))
    # Screenshots are captured in physical px; the vision formula is CSS px, so
    # divide out the backing scale to count the pixels the model actually bills.
    scale = getattr(getattr(shot, "display", None), "scale", 1.0) or 1.0
    return width, height, scale, ""


def _measure(driver, app: str, scope: Scope, mode: str = "full") -> tuple["object", ObsCost]:
    """(snapshot, ObsCost) for one look — the real a11y snapshot (rendered in
    ``mode``) and the real screenshot the driver captures for the SAME state, so
    the comparison is apples-to-apples and un-rigged. Returns the snapshot too,
    for diffing."""
    from computeruse import observe

    snap = driver.snapshot(scope, app)
    text = observe.render_text(snap, mode=mode)
    width, height, scale, error = _screenshot(driver)
    cost = ObsCost(
        a11y_tokens=a11y_tokens(text),
        screenshot_tokens=image_tokens(round(width / scale), round(height / scale)),
        a11y_chars=len(text),
        image_width=width,
        image_height=height,
        element_count=len(snap.elements),
        mode=mode,
        screenshot_error=error,
    )
    return snap, cost


def measure_observation(
    driver, app: str | None = None, *, scope: Scope = Scope.WINDOW, mode: str = "full"
) -> ObsCost:
    """Cost of observing the current UI once, both ways, on ``driver``."""
    if app is None:
        app, _ = driver.frontmost_app()
    return _measure(driver, app, scope, mode)[1]


def run_web_task(driver, url: str, *, rounds: int = 3, mode: str = "full") -> Report:
    """Navigate ``driver`` to ``url`` and measure observation cost ``rounds`` times.

    Beyond the per-observation cost, each RE-observation is also scored as a diff
    of the prior snapshot — the non-accumulation moat: a stable page re-observes
    for ~0 a11y tokens, while a screenshot loop pays its full image cost again.
    ``mode`` selects the rendering view the a11y text is costed in. Requires a
    driver with `navigate` (the browser backend); the measurement is
    backend-agnostic.
    """
    from computeruse import observe

    driver.navigate(url)
    app, _ = driver.frontmost_app()
    report = Report(mode=mode)
    prev = None
    for _ in range(max(1, rounds)):
        snap, cost = _measure(driver, app, Scope.WINDOW, mode)
        report.observations.append(cost)
        if prev is not None:
            diff_text = observe.render_diff(observe.diff_snapshots(prev, snap), mode=mode)
            report.reobserve_a11y_tokens.append(a11y_tokens(diff_text))
        prev = snap
    return report


def format_report(report: Report) -> str:
    """A compact, honest rendering: both raw costs, the ratio, and the method."""
    if not report.observations:
        return "cu-arena: no observations"
    o0 = report.observations[0]
    view = "" if report.mode == "full" else f", {report.mode}"
    lines = [
        f"cu-arena  observations={len(report.observations)}  "
        f"elements={o0.element_count}  frame={o0.image_width}x{o0.image_height}px",
        f"  a11y (text{view})  {report.total_a11y_tokens:>8} tok  "
        f"({o0.a11y_tokens}/obs, {o0.a11y_chars} chars)",
    ]
    if report.screenshot_error:
        lines.append(f"  screenshot (img)   unavailable ({report.screenshot_error}); "
                     "no ratio can be claimed")
    else:
        lines.append(
            f"  screenshot (img) {report.total_screenshot_tokens:>8} tok  "
            f"({o0.screenshot_tokens}/obs, ~wxh/750)"
        )
        lines.append(f"  → a11y-first is {report.ratio:.1f}x cheaper per observation.")
    if report.reobserve_a11y_tokens:
        versus = ("" if report.screenshot_error
                  else f" vs {o0.screenshot_tokens} tok/screenshot")
        lines.append(
            f"  re-observe (a11y diff) {report.avg_reobserve_tokens:.0f} tok avg{versus} "
            f"— the non-accumulation moat (a screenshot cannot diff)."
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Desktop: every rendering view of the same snapshot, versus one screenshot
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ModeCost:
    """Per-view a11y cost across the rounds of a `run_desktop_task`."""

    mode: str
    tokens: list[int] = field(default_factory=list)  #: per observation
    chars: list[int] = field(default_factory=list)
    #: token cost of re-observing as a diff of the prior snapshot, in this view
    reobserve_tokens: list[int] = field(default_factory=list)
    #: element lines the view renders (the interactive view keeps fewer)
    shown: int = 0

    @property
    def avg_tokens(self) -> float:
        return sum(self.tokens) / len(self.tokens) if self.tokens else 0.0

    @property
    def avg_chars(self) -> float:
        return sum(self.chars) / len(self.chars) if self.chars else 0.0

    @property
    def avg_reobserve_tokens(self) -> float:
        r = self.reobserve_tokens
        return sum(r) / len(r) if r else 0.0


@dataclass(slots=True)
class DesktopReport:
    """`run_desktop_task` result: one screenshot cost against every a11y view."""

    app: str
    element_count: int = 0
    screenshot_tokens: list[int] = field(default_factory=list)  #: per observation
    image_width: int = 0
    image_height: int = 0
    screenshot_error: str = ""
    modes: dict[str, ModeCost] = field(default_factory=dict)

    @property
    def rounds(self) -> int:
        return len(self.screenshot_tokens)

    @property
    def avg_screenshot_tokens(self) -> float:
        s = self.screenshot_tokens
        return sum(s) / len(s) if s else 0.0

    def ratio(self, mode: str) -> float:
        """How many times cheaper the ``mode`` view is than a screenshot (0 when
        no screenshot was available or the view rendered nothing)."""
        a = self.modes[mode].avg_tokens
        return self.avg_screenshot_tokens / a if a and not self.screenshot_error else 0.0

    def to_dict(self) -> dict:
        """JSON-ready form (``computeruse bench desktop --json``)."""
        return {
            "app": self.app,
            "rounds": self.rounds,
            "element_count": self.element_count,
            "screenshot": {
                "tokens_per_obs": round(self.avg_screenshot_tokens, 1),
                "image_width": self.image_width,
                "image_height": self.image_height,
                "error": self.screenshot_error,
            },
            "modes": {
                m: {
                    "tokens_per_obs": round(mc.avg_tokens, 1),
                    "chars_per_obs": round(mc.avg_chars, 1),
                    "element_lines": mc.shown,
                    "reobserve_diff_tokens": round(mc.avg_reobserve_tokens, 1),
                    "ratio_vs_screenshot": round(self.ratio(m), 2),
                }
                for m, mc in self.modes.items()
            },
        }


def run_desktop_task(
    driver,
    app: str | None = None,
    *,
    rounds: int = 3,
    scope: Scope = Scope.WINDOW,
    modes: tuple[str, ...] = MODES,
) -> DesktopReport:
    """Observe ``app`` (default: the driver's frontmost app) ``rounds`` times and
    cost each round's snapshot in every view of ``modes`` against the screenshot
    captured at the same moment.

    All views are rendered from the SAME snapshot per round, so the comparison
    between views is exact, and the same `_screenshot`/`image_tokens` method as
    `run_web_task` prices the image side, so desktop and web numbers are
    comparable. Re-observations are also costed as diffs of the prior snapshot,
    per view. Works on any `Driver` (the browser driver's "app" is a tab).
    """
    from computeruse import observe

    if app is None:
        app, _ = driver.frontmost_app()
    report = DesktopReport(app=str(app), modes={m: ModeCost(mode=m) for m in modes})
    prev = None
    for _ in range(max(1, rounds)):
        snap = driver.snapshot(scope, app)
        width, height, scale, error = _screenshot(driver)
        report.element_count = len(snap.elements)
        report.image_width, report.image_height = width, height
        report.screenshot_error = error
        report.screenshot_tokens.append(image_tokens(round(width / scale), round(height / scale)))
        for mode in modes:
            text = observe.render_text(snap, mode=mode)
            cost = report.modes[mode]
            cost.tokens.append(a11y_tokens(text))
            cost.chars.append(len(text))
            cost.shown = (len(observe.interactive_view(snap)) if mode == "interactive"
                          else len(snap.elements))
            if prev is not None:
                diff_text = observe.render_diff(observe.diff_snapshots(prev, snap), mode=mode)
                cost.reobserve_tokens.append(a11y_tokens(diff_text))
        prev = snap
    return report


def format_desktop_report(report: DesktopReport) -> str:
    """The desktop table: one screenshot line, one line per a11y view, then the
    re-observe diffs. Ratios appear only when a real screenshot was captured."""
    if not report.rounds:
        return "cu-arena desktop: no observations"
    lines = [
        f"cu-arena desktop  app={report.app}  observations={report.rounds}  "
        f"elements={report.element_count}  frame={report.image_width}x{report.image_height}px",
    ]
    if report.screenshot_error:
        lines.append(f"  screenshot (img)        unavailable ({report.screenshot_error}); "
                     "a11y costs below are real, no ratio is claimed")
    else:
        lines.append(f"  screenshot (img)       {report.avg_screenshot_tokens:>7.0f} tok/obs  (~wxh/750)")
    for mode, cost in report.modes.items():
        ratio = "" if report.screenshot_error else f"  → {report.ratio(mode):.1f}x cheaper than a screenshot"
        lines.append(
            f"  a11y {mode:<12}      {cost.avg_tokens:>7.0f} tok/obs  "
            f"({cost.avg_chars:.0f} chars, {cost.shown} element lines){ratio}"
        )
    for mode, cost in report.modes.items():
        if cost.reobserve_tokens:
            lines.append(
                f"  re-observe diff {mode:<11} {cost.avg_reobserve_tokens:>5.0f} tok avg"
            )
    if any(c.reobserve_tokens for c in report.modes.values()):
        lines.append("  (a re-observe is a diff of the prior snapshot; a screenshot cannot diff)")
    return "\n".join(lines)


__all__ = [
    "MODES", "ObsCost", "Report", "ModeCost", "DesktopReport",
    "a11y_tokens", "image_tokens", "measure_observation",
    "run_web_task", "run_desktop_task", "format_report", "format_desktop_report",
]
