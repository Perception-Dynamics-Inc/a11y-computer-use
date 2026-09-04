"""Shared pytest scaffolding.

Exposes:
    HAS_AX / HAS_SCREEN — whether this process holds the Accessibility /
        Screen Recording TCC grants. Any test touching a real AX tree, posting
        CGEvents, or capturing the screen MUST be guarded with
        ``@pytest.mark.skipif(not HAS_AX, ...)`` (resp. ``HAS_SCREEN``) — on
        an ungranted machine those calls fail with permission errors, and the
        graceful permission-error path has its own dedicated tests.
    HAS_DISPLAYS — whether a window-server session with at least one active
        display is reachable. False on locked screens and headless/agent
        contexts, where ``CGGetActiveDisplayList`` returns an empty list even
        though it needs no TCC grant; live display-enumeration tests must be
        guarded with it.
    synthetic_snapshot / snapshot_builder — an in-memory a11y tree matching
        `computeruse.schema` exactly, for tests that need no real permissions.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
from pathlib import Path

import pytest

from computeruse.schema import Bounds, Display, Element, Scope, Snapshot


def _detect_permissions() -> tuple[bool, bool]:
    """Probe TCC grants via ctypes (no pyobjc import side effects).

    Returns:
        (has_accessibility, has_screen_recording); (False, False) off macOS.
    """
    if sys.platform != "darwin":
        return False, False
    has_ax = False
    has_screen = False
    try:
        path = ctypes.util.find_library("ApplicationServices")
        if path:
            app_services = ctypes.cdll.LoadLibrary(path)
            app_services.AXIsProcessTrusted.restype = ctypes.c_bool
            has_ax = bool(app_services.AXIsProcessTrusted())
    except (OSError, AttributeError):
        pass
    try:
        path = ctypes.util.find_library("CoreGraphics")
        if path:
            core_graphics = ctypes.cdll.LoadLibrary(path)
            core_graphics.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
            has_screen = bool(core_graphics.CGPreflightScreenCaptureAccess())
    except (OSError, AttributeError):
        pass
    return has_ax, has_screen


def _detect_active_displays() -> bool:
    """Whether the window server reports any active display (ctypes probe)."""
    if sys.platform != "darwin":
        return False
    try:
        path = ctypes.util.find_library("CoreGraphics")
        if not path:
            return False
        core_graphics = ctypes.cdll.LoadLibrary(path)
        count = ctypes.c_uint32(0)
        err = core_graphics.CGGetActiveDisplayList(0, None, ctypes.byref(count))
        return err == 0 and count.value > 0
    except (OSError, AttributeError):
        return False


HAS_AX, HAS_SCREEN = _detect_permissions()
HAS_DISPLAYS = _detect_active_displays()

_DISPLAY = Display(display_id=1, width=2880, height=1800, scale=2.0, is_main=True)


def build_synthetic_snapshot(
    snapshot_id: str = "snap-test-1",
    app: str = "com.apple.TextEdit",
) -> Snapshot:
    """Build a small, realistic pruned tree: a window holding a save button,
    an editable text area, a secure password field, and a disabled button.
    Covers every actionable-flag combination tests usually need."""
    window = Element(
        ref="e1",
        role="AXWindow",
        title="Untitled",
        value=None,
        bounds=Bounds(1, 200, 100, 1600, 1200),
        snapshot_id=snapshot_id,
        parent=None,
        path=("AXWindow",),
    )
    button = Element(
        ref="e2",
        role="AXButton",
        title="Save",
        value=None,
        bounds=Bounds(1, 240, 140, 120, 56),
        snapshot_id=snapshot_id,
        parent="e1",
        path=("AXWindow", "AXButton"),
        clickable=True,
    )
    text_area = Element(
        ref="e3",
        role="AXTextArea",
        title="Document body",
        value="hello",
        bounds=Bounds(1, 240, 220, 1520, 900),
        snapshot_id=snapshot_id,
        parent="e1",
        path=("AXWindow", "AXTextArea"),
        clickable=True,
        editable=True,
        focused=True,
    )
    secure_field = Element(
        ref="e4",
        role="AXTextField",
        title="Password",
        value=None,
        bounds=Bounds(1, 240, 1140, 400, 56),
        snapshot_id=snapshot_id,
        parent="e1",
        path=("AXWindow", "AXTextField"),
        clickable=True,
        editable=True,
        secure=True,
    )
    disabled_button = Element(
        ref="e5",
        role="AXButton",
        title="Publish",
        value=None,
        bounds=Bounds(1, 1700, 1140, 120, 56),
        snapshot_id=snapshot_id,
        parent="e1",
        path=("AXWindow", "AXButton"),
        clickable=True,
        enabled=False,
    )
    return Snapshot(
        snapshot_id=snapshot_id,
        scope=Scope.WINDOW,
        app=app,
        pid=4242,
        created_at=1_752_300_000.0,
        displays=(_DISPLAY,),
        elements=(window, button, text_area, secure_field, disabled_button),
    )


@pytest.fixture
def synthetic_snapshot() -> Snapshot:
    """A ready-made synthetic `Snapshot` (see `build_synthetic_snapshot`)."""
    return build_synthetic_snapshot()


@pytest.fixture
def snapshot_builder():
    """The builder itself, for tests needing custom ids/apps."""
    return build_synthetic_snapshot


if sys.platform == "win32":

    @pytest.fixture(autouse=True)
    def _home_follows_HOME(monkeypatch):
        """Make ``Path.home()`` honour ``HOME`` on Windows for the test session.

        The package resolves ``~/.computeruse`` through ``Path.home()``, which
        on Windows reads ``USERPROFILE`` and ignores ``HOME``. Tests isolate
        state by pointing ``HOME`` at ``tmp_path``; without this shim they would
        read and write the runner's real ``~/.computeruse`` and leak grants and
        audit rows between tests.
        """
        real_home = Path.home

        def _home(cls=Path):
            env = os.environ.get("HOME")
            return Path(env) if env else real_home()

        monkeypatch.setattr(Path, "home", classmethod(lambda cls: _home(cls)))
