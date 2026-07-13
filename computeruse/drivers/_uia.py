"""Windows UI Automation adapter: presents the UIA tree to the SHARED,
platform-free pruning engine (`observe.build_snapshot`).

The trick that makes the Windows backend small: map UIA `ControlType`s onto the
**same AX role vocabulary** the pruning engine already keys off (`AXButton`,
`AXTextField`, `AXWindow`, ...), and map UIA patterns onto the AX action names
(`AXPress`/`AXPick`). Then a Windows tree prunes/indexes through the identical
engine as macOS, with zero engine changes.

Windows-only; imported lazily by `drivers/windows.py`. Every attribute read is
defensive — a flaky cross-process UIA call must degrade to a sane default, not
crash the walk.
"""

from __future__ import annotations

from collections.abc import Sequence

from computeruse.observe import DisplayGeometry, RawNode
from computeruse.schema import Display

# UIA ControlTypeName -> canonical AX role (the pruning engine's vocabulary).
_ROLE = {
    "ButtonControl": "AXButton",
    "SplitButtonControl": "AXButton",
    "CheckBoxControl": "AXCheckBox",
    "RadioButtonControl": "AXRadioButton",
    "HyperlinkControl": "AXLink",
    "EditControl": "AXTextField",
    "DocumentControl": "AXTextArea",
    "TextControl": "AXStaticText",
    "ImageControl": "AXImage",
    "WindowControl": "AXWindow",
    "MenuItemControl": "AXMenuItem",
    "MenuBarControl": "AXMenuBar",
    "MenuControl": "AXMenu",
    "ListControl": "AXList",
    "ListItemControl": "AXRow",
    "TreeControl": "AXOutline",
    "TreeItemControl": "AXRow",
    "DataGridControl": "AXTable",
    "TableControl": "AXTable",
    "ComboBoxControl": "AXComboBox",
    "ToolBarControl": "AXToolbar",
    "ScrollBarControl": "AXScrollBar",
    "TabControl": "AXTabGroup",
    "GroupControl": "AXGroup",
    "PaneControl": "AXGroup",
    "CustomControl": "AXGroup",
}


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _has_pattern(node, getter: str) -> bool:
    return _safe(lambda: bool(getattr(node, getter)())) or False


def _actions(node) -> tuple[str, ...]:
    acts: list[str] = []
    if _has_pattern(node, "GetInvokePattern"):
        acts.append("AXPress")
    if _has_pattern(node, "GetTogglePattern"):
        acts.append("AXPress")
    if _has_pattern(node, "GetExpandCollapsePattern"):
        acts.append("AXPress")
    if _has_pattern(node, "GetSelectionItemPattern"):
        acts.append("AXPick")
    return tuple(dict.fromkeys(acts))  # de-dup, preserve order


class UIAAccessor:
    """`observe.TreeAccessor` over `uiautomation.Control` handles."""

    def read(self, node: object) -> RawNode:
        role = _ROLE.get(_safe(lambda: node.ControlTypeName, "") or "", "AXGroup")
        rect = _safe(lambda: node.BoundingRectangle)
        position = size = None
        if rect is not None:
            left = _safe(lambda: rect.left, 0)
            top = _safe(lambda: rect.top, 0)
            right = _safe(lambda: rect.right, 0)
            bottom = _safe(lambda: rect.bottom, 0)
            if right > left and bottom > top:
                position = (float(left), float(top))
                size = (float(right - left), float(bottom - top))
        value = None
        vp = _safe(lambda: node.GetValuePattern())
        if vp is not None:
            value = _safe(lambda: vp.Value)
        return RawNode(
            role=role,
            subrole=None,
            title=str(_safe(lambda: node.Name, "") or ""),
            value=value,
            description="",
            enabled=bool(_safe(lambda: node.IsEnabled, True)),
            focused=bool(_safe(lambda: node.HasKeyboardFocus, False)),
            position=position,
            size=size,
            actions=_actions(node),
        )

    def children(self, node: object) -> Sequence[object]:
        return _safe(lambda: node.GetChildren(), []) or []


def primary_geometry() -> tuple[DisplayGeometry, ...]:
    """The primary monitor as one `DisplayGeometry`. UIA bounds are physical
    pixels, so scale=1.0 makes the engine's point→pixel projection an identity."""
    import ctypes

    _safe(lambda: ctypes.windll.user32.SetProcessDPIAware())  # physical px, DPI-aware
    width = _safe(lambda: ctypes.windll.user32.GetSystemMetrics(0), 1920) or 1920
    height = _safe(lambda: ctypes.windll.user32.GetSystemMetrics(1), 1080) or 1080
    display = Display(display_id=0, width=int(width), height=int(height), scale=1.0, is_main=True)
    return (DisplayGeometry(display=display, origin=(0.0, 0.0)),)


def find_window(app: str):
    """The top-level window Control whose title or class or process matches
    ``app`` (substring, case-insensitive), or None. ``app`` may be a window
    title, a window ClassName, or a process exe name."""
    import uiautomation as auto

    needle = (app or "").lower()
    root = auto.GetRootControl()
    for w in _safe(lambda: root.GetChildren(), []) or []:
        name = (_safe(lambda: w.Name, "") or "").lower()
        cls = (_safe(lambda: w.ClassName, "") or "").lower()
        if needle and (needle in name or needle in cls):
            return w
    return None
