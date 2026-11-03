"""#1470: an APG menu button with no aria-controls opens a portal menu at the
end of <body>. Its item is owned by that button only when exactly one menu
is open; zero or several open menus keep today's refusal."""
from a11y_computer_use.drivers import _atspi
from tests.test_linux_synthetic import _GeckoNode

BUTTON = (351, 183, 87, 37)
MENU = (351, 220, 120, 93)
MONO = (392, 282, 69, 31)
INSIDE = (426, 297)


def _tree(gnode_states=("SHOWING", "VISIBLE", "EXPANDED")):
    app = _GeckoNode("application", "Google Chrome")
    app.toolkit = "Chromium"
    button = _GeckoNode("push button", "Font: Sans", gnode_states)
    button.attributes = {"haspopup": "true"}
    toolbar = _GeckoNode("tool bar", "Text formatting", ("SHOWING", "VISIBLE"), [button])
    items = []
    for name in ("Sans", "Serif", "Mono"):
        node = _GeckoNode("radio menu item", name, ("SHOWING", "VISIBLE"))
        node.actions = ["check"]
        items.append(node)
    menu = _GeckoNode("menu", "Font family", ("SHOWING", "VISIBLE"), items)
    # Portal: the menu is the last child of the document, not near the button.
    document = _GeckoNode("document web", "fixture", ("SHOWING", "VISIBLE"), [toolbar, menu])
    frame = _GeckoNode("frame", "fixture - Google Chrome", ("SHOWING", "VISIBLE"), [document])
    app.children = [frame]
    frame.parent = app
    rects = {id(button): BUTTON, id(menu): MENU, id(items[2]): MONO}
    return app, document, button, menu, items[2], rects


def _wire(monkeypatch, app, rects):
    stack = [app]
    while stack:
        node = stack.pop()
        node.application = app
        stack.extend(node.children)
    monkeypatch.setattr(_atspi, "_relation_targets", lambda node, kind: [])
    monkeypatch.setattr(_atspi, "_raw_rect", lambda node, coord: rects.get(id(node), (0, 0, 1280, 800)))


def test_exactly_one_open_menu_is_owned_by_the_only_expanded_opener(monkeypatch):
    app, document, button, menu, mono, rects = _tree()
    _wire(monkeypatch, app, rects)
    # Before #1470: covered by "Font: Sans (open menu, unreadable)".
    assert _atspi.chromium_menu_occluder(mono, *INSIDE) is None
    assert _atspi.chromium_menu_occluder(mono) is None  # node-bound paths use the centre


def test_a_point_outside_the_only_open_menu_is_still_refused(monkeypatch):
    app, document, button, menu, mono, rects = _tree()
    _wire(monkeypatch, app, rects)
    occluder = _atspi.chromium_menu_occluder(mono, 900, 700)
    assert occluder is not None and "Font: Sans (open menu, unreadable)" in occluder[1]


def test_page_target_under_the_open_menu_is_still_refused(monkeypatch):
    app, document, button, menu, mono, rects = _tree()
    other = _GeckoNode("push button", "Save", ("SHOWING", "VISIBLE"))
    document.children.insert(1, other)
    other.parent = document
    _wire(monkeypatch, app, rects)
    occluder = _atspi.chromium_menu_occluder(other, *INSIDE)
    assert occluder is not None and "Font: Sans" in occluder[1]


def test_two_open_menus_keep_the_refusal(monkeypatch):
    app, document, button, menu, mono, rects = _tree()
    second = _GeckoNode("menu", "Actions", ("SHOWING", "VISIBLE"),
                        [_GeckoNode("menu item", "Delete", ("SHOWING", "VISIBLE"))])
    document.children.append(second)
    second.parent = document
    rects[id(second)] = (900, 600, 100, 100)
    _wire(monkeypatch, app, rects)
    assert _atspi.chromium_menu_occluder(mono, *INSIDE) is not None


def test_two_expanded_openers_keep_the_refusal(monkeypatch):
    app, document, button, menu, mono, rects = _tree()
    opener = _GeckoNode("push button", "Actions", ("SHOWING", "VISIBLE", "EXPANDED"))
    opener.attributes = {"haspopup": "menu"}
    document.children.insert(0, opener)
    opener.parent = document
    _wire(monkeypatch, app, rects)
    occluder = _atspi.chromium_menu_occluder(mono, *INSIDE)
    assert occluder is not None and "open menu, unreadable" in occluder[1]


def test_no_open_menu_keeps_the_refusal(monkeypatch):
    """Zero showing menus: the expanded opener's menu cannot be located."""
    app, document, button, menu, mono, rects = _tree()
    menu.states.discard("SHOWING")
    _wire(monkeypatch, app, rects)
    occluder = _atspi.chromium_menu_occluder(mono, *INSIDE)
    assert occluder is not None and "Font: Sans (open menu, unreadable)" in occluder[1]


def test_no_expanded_opener_is_unchanged(monkeypatch):
    app, document, button, menu, mono, rects = _tree(("SHOWING", "VISIBLE"))
    _wire(monkeypatch, app, rects)
    assert _atspi.chromium_menu_occluder(mono, *INSIDE) is None
