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

from a11y_computer_use.observe import DisplayGeometry, RawNode
from a11y_computer_use.schema import Display

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


def _checked(node) -> bool | None:
    """Toggle state via UIA TogglePattern (ToggleState 0=off,1=on,2=indeterminate);
    None when the control has no toggle pattern."""
    tp = _safe(lambda: node.GetTogglePattern())
    if tp is None:
        return None
    state = _safe(lambda: tp.ToggleState)
    return None if state is None else state != 0


def _selected(node) -> bool:
    sip = _safe(lambda: node.GetSelectionItemPattern())
    return bool(_safe(lambda: sip.IsSelected, False)) if sip is not None else False


def _expanded(node) -> bool | None:
    """Disclosure via ExpandCollapsePattern (0=collapsed,1=expanded,2=partial,
    3=leaf-no-children); None when the control does not expand."""
    ecp = _safe(lambda: node.GetExpandCollapsePattern())
    if ecp is None:
        return None
    state = _safe(lambda: ecp.ExpandCollapseState)
    if state is None or state == 3:  # LeafNode: not an expandable control
        return None
    return state in (1, 2)


class UIAAccessor:
    """`observe.TreeAccessor` over `uiautomation.Control` handles."""

    def read(self, node: object) -> RawNode:
        role = _ROLE.get(_safe(lambda: node.ControlTypeName, "") or "", "AXGroup")
        # UIA_IsPasswordPropertyId: a password edit is a text field whose value
        # must never be read or emitted. Mapping it onto AXSecureTextField makes
        # the engine mark it `secure`, so press/set_value/type refuse it and the
        # snapshot withholds its value, exactly as on the other backends.
        # Unit-tested with a fake control; not yet live-verified on Windows.
        if role in ("AXTextField", "AXTextArea") and bool(_safe(lambda: node.IsPassword, False)):
            role = "AXSecureTextField"
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
        if role != "AXSecureTextField":  # never read a password field's value
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
            checked=_checked(node),
            selected=_selected(node),
            expanded=_expanded(node),
            stable_id=(_safe(lambda: node.AutomationId, "") or "") or None,
        )

    def children(self, node: object) -> Sequence[object]:
        return _safe(lambda: node.GetChildren(), []) or []


def displays_from_monitors(
    monitors: Sequence[tuple[bool, int, int, int, int]],
) -> tuple[Display, ...]:
    """One `Display` per monitor, primary first.

    Each tuple is ``(is_primary, left, top, width, height)`` in virtual-screen
    pixels. ``left`` and ``top`` may be negative when the monitor sits left of
    or above the primary. The display's width and height are that monitor's
    own size. Callers address it in display-local pixels, 0..width-1 and
    0..height-1. The virtual-screen origin is not added here; a driver that
    posts global input adds it later, the way macOS adds ``CGDisplayBounds``.
    """
    ordered = sorted(enumerate(monitors), key=lambda item: (not item[1][0], item[0]))
    displays: list[Display] = []
    for display_id, (_index, (is_primary, _left, _top, width, height)) in enumerate(ordered):
        if width < 1 or height < 1:
            continue
        displays.append(Display(
            display_id=display_id, width=int(width), height=int(height),
            scale=1.0, is_main=bool(is_primary) or display_id == 0,
        ))
    if displays and not any(item.is_main for item in displays):
        first = displays[0]
        displays[0] = Display(first.display_id, first.width, first.height, first.scale, True)
    return tuple(displays)


def _enum_monitors() -> list[tuple[bool, int, int, int, int]]:
    """``(is_primary, left, top, width, height)`` for each attached monitor.

    Raises ``AttributeError`` off Windows, where ``ctypes.windll`` is absent.
    """
    import ctypes

    user32 = ctypes.windll.user32

    class _RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long), ("top", ctypes.c_long),
            ("right", ctypes.c_long), ("bottom", ctypes.c_long),
        ]

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("rcMonitor", _RECT),
            ("rcWork", _RECT),
            ("dwFlags", ctypes.c_ulong),
        ]

    found: list[tuple[bool, int, int, int, int]] = []
    prototype = ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_RECT), ctypes.c_ssize_t,
    )

    @prototype
    def callback(hmon, _hdc, _lprc, _data):
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            rect = info.rcMonitor
            width = int(rect.right - rect.left)
            height = int(rect.bottom - rect.top)
            if width > 0 and height > 0:
                found.append((bool(info.dwFlags & 1), int(rect.left), int(rect.top), width, height))
        return 1

    user32.EnumDisplayMonitors(None, None, callback, 0)
    return found


def attached_displays() -> tuple[Display, ...]:
    """Every monitor, or the primary geometry when the enumeration is empty."""
    monitors = _enum_monitors()
    if not monitors:
        return tuple(geom.display for geom in primary_geometry())
    return displays_from_monitors(monitors)


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
    """The top-level window Control whose title, class, or process-exe matches
    ``app`` (substring, case-insensitive), or None. ``app`` may be a window
    title, a window ClassName, or a process exe name (e.g. "notepad.exe")."""
    import uiautomation as auto

    from a11y_computer_use.drivers import _win_system

    needle = (app or "").lower()
    if not needle:
        return None
    root = auto.GetRootControl()
    for w in _safe(lambda: root.GetChildren(), []) or []:
        name = (_safe(lambda: w.Name, "") or "").lower()
        cls = (_safe(lambda: w.ClassName, "") or "").lower()
        exe = (_win_system._exe_for_pid(_safe(lambda: w.ProcessId, 0)) or "").lower()
        if needle in name or needle in cls or needle in exe:
            return w
    return None
