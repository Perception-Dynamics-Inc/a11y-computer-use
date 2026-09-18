"""The platform driver seam — the boundary that makes a11y-computer-use reusable
across operating systems.

Everything above this line is platform-free: the canonical `schema` (the
contract), the tree-pruning core (`observe.build_snapshot`), the safety layer,
and the MCP server surface. Everything OS-specific — walking the accessibility
tree, synthesizing input, capturing pixels, enumerating windows — lives behind
the `Driver` protocol below.

- macOS is implemented in `drivers/macos.py` (AXUIElement / CGEvent / Quartz).
- Windows is the port target in `drivers/windows.py` (UI Automation / SendInput
  / DXGI); see `docs/windows-port.md` for the primitive-by-primitive mapping.

`get_driver()` (in `drivers/__init__.py`) returns the backend for the current
OS. The Runtime holds one and routes every platform operation through it, so
adding an OS is "implement the protocol", never "touch the core".
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol, runtime_checkable

from a11y_computer_use.schema import (
    Bounds,
    Element,
    MouseButton,
    Scope,
    ScrollUnit,
    Snapshot,
    Target,
    WaitCondition,
)


@runtime_checkable
class Driver(Protocol):
    """One OS backend. Every method speaks the canonical `schema` types, so the
    Runtime never sees a platform detail. Implementations must be import-safe on
    every OS (do heavy, OS-only imports lazily inside the methods) so that
    `drivers` can be imported anywhere and `get_driver()` can pick correctly."""

    #: Stable backend id, e.g. "macos" | "windows".
    name: str

    # -- permissions --------------------------------------------------------
    def ensure_trusted(self) -> None:
        """Raise `ErrorCode.PERMISSION_DENIED_*` if the OS accessibility grant
        (macOS TCC / a Windows equivalent) is missing for this process."""
        ...

    # -- observe ------------------------------------------------------------
    def snapshot(self, scope: Scope, app: str) -> Snapshot:
        """A pruned, indexed accessibility snapshot of ``app`` at ``scope``."""
        ...

    def resolve_ref(self, snap: Snapshot, ref: str, *, live: Snapshot | None = None) -> Element:
        """Re-resolve a snapshot-scoped ref against the live tree."""
        ...

    def press_element(self, element: Element) -> bool:
        """Activate ``element`` via the accessibility API without moving the
        pointer (True on success; False → caller falls back to a synthetic click)."""
        ...

    def scroll_into_view(self, element: Element) -> bool:
        """Reveal ``element`` via the accessibility API without moving the pointer."""
        ...

    def set_value(self, element: Element, value: str) -> bool:
        """Set ``element``'s text/value directly through the accessibility API —
        one deterministic op, no keystroke simulation and no focus dance (macOS
        `AXValue` set / UIA `ValuePattern.SetValue` / AT-SPI `EditableText`).
        True on success; False → caller falls back to focus + type. Secure fields
        are refused (return False)."""
        ...

    # -- act (synthesized input) -------------------------------------------
    def click(
        self,
        target: Target,
        *,
        button: MouseButton = MouseButton.LEFT,
        count: int = 1,
        modifiers: tuple[str, ...] = (),
        pre_check: Callable | None = None,
        dry_run: bool = False,
    ) -> object: ...

    def drag(self, start: Target, end: Target, *, button: MouseButton = MouseButton.LEFT,
             path: Sequence[Target] = (), pre_check: Callable | None = None,
             dry_run: bool = False) -> object: ...

    def scroll(self, target: Target, *, dx: int = 0, dy: int = 0,
               unit: ScrollUnit = ScrollUnit.LINES, pre_check: Callable | None = None,
               dry_run: bool = False) -> object: ...

    def type_text(self, text: str, *, pre_check: Callable | None = None,
                  dry_run: bool = False) -> object: ...

    def key_chord(self, chord: str, *, pre_check: Callable | None = None,
                  dry_run: bool = False) -> object: ...

    def wait_for(self, target: Element, *, condition: WaitCondition,
                 timeout_s: float, checker: Callable | None = None) -> Element: ...

    # -- capture ------------------------------------------------------------
    def screenshot(self, display_id: int | None = None) -> object:
        """A full-display capture (a `capture.Screenshot`-shaped object)."""
        ...

    def main_display_id(self) -> int:
        """The display id a raw x/y target defaults to when the caller gives
        none. It must be the id this backend stamps on its snapshot geometry
        and screenshots (0 on single-display backends), so an un-qualified
        point lands on the same display the model just observed."""
        ...

    def zoom_region(self, region: Bounds) -> bytes:
        """A native-resolution PNG crop of ``region``."""
        ...

    # -- system / windowing -------------------------------------------------
    def frontmost_app(self) -> tuple[str | None, int | None]:
        """(bundle-or-app-id, pid) of the foreground app, or (None, None)."""
        ...

    def app_at_point(self, point: Point) -> str | None:  # noqa: F821 (Point via Target)
        """The app id under a screen point (the act-time hit-test), or None."""
        ...

    def running_apps(self) -> list[dict]: ...

    def launch_app(self, identifier: str) -> None: ...

    def activate_app(self, identifier: str) -> str: ...

    def windows(self) -> list[dict]: ...

    def window_owner(self, window_id: int) -> str:
        """The app id (the permission grant key) owning ``window_id``.

        Raises `ErrorCode.APP_NOT_FOUND` when no such window exists, and
        `ErrorCode.UNSUPPORTED` where windows are not addressable by an integer
        id (the browser's windows are tabs: use ``app focus <target id>``)."""
        ...

    def raise_window(self, window_id: int) -> None:
        """Bring ``window_id`` frontmost. Same error contract as `window_owner`;
        never returns normally for a no-op (the Runtime reports success)."""
        ...

    def menu_items(self, app: str, path: str | None) -> list[dict]:
        """Items of the menu at ``path`` in ``app``'s menu bar (top-level menus
        when ``path`` is empty): dicts with title, enabled, shortcut, submenu,
        checked. Backends without an accessible menu bar raise a structured
        ``unsupported``."""
        ...

    def menu_press(self, app: str, path: str) -> str:
        """Activate the menu item at ``path``; returns its title."""
        ...

    def menu_state(self, app: str) -> dict:
        """``{"open": bool, "path": [titles]}`` for the menu currently showing
        in ``app``'s menu bar. Backends without menu bars return
        ``{"open": False, "path": []}`` rather than raising: the Runtime asks
        before every keystroke."""
        ...

    def menu_close(self, app: str) -> list[str]:
        """Close the open menu; returns the path that was open (empty if none)."""
        ...

    def file_dialog(self, verb: object, path: str, app: str) -> dict:
        """Drive the frontmost open/save panel of ``app`` to ``path``."""
        ...

    def read_clipboard(self) -> str | None: ...

    def write_clipboard(self, text: str) -> None: ...


__all__ = ["Driver"]
