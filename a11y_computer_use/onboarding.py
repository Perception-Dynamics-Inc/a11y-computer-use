"""Getting the two macOS grants without the user hunting through System Settings.

macOS never lets a process grant itself Accessibility or Screen Recording,
but it does let the process *ask*: `AXIsProcessTrustedWithOptions` with the
prompt option shows the system dialog ("<Host> would like to control this
computer using accessibility features") with an Open System Settings button,
and `CGRequestScreenCaptureAccess` does the same for Screen Recording. Both
name the host app for the user, which is the part nobody knows by heart.

`request` triggers the dialog, opens the exact settings pane as a fallback,
names the host app, and waits for the switch to flip so an agent can call it
once and get "granted" back. `first_hint` is what a permission error carries
the first time it happens in a process: it fires the dialog on the spot.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable

from a11y_computer_use import doctor

KINDS = ("accessibility", "screen_recording")

_PANES = {
    "accessibility": doctor.ACCESSIBILITY_SETTINGS_URL,
    "screen_recording": doctor.SCREEN_RECORDING_SETTINGS_URL,
}
_LABELS = {"accessibility": "Accessibility", "screen_recording": "Screen Recording"}

_prompted: set[str] = set()
_lock = threading.Lock()


def host_app() -> str | None:
    """The app bundle macOS attributes this process's grants to (Ghostty,
    Terminal, Claude, Codex, ...), or None when there is no bundle ancestor."""
    return doctor.responsible_app(doctor.parent_chain(os.getpid()))


def granted(kind: str) -> bool:
    if kind == "accessibility":
        return doctor._ax_trusted()
    if kind == "screen_recording":
        return doctor._screen_capture_preflight()
    raise ValueError(f"kind must be one of {KINDS}, not {kind!r}")


#: Set to 1 to keep the OS dialog and the settings pane closed (test suites,
#: unattended hosts); the hints still name the host app and the switch.
_QUIET = "A11Y_COMPUTER_USE_NO_OS_PROMPT"


def _quiet() -> bool:
    return os.environ.get(_QUIET, "0") not in ("", "0")


def _system_prompt(kind: str) -> bool:
    """Ask macOS to show its own grant dialog. False when unavailable."""
    if sys.platform != "darwin" or _quiet():
        return False
    try:
        if kind == "accessibility":
            from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt

            AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
            return True
        import Quartz

        Quartz.CGRequestScreenCaptureAccess()
        return True
    except Exception:  # noqa: BLE001 - pyobjc missing or the call refused
        return False


def open_pane(kind: str, opener: Callable[[list[str]], object] | None = None) -> bool:
    """Open the exact System Settings pane; the user only has to flip one switch."""
    if sys.platform != "darwin" or (_quiet() and opener is None):
        return False
    run = opener or (lambda cmd: subprocess.run(cmd, capture_output=True, timeout=10))
    try:
        run(["open", _PANES[kind]])
        return True
    except Exception:  # noqa: BLE001
        return False


def steps(kind: str, host: str | None) -> list[str]:
    who = f"'{host}'" if host else "the app that launched a11y-computer-use (the terminal or the agent app)"
    out = [
        f"A macOS dialog and the {_LABELS[kind]} pane of System Settings were opened.",
        f"Switch on {who} in that list (click Open System Settings in the dialog if it is showing).",
    ]
    if kind == "screen_recording":
        out.append("Screen Recording only takes effect after the host app is quit and reopened.")
    else:
        out.append("Accessibility takes effect immediately; no restart needed.")
    return out


def request(kind: str, *, wait_s: float = 90.0, poll_s: float = 1.0,
            opener: Callable[[list[str]], object] | None = None,
            is_granted: Callable[[str], bool] | None = None,
            prompt: Callable[[str], bool] | None = None,
            clock: Callable[[], float] = time.monotonic,
            sleep: Callable[[float], None] = time.sleep) -> dict:
    """Fire the OS dialog, open the pane, wait for the grant.

    Returns a dict: needed (False off macOS), granted, host_app, kind,
    dialog_shown, pane_opened, waited_s, steps (for the user), settings_url.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, not {kind!r}")
    check = is_granted or granted
    if sys.platform != "darwin":
        return {"needed": False, "granted": True, "kind": kind, "host_app": None,
                "steps": [f"{_LABELS[kind]} grants are a macOS concept; nothing to do here."]}
    host = host_app()
    if check(kind):
        return {"needed": True, "granted": True, "kind": kind, "host_app": host,
                "dialog_shown": False, "pane_opened": False, "waited_s": 0.0,
                "steps": [f"{_LABELS[kind]} is already granted to {host or 'this process'}."],
                "settings_url": _PANES[kind]}
    shown = (prompt or _system_prompt)(kind)
    opened = open_pane(kind, opener)
    with _lock:
        _prompted.add(kind)
    start = clock()
    ok = False
    while clock() - start < wait_s:
        if check(kind):
            ok = True
            break
        sleep(poll_s)
    waited = clock() - start
    out = {"needed": True, "granted": ok, "kind": kind, "host_app": host, "dialog_shown": shown,
           "pane_opened": opened, "waited_s": round(waited, 1), "settings_url": _PANES[kind],
           "steps": steps(kind, host)}
    if ok:
        out["steps"] = [f"{_LABELS[kind]} granted to {host or 'this process'} after {waited:.0f} s."]
        if kind == "screen_recording":
            out["steps"].append("Quit and reopen the host app before the next screenshot.")
    return out


def first_hint(kind: str, opener: Callable[[list[str]], object] | None = None,
               prompt: Callable[[str], bool] | None = None) -> str:
    """The hint a permission error carries. The first time per process it also
    fires the OS dialog and opens the pane, so the user sees what to click
    the moment the agent hits the wall."""
    host = host_app()
    who = f"'{host}'" if host else "the app that launched this server"
    with _lock:
        first = kind not in _prompted
        _prompted.add(kind)
    shown = opened = False
    if first and sys.platform == "darwin":
        shown = (prompt or _system_prompt)(kind)
        opened = open_pane(kind, opener)
    if shown or opened:
        lead = f"macOS is showing its {_LABELS[kind]} dialog and the settings pane now."
    else:
        lead = f"{_LABELS[kind]} is not granted to {who}."
    return (f"{lead} Tell the user: switch on {who} under System Settings > Privacy & Security > "
            f"{_LABELS[kind]}. Then call request_permission(kind='{kind}'), which reopens the pane "
            f"if needed and waits up to 90 s for the switch, and retry.")


SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".a11y-computer-use", "settings.json")


def settings() -> dict:
    """User choices remembered from dialogs (`report_issue_always`, ...)."""
    try:
        import json

        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - missing or unreadable: no choices remembered
        return {}


def remember(key: str, value: object) -> None:
    import json

    data = settings()
    data[key] = value
    os.makedirs(os.path.dirname(SETTINGS_PATH), mode=0o700, exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, SETTINGS_PATH)


def native_confirm(title: str, message: str, *, ok: str = "Allow", cancel: str = "Don't Allow",
                   timeout_s: float = 120.0, details: str | None = None,
                   remember_label: str | None = None,
                   runner: Callable[..., "subprocess.CompletedProcess[str]"] | None = None) -> bool | None:
    """A macOS dialog shown by this process, for hosts that cannot show one.

    A native `NSAlert` from a short-lived helper (`a11y_computer_use._alert`):
    the project icon, ``title`` as the headline, ``message`` as one short
    paragraph, ``details`` in a scrollable box, ``ok`` and ``cancel`` buttons,
    and, with ``remember_label``, a "don't ask again" checkbox whose answer
    `last_remember` reports. AppleScript's `display dialog` is the fallback
    when the helper cannot run.

    Returns True for ``ok``, False for ``cancel`` or the timeout, None when no
    dialog could be shown (off macOS, no window server, no helper). The
    dialog is a separate process this tool's tiers never grant, so the agent
    cannot click it through this server.
    """
    global last_remember
    last_remember = False
    if sys.platform != "darwin" or _quiet():
        return None
    run = runner or (lambda cmd, **kw: subprocess.run(cmd, capture_output=True, text=True, **kw))
    answer = _alert_helper(title, message, ok=ok, cancel=cancel, timeout_s=timeout_s, details=details,
                           remember_label=remember_label, run=run)
    if answer is not None:
        return answer
    if details:
        message = f"{message}\n\n{details[:900]}" + ("\n..." if len(details) > 900 else "")
    esc = lambda t: t.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
    script = (
        'tell application "System Events"\n'
        "activate\n"
        f'display dialog "{esc(message)}" with title "{esc(title)}" '
        f'buttons {{"{esc(cancel)}", "{esc(ok)}"}} default button "{esc(cancel)}" '
        f'cancel button "{esc(cancel)}" with icon caution giving up after {int(timeout_s)}\n'
        "end tell"
    )
    try:
        done = run(["osascript", "-e", script], timeout=timeout_s + 15)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (done.stdout or "") + (done.stderr or "")
    if "gave up:true" in out:
        return False
    if f"button returned:{ok}" in out:
        return True
    if done.returncode != 0 and "User canceled" not in out and "-128" not in out:
        return None  # osascript itself failed: no display, no binary
    return False


#: Whether the user ticked the "don't ask again" box in the last native dialog.
last_remember = False


def _alert_helper(title: str, message: str, *, ok: str, cancel: str, timeout_s: float,
                  details: str | None, remember_label: str | None, run) -> bool | None:
    """Run the NSAlert helper; None when it could not show a dialog."""
    global last_remember
    import json
    import tempfile

    cmd = [sys.executable, "-m", "a11y_computer_use._alert", "--title", title, "--message", message,
           "--allow", ok, "--deny", cancel, "--timeout", str(int(timeout_s))]
    tmp = None
    if details:
        fd, tmp = tempfile.mkstemp(prefix="a11y-alert-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(details)
        cmd += ["--details", tmp]
    if remember_label:
        cmd += ["--remember", remember_label]
    try:
        done = run(cmd, timeout=timeout_s + 20)
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    if done.returncode != 0 or not (done.stdout or "").strip():
        return None
    try:
        out = json.loads((done.stdout or "").strip().splitlines()[-1])
    except ValueError:
        return None
    last_remember = bool(out.get("remember"))
    button = out.get("button")
    if button == "allow":
        return True
    if button in ("deny", "timeout"):
        return False
    return None
