"""Small live AT-SPI reads for browser dialog confirmation.

Ref checks follow only the resolved native target's parents. Keyboard and
coordinate checks query dialog roles before walking browser chrome, never web
content. All calls run on LinuxDriver's AT-SPI thread; no snapshot or recursive
cache clear is used.
"""
from collections import deque
from dataclasses import replace
from types import SimpleNamespace

from a11y_computer_use import observe, safety
from a11y_computer_use.schema import (
    Bounds, Click, ComputerUseError, Element, ErrorCode, KeyChord, Scope, canonical_chord,
)


_DOCUMENTS = {"document web", "document frame", "document text", "document spreadsheet",
              "document presentation", "document"}
_ROLES = {"push button": "AXButton", "button": "AXButton", "dialog": "AXDialog",
          "alert": "AXAlert", "frame": "AXWindow", "window": "AXWindow"}


def _dialog_roots(root):
    """Native role query: [] proves absence; None retains the bounded fallback.

    Collection runs in the application, so an empty result costs one bus
    round trip instead of reading every toolbar control. Include nested
    dialogs, not only immediate frame children. Complete results can serve
    modal discovery while its subscribed boundary epoch remains unchanged.
    """
    from a11y_computer_use.drivers import _atspi as api, _atspi_events

    if root is None:
        return []
    api._drop_cached_properties(root)
    role = api._role_name(root)
    if role in {"dialog", "alert"}:
        return [root]
    if role in {"frame", "window"}:
        title = api._call_first(root, ("get_name",), default="") or ""
        if title.casefold() in {"leave site?", "reload site?"} or "wants to:" in title.casefold():
            return [root]
    try:
        coll = api._call_first(root, ("get_collection_iface", "get_collection"))
        if coll is None:
            return None
        Atspi = api._atspi()
        mt = Atspi.CollectionMatchType
        roles = [Atspi.Role.DIALOG, Atspi.Role.ALERT, Atspi.Role.WINDOW, Atspi.Role.FRAME]
        if hasattr(Atspi.Role, "FILE_CHOOSER"):
            roles.append(Atspi.Role.FILE_CHOOSER)
        rule = Atspi.MatchRule.new(
            Atspi.StateSet.new([]), mt.NONE, {}, mt.NONE,
            roles,
            mt.ANY, [], mt.NONE, False,
        )
        # Zero means all matches: do not silently truncate dialog discovery.
        generation = (_atspi_events.modal_epoch()
                      if _atspi_events.modal_cache_ready() else None)
        hits = coll.get_matches(rule, Atspi.CollectionSortOrder.CANONICAL, 0, True)
        if hits is None:
            return None
        result = list(hits)
        if hasattr(Atspi.Role, "FILE_CHOOSER"):
            api._remember_modal_collection(root, result, generation)
        return result
    except Exception:
        return None


def _button_shortcut(node):
    """Read an actual AT-SPI mnemonic or accelerator, never infer from a name."""
    from a11y_computer_use.drivers._linux_menus import (
        _key_binding, mnemonic_from_binding, shortcut_from_binding,
    )

    binding = _key_binding(node)
    mnemonic = mnemonic_from_binding(binding)
    return "Alt+" + mnemonic if mnemonic else shortcut_from_binding(binding)


def linux_prompt(action, app):
    """Return the shared prompt from live ancestry or a bounded chrome walk."""
    from a11y_computer_use.drivers import _atspi as api

    elements = []
    key = canonical_chord(action.chord).casefold() if isinstance(action, KeyChord) else ""

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
        # Read a button's StateSet only once for both flags. This cache is
        # local to this node/read and cannot survive approval or dispatch.
        states = (api._call_first(node, ("get_state_set", "getState"))
                  if button and key and not key.startswith("alt+") else None)

        def state(name):
            if states is None:
                return False
            flag = getattr(api._atspi().StateType, name, None)
            return flag is not None and bool(states.contains(flag))

        el = Element(str(len(elements)), _ROLES.get(role, role), name, None,
                     bounds, "dialog-probe", parent=parent,
                     focused=state("FOCUSED"), default=state("IS_DEFAULT"),
                     shortcut=_button_shortcut(node) if button and key.startswith("alt+") else None)
        elements.append(el)
        return el, role

    judged = action
    if isinstance(action, Click) and isinstance(action.target, Element):
        node = observe.ax_handle_for(action.target.snapshot_id, action.target.ref)
        if node is None:
            return NotImplemented  # Injected drivers may have no native handles.
        seen = set()
        for _ in range(api._FULL_ANCESTRY_MAX):
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
        roots = _dialog_roots(root)
        if roots is None:
            roots = [root] if root is not None else []
        pending = deque((node, None) for node in roots)
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
