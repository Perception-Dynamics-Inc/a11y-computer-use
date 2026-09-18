"""macOS backend: AXUIElement (observe), CGEvent (act), Quartz (capture).

Thin delegators to the existing platform modules — the real implementation
already lives in `observe` / `act` / `capture`, and this just presents it
behind the `Driver` protocol so the Runtime is OS-agnostic. Methods call
``module.function(...)`` at call time (not a bound import) so a test that
monkeypatches, e.g., ``observe.snapshot`` still intercepts.

System/windowing helpers live in `server` today and are delegated to lazily
(they import pyobjc); migrating them into this module is the next step, but the
protocol is already complete so the Windows port has the full contract.
"""

from __future__ import annotations

from collections.abc import Callable

from a11y_computer_use import act, capture, observe
from a11y_computer_use.schema import (
    Bounds,
    Element,
    MouseButton,
    Point,
    Scope,
    ScrollUnit,
    Snapshot,
    Target,
    WaitCondition,
)


class MacOSDriver:
    """The `Driver` protocol, backed by macOS AX / CGEvent / Quartz."""

    name = "macos"

    # -- permissions --------------------------------------------------------
    def ensure_trusted(self) -> None:
        observe.ensure_trusted()

    # -- observe ------------------------------------------------------------
    def snapshot(self, scope: Scope, app: str) -> Snapshot:
        return observe.snapshot(scope, app=app)

    def resolve_ref(self, snap: Snapshot, ref: str, *, live: Snapshot | None = None) -> Element:
        return observe.resolve_ref(snap, ref, live=live)

    def press_element(self, element: Element) -> bool:
        return observe.press_element(element)

    def scroll_into_view(self, element: Element) -> bool:
        return observe.scroll_into_view(element)

    def set_value(self, element: Element, value: str) -> bool:
        return observe.set_value(element, value)

    # -- act ----------------------------------------------------------------
    def click(self, target: Target, *, button: MouseButton = MouseButton.LEFT, count: int = 1,
              modifiers: tuple[str, ...] = (), pre_check: Callable | None = None,
              dry_run: bool = False) -> object:
        return act.click(target, button=button, count=count, modifiers=modifiers,
                         pre_check=pre_check, dry_run=dry_run)

    def drag(self, start: Target, end: Target, *, button: MouseButton = MouseButton.LEFT,
             pre_check: Callable | None = None, dry_run: bool = False) -> object:
        return act.drag(start, end, button=button, pre_check=pre_check, dry_run=dry_run)

    def scroll(self, target: Target, *, dx: int = 0, dy: int = 0,
               unit: ScrollUnit = ScrollUnit.LINES, pre_check: Callable | None = None,
               dry_run: bool = False) -> object:
        return act.scroll(target, dx=dx, dy=dy, unit=unit, pre_check=pre_check, dry_run=dry_run)

    def type_text(self, text: str, *, pre_check: Callable | None = None,
                  dry_run: bool = False) -> object:
        return act.type_text(text, pre_check=pre_check, dry_run=dry_run)

    def key_chord(self, chord: str, *, pre_check: Callable | None = None,
                  dry_run: bool = False) -> object:
        return act.key_chord(chord, pre_check=pre_check, dry_run=dry_run)

    def wait_for(self, target: Element, *, condition: WaitCondition, timeout_s: float,
                 checker: Callable | None = None) -> Element:
        return act.wait_for(target, condition=condition, timeout_s=timeout_s, checker=checker)

    # -- capture ------------------------------------------------------------
    def screenshot(self, display_id: int | None = None) -> object:
        return capture.screenshot(display_id)

    def zoom_region(self, region: Bounds) -> bytes:
        return capture.zoom_region(region)

    def main_display_id(self) -> int:
        import Quartz  # pyobjc, macOS only; loaded at call time like the rest

        return int(Quartz.CGMainDisplayID())

    # -- system / windowing (delegated to server helpers for now) ----------
    def frontmost_app(self) -> tuple[str | None, int | None]:
        from a11y_computer_use import safety
        return safety.frontmost_app()

    def app_at_point(self, point: Point) -> str | None:
        from a11y_computer_use import server
        return server._app_at_point(point)

    def running_apps(self) -> list[dict]:
        from a11y_computer_use import server
        return server._list_apps()

    def launch_app(self, identifier: str) -> None:
        from a11y_computer_use import server
        server._launch_app(identifier)

    def activate_app(self, identifier: str) -> str:
        from a11y_computer_use import server
        running, bundle = server._running_app(identifier)
        server._activate(running)
        return bundle

    def windows(self) -> list[dict]:
        from a11y_computer_use import server
        return server._window_rows()

    def window_owner(self, window_id: int) -> str:
        from a11y_computer_use import server
        _running, bundle = server._window_running(window_id)
        return bundle

    def raise_window(self, window_id: int) -> None:
        # MVP: raising activates the owning app (per-window AXRaise needs the
        # private CGWindowID<->AXUIElement bridge).
        from a11y_computer_use import server
        running, _bundle = server._window_running(window_id)
        server._activate(running)

    # -- menus and panels (accessible even in custom-drawn apps) -------------
    def menu_items(self, app: str, path: str | None) -> list[dict]:
        from a11y_computer_use import menus
        return menus.macos_menu_items(app, path)

    def menu_press(self, app: str, path: str) -> str:
        from a11y_computer_use import menus
        return menus.macos_menu_press(app, path)

    def file_dialog(self, verb: object, path: str, app: str) -> dict:
        from a11y_computer_use import menus
        from a11y_computer_use.schema import FileDialogVerb
        return menus.macos_file_dialog(FileDialogVerb(verb), path, app)

    def read_clipboard(self) -> str | None:
        from a11y_computer_use import server
        return server._read_clipboard()

    def write_clipboard(self, text: str) -> None:
        from a11y_computer_use import server
        server._write_clipboard(text)
