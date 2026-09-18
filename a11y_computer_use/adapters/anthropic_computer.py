"""Anthropic computer-use executor: ``computer_toolset_20260801`` members and the
legacy ``computer_20251124`` / ``computer_20250124`` ``action`` shape, executed
through the gated Runtime.

Wire shapes (platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool):

* Toolset: the request declares ``{"type": "computer_toolset_20260801"}``; Claude
  emits ``tool_use`` blocks whose ``name`` is the member (``left_click``,
  ``screenshot``, ...) with ``toolset_name: "computer"`` and the member's
  parameters in ``input``. Results carry ``toolset_name`` too. No beta header.
  Screen size is implied by the screenshots you return.
* Legacy: ``{"type": "computer_20251124", "name": "computer", "display_width_px",
  "display_height_px"}`` with beta header ``computer-use-2025-11-24`` (the
  ``computer_20250124`` variant uses ``computer-use-2025-01-24``); Claude emits
  ``name: "computer"`` and puts the member name in ``input.action``.

Both shapes share one action vocabulary, handled by `AnthropicComputerAdapter.handle`.
"""

from __future__ import annotations

from collections.abc import Mapping

from a11y_computer_use.adapters.base import (
    ComputerAdapter,
    Result,
    modifiers_from_text,
    xdotool_to_chord,
)

__all__ = ["ACTIONS", "TOOLSET", "LEGACY_VERSIONS", "AnthropicComputerAdapter"]

TOOLSET = "computer_toolset_20260801"

#: Legacy tool types -> the beta header they need.
LEGACY_VERSIONS: dict[str, str] = {
    "computer_20251124": "computer-use-2025-11-24",
    "computer_20250124": "computer-use-2025-01-24",
}

#: The 17 member tools of the toolset (and the legacy ``action`` values).
ACTIONS: frozenset[str] = frozenset({
    "screenshot", "zoom", "left_click", "right_click", "middle_click", "double_click",
    "triple_click", "left_click_drag", "left_mouse_down", "left_mouse_up", "mouse_move",
    "cursor_position", "scroll", "type", "key", "hold_key", "wait",
})

_CLICKS: dict[str, tuple[str, int]] = {
    "left_click": ("left", 1),
    "right_click": ("right", 1),
    "middle_click": ("middle", 1),
    "double_click": ("left", 2),
    "triple_click": ("left", 3),
}

#: scroll_direction -> (dx, dy) sign in the Runtime's convention: positive dy
#: moves content up (a wheel-down, "scroll down"), positive dx moves content left.
_SCROLL_SIGN: dict[str, tuple[int, int]] = {
    "down": (0, 1), "up": (0, -1), "right": (1, 0), "left": (-1, 0),
}


def _coordinate(value: object, what: str) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{what} must be [x, y], got {value!r}")
    return int(value[0]), int(value[1])


class AnthropicComputerAdapter(ComputerAdapter):
    """Execute Anthropic computer-use actions through a11y-computer-use.

    Args (beyond `ComputerAdapter`):
        display_width_px / display_height_px: The legacy tool definition's
            declared screenshot size. When given, their long edge becomes the
            screenshot budget; the values actually declared are read back from
            the first screenshot so they always equal the image Claude sees.
        display_number: Legacy ``display_number`` (X11 display), passed through.
        enable_zoom: Whether the toolset/legacy definition advertises zoom.
    """

    def __init__(
        self,
        runtime,
        *,
        display_width_px: int | None = None,
        display_height_px: int | None = None,
        display_number: str | int | None = None,
        enable_zoom: bool = True,
        **kwargs,
    ) -> None:
        if "max_long_edge" not in kwargs:
            declared = max(int(display_width_px or 0), int(display_height_px or 0))
            if declared:
                kwargs["max_long_edge"] = declared
        super().__init__(runtime, **kwargs)
        self.display_number = display_number
        self.enable_zoom = enable_zoom

    # -- tool definition -------------------------------------------------------

    @staticmethod
    def beta_header(version: str = TOOLSET) -> str | None:
        """The ``anthropic-beta`` value a version needs; None for the toolset."""
        if version == TOOLSET:
            return None
        if version in LEGACY_VERSIONS:
            return LEGACY_VERSIONS[version]
        raise ValueError(f"unknown computer tool version {version!r}")

    def display_size(self) -> tuple[int, int]:
        """(width, height) of the screenshots Claude receives. Probes one
        screenshot through the gate when none has been taken."""
        if self._screen is None:
            shot = self.screenshot()
            if not shot.ok or self._screen is None:
                raise RuntimeError(f"cannot determine display size: {shot.text}")
        return self._screen.width, self._screen.height

    def tool_definition(self, version: str = TOOLSET) -> dict:
        """The ``tools`` entry for ``version``.

        The toolset entry is just its type (plus ``configs`` when zoom is off).
        Legacy entries declare ``name: "computer"`` and the exact screenshot
        size, which is why they probe a screenshot first.
        """
        if version == TOOLSET:
            definition: dict = {"type": TOOLSET}
            if not self.enable_zoom:
                definition["configs"] = {"zoom": {"enabled": False}}
            return definition
        if version not in LEGACY_VERSIONS:
            raise ValueError(f"unknown computer tool version {version!r}")
        width, height = self.display_size()
        definition = {
            "type": version,
            "name": "computer",
            "display_width_px": width,
            "display_height_px": height,
        }
        if self.display_number is not None:
            definition["display_number"] = str(self.display_number)
        if version == "computer_20251124" and self.enable_zoom:
            definition["enable_zoom"] = True
        return definition

    # -- dispatch ----------------------------------------------------------------

    def handle(self, input: Mapping[str, object], *, name: str | None = None) -> Result:
        """Execute one action. ``input`` is the ``tool_use.input``; ``name`` is the
        member name for toolset blocks (legacy blocks carry it as
        ``input["action"]``)."""
        action = input.get("action") or name
        if not isinstance(action, str) or not action:
            return Result("?", "missing action: pass input.action (legacy) or the member name",
                          error="invalid")
        handler = getattr(self, f"_do_{action}", None)
        if action not in ACTIONS or handler is None:
            return Result(action, f"unknown computer action {action!r}; supported: "
                          + ", ".join(sorted(ACTIONS)), error="invalid")
        try:
            return handler(input)
        except (ValueError, KeyError, TypeError) as exc:
            return Result(action, f"invalid {action}: {exc}", error="invalid")

    def handle_tool_use(self, block) -> dict:
        """``tool_use`` block (dict or SDK object) -> complete ``tool_result`` dict.

        Toolset blocks (``toolset_name`` set, or ``name`` not ``"computer"``) are
        dispatched by member name and the result echoes ``toolset_name``.
        """
        get = block.get if isinstance(block, Mapping) else lambda k, d=None: getattr(block, k, d)
        name = get("name")
        toolset_name = get("toolset_name")
        raw_input = get("input") or {}
        tool_use_id = get("id")
        if toolset_name or name != "computer":
            result = self.handle(raw_input, name=name)
            return result.to_anthropic_tool_result(tool_use_id, toolset_name=toolset_name or "computer")
        return self.handle(raw_input).to_anthropic_tool_result(tool_use_id)

    # -- members -----------------------------------------------------------------

    def _point_or_cursor(self, input: Mapping[str, object], key: str = "coordinate") -> tuple[int, int]:
        value = input.get(key)
        if value is None:
            if self._cursor is None:
                raise ValueError(f"no {key} given and no prior pointer position")
            return self._cursor
        return _coordinate(value, key)

    def _do_screenshot(self, input: Mapping[str, object]) -> Result:
        return self.screenshot()

    def _do_zoom(self, input: Mapping[str, object]) -> Result:
        region = input.get("region")
        if not isinstance(region, (list, tuple)) or len(region) != 4:
            raise ValueError(f"region must be [x0, y0, x1, y1], got {region!r}")
        x0, y0, x1, y1 = (int(v) for v in region)
        return self.zoom(x0, y0, x1, y1)

    def _click(self, action: str, input: Mapping[str, object]) -> Result:
        button, count = _CLICKS[action]
        x, y = self._point_or_cursor(input)
        mods = modifiers_from_text(input.get("text"))  # type: ignore[arg-type]
        return self.click(x, y, button=button, count=count, modifiers=mods, action=action)

    def _do_left_click(self, input):
        return self._click("left_click", input)

    def _do_right_click(self, input):
        return self._click("right_click", input)

    def _do_middle_click(self, input):
        return self._click("middle_click", input)

    def _do_double_click(self, input):
        return self._click("double_click", input)

    def _do_triple_click(self, input):
        return self._click("triple_click", input)

    def _do_left_click_drag(self, input: Mapping[str, object]) -> Result:
        x0, y0 = _coordinate(input.get("start_coordinate"), "start_coordinate")
        x1, y1 = _coordinate(input.get("coordinate"), "coordinate")
        return self.drag(x0, y0, x1, y1)

    def _do_left_mouse_down(self, input: Mapping[str, object]) -> Result:
        if input.get("coordinate") is not None:
            x, y = _coordinate(input["coordinate"], "coordinate")
            self.move(x, y)
        return self.mouse_down()

    def _do_left_mouse_up(self, input: Mapping[str, object]) -> Result:
        if input.get("coordinate") is not None:
            x, y = _coordinate(input["coordinate"], "coordinate")
            self.move(x, y)
        return self.mouse_up()

    def _do_mouse_move(self, input: Mapping[str, object]) -> Result:
        x, y = _coordinate(input.get("coordinate"), "coordinate")
        return self.move(x, y)

    def _do_cursor_position(self, input: Mapping[str, object]) -> Result:
        return self.cursor_position()

    def _do_scroll(self, input: Mapping[str, object]) -> Result:
        direction = str(input.get("scroll_direction", "down")).lower()
        if direction not in _SCROLL_SIGN:
            raise ValueError(f"scroll_direction must be up/down/left/right, got {direction!r}")
        amount = int(input.get("scroll_amount", 3))
        sx, sy = _SCROLL_SIGN[direction]
        coord = input.get("coordinate")
        x, y = _coordinate(coord, "coordinate") if coord is not None else (None, None)
        mods = modifiers_from_text(input.get("text"))  # type: ignore[arg-type]
        result = self.scroll(x, y, dx=sx * amount, dy=sy * amount, unit="lines")
        if mods and result.ok:
            return Result(result.action, result.text + f" [modifiers {'+'.join(mods)} not applied: "
                          "wheel scrolls carry no modifiers on this backend]")
        return result

    def _do_type(self, input: Mapping[str, object]) -> Result:
        text = input.get("text")
        if not isinstance(text, str):
            raise ValueError("type needs a string text")
        return self.type_text(text)

    def _do_key(self, input: Mapping[str, object]) -> Result:
        text = input.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("key needs a non-empty text such as 'ctrl+s' or 'Return'")
        chord = xdotool_to_chord(text, self.driver_name)
        return self.key(chord, repeat=int(input.get("repeat", 1)))

    def _do_hold_key(self, input: Mapping[str, object]) -> Result:
        text = input.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("hold_key needs a non-empty text")
        chord = xdotool_to_chord(text, self.driver_name)
        duration = float(input.get("duration", 0))
        result = self.key(chord, action="hold_key")
        if result.ok:
            return Result(result.action, result.text + f" [approximation: pressed once instead of "
                          f"held for {duration:g}s; the drivers expose no key-hold primitive]")
        return result

    def _do_wait(self, input: Mapping[str, object]) -> Result:
        return self.wait(float(input.get("duration", 1.0)))
