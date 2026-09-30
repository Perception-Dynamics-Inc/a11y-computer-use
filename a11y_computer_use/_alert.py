"""A native macOS confirmation, shown by a short-lived helper process.

`python -m a11y_computer_use._alert --title ... --message ... [--details FILE]
[--allow Allow] [--deny "Don't Allow"] [--remember "Don't ask again ..."]
[--timeout 120]` shows one `NSAlert` with the project icon, a short message,
the details in a scrollable box, two buttons, and an optional checkbox, and
prints one JSON line: ``{"button": "allow"|"deny"|"timeout", "remember": bool}``.

It runs in its own process because the MCP server has no AppKit run loop and
must not become an app. The helper activates itself only for the dialog and
exits, so the user's previous app gets focus back. `onboarding.native_confirm`
is the caller.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading


def _icon_path() -> str | None:
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in ("assets/alert-icon.png", "../docs/assets/logo.png"):
        path = os.path.normpath(os.path.join(here, rel))
        if os.path.exists(path):
            return path
    return None


def show(title: str, message: str, *, details: str | None = None, allow: str = "Allow",
         deny: str = "Don't Allow", remember: str | None = None, timeout_s: float = 120.0) -> dict:
    from AppKit import (
        NSAlert, NSAlertFirstButtonReturn, NSApplication, NSApplicationActivationPolicyAccessory,
        NSFont, NSImage, NSMakeRect, NSScrollView, NSTextView,
    )

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(message)
    icon = _icon_path()
    if icon:
        image = NSImage.alloc().initWithContentsOfFile_(icon)
        if image is not None:
            alert.setIcon_(image)
    alert.addButtonWithTitle_(allow)  # first button = default, returns NSAlertFirstButtonReturn
    alert.addButtonWithTitle_(deny)
    if details:
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, 460, 180))
        text = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 460, 180))
        text.setEditable_(False)
        text.setFont_(NSFont.userFixedPitchFontOfSize_(11))
        text.setString_(details)
        scroll.setDocumentView_(text)
        scroll.setHasVerticalScroller_(True)
        alert.setAccessoryView_(scroll)
    if remember:
        alert.setShowsSuppressionButton_(True)
        alert.suppressionButton().setTitle_(remember)

    outcome = {"button": "timeout", "remember": False}

    def give_up() -> None:
        try:
            app.abortModal()
        except Exception:  # noqa: BLE001
            pass

    timer = threading.Timer(timeout_s, give_up)
    timer.daemon = True
    timer.start()
    app.activateIgnoringOtherApps_(True)
    code = alert.runModal()
    timer.cancel()
    if code == NSAlertFirstButtonReturn:
        outcome["button"] = "allow"
    elif code == NSAlertFirstButtonReturn + 1:
        outcome["button"] = "deny"
    if remember and alert.suppressionButton().state():
        outcome["remember"] = True
    return outcome


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    ap.add_argument("--message", required=True)
    ap.add_argument("--details", help="file whose text goes in the scrollable box")
    ap.add_argument("--allow", default="Allow")
    ap.add_argument("--deny", default="Don't Allow")
    ap.add_argument("--remember")
    ap.add_argument("--timeout", type=float, default=120.0)
    a = ap.parse_args(argv)
    details = open(a.details, encoding="utf-8").read() if a.details else None
    out = show(a.title, a.message, details=details, allow=a.allow, deny=a.deny,
               remember=a.remember, timeout_s=a.timeout)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
