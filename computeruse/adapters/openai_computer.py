"""OpenAI computer-use executor: Responses API ``computer`` tool actions (GA) and
the deprecated ``computer_use_preview`` shape, executed through the gated Runtime.

Wire shapes (platform.openai.com/docs/guides/computer-use):

* GA: the request declares ``{"type": "computer"}``. The model returns a
  ``computer_call`` item with ``call_id`` and an ``actions`` array; the host
  answers with one ``computer_call_output`` whose ``output`` is
  ``{"type": "computer_screenshot", "image_url": "data:image/png;base64,..."}``.
* Preview: ``{"type": "computer_use_preview", "display_width", "display_height",
  "environment"}``; the ``computer_call`` carries a single ``action`` and the
  output image is ``{"type": "input_image", "image_url": ...}``.

Actions: ``click {x, y, button, keys?}``, ``double_click {x, y}``, ``drag {path}``,
``keypress {keys}``, ``move {x, y}``, ``screenshot``, ``scroll {x, y, scroll_x,
scroll_y}``, ``type {text}``, ``wait``. ``pending_safety_checks`` on a call are
echoed as ``acknowledged_safety_checks`` only when the host opts in.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence

from computeruse.adapters.base import (
    _MODIFIER_ALIASES,
    ComputerAdapter,
    Result,
    openai_keys_to_chords,
)

__all__ = ["ACTIONS", "GA_TYPE", "PREVIEW_TYPE", "OpenAIComputerAdapter"]

GA_TYPE = "computer"
PREVIEW_TYPE = "computer_use_preview"

ACTIONS: frozenset[str] = frozenset({
    "click", "double_click", "drag", "keypress", "move", "screenshot", "scroll", "type", "wait",
})

#: Driver name -> the tool definition's ``environment`` value.
_ENVIRONMENT: dict[str, str] = {
    "macos": "mac", "windows": "windows", "linux": "ubuntu", "browser": "browser",
}

_BUTTONS: dict[str, str] = {"left": "left", "right": "right", "wheel": "middle"}

#: Browser history buttons have no pointer equivalent; they become the platform
#: back/forward shortcut.
_HISTORY_CHORD: dict[str, dict[str, str]] = {
    "back": {"macos": "cmd+leftbracket", "default": "alt+left"},
    "forward": {"macos": "cmd+rightbracket", "default": "alt+right"},
}


def _xy(action: Mapping[str, object], kx: str = "x", ky: str = "y") -> tuple[int, int]:
    if action.get(kx) is None or action.get(ky) is None:
        raise ValueError(f"{action.get('type')} needs {kx} and {ky}")
    return int(action[kx]), int(action[ky])  # type: ignore[arg-type]


def _path_point(point: object) -> tuple[int, int]:
    if isinstance(point, Mapping):
        return int(point["x"]), int(point["y"])
    if isinstance(point, (list, tuple)) and len(point) == 2:
        return int(point[0]), int(point[1])
    raise ValueError(f"drag path points must be {{x, y}} or [x, y], got {point!r}")


class OpenAIComputerAdapter(ComputerAdapter):
    """Execute OpenAI computer-use actions through computerUse.

    Args (beyond `ComputerAdapter`):
        environment: Override for the preview definition's ``environment``
            (``browser`` | ``mac`` | ``windows`` | ``ubuntu``); derived from the
            driver name when None.
        default_wait_s: Seconds a ``wait`` action pauses (the action carries no
            duration).
    """

    def __init__(self, runtime, *, environment: str | None = None, default_wait_s: float = 1.0,
                 **kwargs) -> None:
        super().__init__(runtime, **kwargs)
        self._environment = environment
        self.default_wait_s = default_wait_s

    # -- tool definition -------------------------------------------------------

    def environment(self) -> str:
        return self._environment or _ENVIRONMENT.get(self.driver_name, "ubuntu")

    def tool_definition(self, *, preview: bool = False) -> dict:
        """The ``tools`` entry. GA is just ``{"type": "computer"}``; the preview
        shape declares the screenshot size (probing one when needed) and the
        environment."""
        if not preview:
            return {"type": GA_TYPE}
        if self._screen is None:
            shot = self.screenshot()
            if not shot.ok or self._screen is None:
                raise RuntimeError(f"cannot determine display size: {shot.text}")
        return {
            "type": PREVIEW_TYPE,
            "display_width": self._screen.width,
            "display_height": self._screen.height,
            "environment": self.environment(),
        }

    # -- dispatch ----------------------------------------------------------------

    def handle(self, action: Mapping[str, object]) -> Result:
        """Execute one action object (``{"type": "click", ...}``)."""
        kind = action.get("type")
        if not isinstance(kind, str) or not kind:
            return Result("?", "missing action type", error="invalid")
        handler = getattr(self, f"_do_{kind}", None)
        if kind not in ACTIONS or handler is None:
            return Result(kind, f"unknown computer action {kind!r}; supported: "
                          + ", ".join(sorted(ACTIONS)), error="invalid")
        try:
            return handler(action)
        except (ValueError, KeyError, TypeError) as exc:
            return Result(kind, f"invalid {kind}: {exc}", error="invalid")

    def handle_call(
        self,
        call: Mapping[str, object],
        *,
        acknowledge_safety_checks: bool = False,
        preview: bool = False,
    ) -> tuple[dict, list[Result]]:
        """Execute a ``computer_call`` item and build its ``computer_call_output``.

        Runs the ``actions`` array (or the preview's single ``action``) in order
        and stops at the first failure. The output always carries a screenshot,
        so the model sees the state the failure left behind. Returns the output
        item plus every per-action `Result` (the host decides what to do with a
        failure; the wire item has no error slot).

        ``pending_safety_checks`` are echoed as ``acknowledged_safety_checks``
        only when ``acknowledge_safety_checks`` is True; acknowledging is a
        policy decision the host owns, so the default leaves them unacknowledged.
        """
        actions: Sequence[Mapping[str, object]]
        if isinstance(call.get("actions"), list):
            actions = call["actions"]  # type: ignore[assignment]
        elif isinstance(call.get("action"), Mapping):
            actions = [call["action"]]  # type: ignore[list-item]
        else:
            actions = []
        results: list[Result] = []
        for action in actions:
            result = self.handle(action)
            results.append(result)
            if not result.ok:
                break
        last_png = results[-1].png if results and results[-1].ok else None
        if last_png is None:
            shot = self.screenshot()
            results.append(shot)
            last_png = shot.png
        output: dict = {
            "type": "computer_call_output",
            "call_id": call.get("call_id"),
            "output": {"type": "input_image" if preview else "computer_screenshot"},
        }
        if last_png is not None:
            output["output"]["image_url"] = (
                "data:image/png;base64," + base64.b64encode(last_png).decode("ascii")
            )
        url = self.current_url()
        if url:
            output["output"]["current_url"] = url
        pending = call.get("pending_safety_checks")
        if acknowledge_safety_checks and isinstance(pending, list) and pending:
            output["acknowledged_safety_checks"] = [
                {k: c.get(k) for k in ("id", "code", "message") if k in c} for c in pending
                if isinstance(c, Mapping)
            ]
        return output, results

    def current_url(self) -> str | None:
        """The bound tab's URL on the browser backend (through the gated ``app
        list``), None elsewhere or when unavailable."""
        if self.driver_name != "browser":
            return None
        try:
            rows = json.loads(self.runtime.app("list"))
            front = self.runtime._frontmost()
        except Exception:
            return None
        for row in rows:
            if isinstance(row, Mapping) and row.get("id") == front:
                return str(row.get("url") or "") or None
        return None

    # -- actions -------------------------------------------------------------------

    def _do_click(self, action: Mapping[str, object]) -> Result:
        x, y = _xy(action)
        button = str(action.get("button", "left")).lower()
        mods = self._held(action.get("keys"))
        if button in _HISTORY_CHORD:
            self.move(x, y)
            table = _HISTORY_CHORD[button]
            chord = table.get(self.driver_name, table["default"])
            result = self.key(chord, action="click")
            if result.ok:
                return Result("click", result.text + f" [{button} button mapped to the history shortcut]")
            return result
        if button not in _BUTTONS:
            raise ValueError(f"button must be left/right/wheel/back/forward, got {button!r}")
        return self.click(x, y, button=_BUTTONS[button], modifiers=mods, action="click")

    def _do_double_click(self, action: Mapping[str, object]) -> Result:
        x, y = _xy(action)
        return self.click(x, y, count=2, modifiers=self._held(action.get("keys")),
                          action="double_click")

    def _do_drag(self, action: Mapping[str, object]) -> Result:
        path = action.get("path")
        if not isinstance(path, list) or len(path) < 2:
            raise ValueError("drag needs a path of at least two points")
        x0, y0 = _path_point(path[0])
        x1, y1 = _path_point(path[-1])
        result = self.drag(x0, y0, x1, y1, action="drag")
        if result.ok and len(path) > 2:
            return Result(result.action, result.text + f" [{len(path) - 2} intermediate path "
                          "points dropped: drivers drag start to end]")
        return result

    def _do_keypress(self, action: Mapping[str, object]) -> Result:
        keys = action.get("keys")
        if not isinstance(keys, list) or not keys:
            raise ValueError("keypress needs a non-empty keys list")
        chords = openai_keys_to_chords([str(k) for k in keys], self.driver_name)
        last: Result | None = None
        for chord in chords:
            last = self.key(chord, action="keypress")
            if not last.ok:
                return last
        assert last is not None
        if len(chords) > 1:
            return Result(last.action, "pressed " + ", ".join(chords))
        return last

    def _do_move(self, action: Mapping[str, object]) -> Result:
        x, y = _xy(action)
        return self.move(x, y, action="move")

    def _do_screenshot(self, action: Mapping[str, object]) -> Result:
        return self.screenshot()

    def _do_scroll(self, action: Mapping[str, object]) -> Result:
        x, y = _xy(action)
        dx = int(action.get("scroll_x", 0) or 0)
        dy = int(action.get("scroll_y", 0) or 0)
        # OpenAI: positive scroll_y scrolls down, positive scroll_x scrolls right.
        # Runtime: positive dy moves content up (a scroll down), positive dx moves
        # content left (a scroll right). The signs agree; the unit is pixels.
        return self.scroll(x, y, dx=dx, dy=dy, unit="pixels", action="scroll")

    def _do_type(self, action: Mapping[str, object]) -> Result:
        text = action.get("text")
        if not isinstance(text, str):
            raise ValueError("type needs a string text")
        return self.type_text(text, action="type")

    def _do_wait(self, action: Mapping[str, object]) -> Result:
        return self.wait(self.default_wait_s, action="wait")

    @staticmethod
    def _held(keys: object) -> tuple[str, ...]:
        """Modifier keys held during a pointer action (OpenAI ``keys`` on
        click/move/scroll). Non-modifier names are rejected."""
        if not keys:
            return ()
        if not isinstance(keys, list):
            raise ValueError("keys must be a list of modifier names")
        mods: list[str] = []
        for k in keys:
            canon = _MODIFIER_ALIASES.get(str(k).strip().lower())
            if canon is None:
                raise ValueError(f"held key {k!r} is not a modifier")
            if canon not in mods:
                mods.append(canon)
        return tuple(mods)
