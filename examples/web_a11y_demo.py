"""Before/after demo: force-enabling a Chromium/Electron app's accessibility tree.

Chrome, Slack, VS Code / Cursor, Discord, Spotify, Teams and most Electron apps
expose only an empty AXWebArea shell until a screen-reader-like client sets
`AXEnhancedUserInterface` / `AXManualAccessibility`. a11y-computer-use does this
automatically on the first snapshot — turning "0 interactive refs, fall back to
blind pixel-clicking" into a full, ref-addressable a11y tree.

This script snapshots the same running app twice — once with the force-enable
DISABLED (the old behaviour) and once with it ON — and prints the difference.

Run from a terminal that has Accessibility granted (System Settings > Privacy &
Security > Accessibility), with at least one Chromium/Electron app open:

    python examples/web_a11y_demo.py                 # auto-pick a running app
    python examples/web_a11y_demo.py com.google.Chrome
"""

from __future__ import annotations

import os
import sys

from a11y_computer_use import observe
from a11y_computer_use.schema import ComputerUseError, Scope

# Common Chromium/Electron apps (bundle id → display name), tried in order.
_CANDIDATES = [
    ("com.google.Chrome", "Google Chrome"),
    ("com.microsoft.VSCode", "VS Code"),
    ("com.todesktop.230313mzl4w4u92", "Cursor"),
    ("com.tinyspeck.slackmacgap", "Slack"),
    ("com.hnc.Discord", "Discord"),
    ("com.microsoft.edgemac", "Microsoft Edge"),
    ("com.brave.Browser", "Brave"),
    ("com.anthropic.claudefordesktop", "Claude"),
]


def _running_bundles() -> set[str]:
    from AppKit import NSWorkspace

    return {
        a.bundleIdentifier()
        for a in NSWorkspace.sharedWorkspace().runningApplications()
        if a.bundleIdentifier()
    }


def _pick_app(argv: list[str]) -> str:
    if len(argv) > 1:
        return argv[1]
    running = _running_bundles()
    for bundle, name in _CANDIDATES:
        if bundle in running:
            print(f"Auto-picked {name} ({bundle}). Pass a bundle id to override.\n")
            return bundle
    sys.exit("No known Chromium/Electron app is running. Open Chrome/Slack/Cursor, or pass a bundle id.")


def _count(app: str, *, force_enable: bool) -> tuple[int, int, list[str]]:
    """(total elements, interactive count, sample interactive lines) for a fresh
    snapshot with the web-a11y force-enable on or off."""
    if force_enable:
        os.environ.pop("A11Y_COMPUTER_USE_NO_WEB_A11Y", None)
    else:
        os.environ["A11Y_COMPUTER_USE_NO_WEB_A11Y"] = "1"
    observe._WEB_A11Y_ENABLED.clear()  # re-probe/enable this run (demo only)
    snap = observe.snapshot(Scope.APP, app=app)
    interactive = [el for el in snap.elements if el.clickable or el.editable]
    sample = [observe._render_line(el) for el in interactive[:8]]
    return len(snap.elements), len(interactive), sample


def main() -> None:
    app = _pick_app(sys.argv)
    try:
        before_total, before_i, _ = _count(app, force_enable=False)
        after_total, after_i, after_sample = _count(app, force_enable=True)
    except ComputerUseError as exc:
        sys.exit(f"\n{exc.code.value}: {exc.message}\n(Grant Accessibility to this terminal and retry.)")

    print("=" * 64)
    print(f"  {app}")
    print("=" * 64)
    print(f"  WITHOUT force-enable : {before_total:>4} elements, {before_i:>4} interactive")
    print(f"  WITH    force-enable : {after_total:>4} elements, {after_i:>4} interactive")
    gained = after_i - before_i
    print("-" * 64)
    if gained > 0:
        print(f"  ► +{gained} interactive elements became ref-addressable — the web/Electron")
        print("    content that was invisible to a11y is now fully drivable (no pixels).")
    elif before_i == after_i == 0:
        print("  This app exposes no a11y tree either way (truly custom-drawn) — the")
        print("  vision fallback (screenshot + coordinates) covers it.")
    else:
        print("  This app already exposed its tree natively; nothing to force-enable.")
    if after_sample:
        print("\n  Sample of now-addressable elements:")
        for line in after_sample:
            print(f"    {line}")


if __name__ == "__main__":
    main()
