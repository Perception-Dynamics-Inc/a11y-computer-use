"""Small live AT-SPI reads for browser dialog confirmation.

Ref checks follow only the resolved native target's parents. Keyboard and
coordinate checks walk browser chrome, never web-document content. All calls
run on LinuxDriver's AT-SPI thread; no snapshot or recursive cache clear is used.
"""
from collections import deque
from dataclasses import replace
from types import SimpleNamespace

from a11y_computer_use import observe, safety
from a11y_computer_use.schema import Bounds, Click, ComputerUseError, Element, ErrorCode, Scope


_DOCUMENTS = {"document web", "document frame", "document text", "document spreadsheet",
              "document presentation", "document"}
_ROLES = {"push button": "AXButton", "button": "AXButton", "dialog": "AXDialog",
          "alert": "AXAlert", "frame": "AXWindow", "window": "AXWindow"}


def linux_prompt(action, app):
    """Return the shared prompt from live ancestry or a bounded chrome walk."""
    from a11y_computer_use.drivers import _atspi as api

    elements = []

    def read(node, parent=None, *, geometry=False):
        api._drop_cached_properties(node)
        role = api._role_name(node)
        name = api._call_first(node, ("get_name",), default="") or ""
        bounds = None
        if geometry and role in {"button", "push button"}:
            pos, size = api._extents(node)
            if pos is not None and size is not None:
                bounds = Bounds(action.target.display_id, *pos, *size)
        button = role in {"button", "push button"}
        el = Element(str(len(elements)), _ROLES.get(role, role), name, None,
                     bounds, "dialog-probe", parent=parent,
                     focused=api._state_has(node, "FOCUSED") if button else False,
                     default=api._has_default_state(node) if button else False)
        elements.append(el)
        return el, role

    judged = action
    if isinstance(action, Click) and isinstance(action.target, Element):
        node = observe.ax_handle_for(action.target.snapshot_id, action.target.ref)
        if node is None:
            return NotImplemented  # Injected drivers may have no native handles.
        seen = set()
        for _ in range(32):
            if node is None:
                break
            identity = api._atspi_wrapper_key(node) or id(node)
            if identity in seen:
                break
            seen.add(identity)
            el, role = read(node)
            if len(elements) > 1:
                elements[-2] = replace(elements[-2], parent=el.ref)
            if role in _DOCUMENTS:
                break  # Page controls never inherit a browser dialog's label.
            node = api._parent_of(node)
        else:
            raise ComputerUseError(ErrorCode.UNSUPPORTED, "Cannot verify browser dialog ancestry")
        judged = replace(action, target=elements[0])
    else:
        # Native browser prompts live outside the web document. Avoid probing
        # unrelated desktop apps for ordinary Return/Space input.
        if not any(name in app.casefold() for name in (
            "chrom", "browser", "firefox", "brave", "edge", "opera", "vivaldi",
        )):
            return None
        root = api.find_root(app, Scope.WINDOW)
        pending = deque([(root, None)]) if root is not None else deque()
        seen = set()
        while pending and len(elements) < 400:
            node, parent = pending.popleft()
            identity = api._atspi_wrapper_key(node) or id(node)
            if identity in seen:
                continue
            seen.add(identity)
            el, role = read(node, parent, geometry=isinstance(action, Click))
            if role in _DOCUMENTS:
                continue
            count = api._child_count(node)
            if count + len(pending) + len(elements) > 400:
                raise ComputerUseError(ErrorCode.UNSUPPORTED, "Browser dialog probe exceeded its chrome budget")
            for index in range(max(0, count)):
                child = api._child_at(node, index)
                if child is not None:
                    pending.append((child, el.ref))
        if pending:
            raise ComputerUseError(ErrorCode.UNSUPPORTED, "Browser dialog probe exceeded its chrome budget")
    return safety.browser_dialog_prompt(judged, SimpleNamespace(elements=tuple(elements)), app)
