"""CDP accessibility adapter — map a Chromium AX tree onto the canonical schema.

`Accessibility.getFullAXTree` returns flat AX nodes (ARIA computed roles, no
geometry). `DOMSnapshot.captureSnapshot` returns every laid-out box in one call.
This module fuses them: `build_tree` indexes the AX nodes and, via each node's
``backendDOMNodeId``, joins the DOMSnapshot layout for geometry and detects
password inputs for secure-field safety. The result is a `TreeAccessor` that the
platform-free `observe.build_snapshot` walks exactly like the macOS/Linux ones —
so a web page prunes, indexes, and re-resolves through the identical core.

Roles are mapped to the SAME ``AX*`` vocabulary the pruning engine keys off
(``observe._CLICKABLE_ROLES`` etc.), and interactive web roles get a synthetic
``AXPress`` action (the AX tree carries no action list) so they read as clickable.
``stable_id`` is the backend DOM node id — stable across snapshots within a page,
which gives `observe._match_anchor` a deterministic re-resolution key.
"""

from __future__ import annotations

from collections.abc import Sequence

from computeruse.observe import RawNode

#: CDP/ARIA computed role -> canonical AX role. Best-effort and readable; roles
#: absent here fall back to AXGroup (a wrapper the pruner collapses when empty).
_ROLE = {
    "button": "AXButton",
    "link": "AXLink",
    "checkbox": "AXCheckBox",
    "switch": "AXCheckBox",
    "radio": "AXRadioButton",
    "menuitem": "AXMenuItem",
    "menuitemcheckbox": "AXMenuItem",
    "menuitemradio": "AXMenuItem",
    "menu": "AXMenu",
    "menubar": "AXMenuBar",
    "tab": "AXTab",
    "combobox": "AXComboBox",
    "listbox": "AXList",
    "option": "AXMenuItem",
    "textbox": "AXTextField",  # multiline -> AXTextArea below; password -> secure
    "searchbox": "AXSearchField",
    "spinbutton": "AXTextField",
    "slider": "AXSlider",
    "heading": "AXHeading",
    "img": "AXImage",
    "image": "AXImage",
    "StaticText": "AXStaticText",
    "text": "AXStaticText",
    "RootWebArea": "AXWebArea",
    "WebArea": "AXWebArea",
    "list": "AXList",
    "table": "AXTable",
    "grid": "AXGrid",
    "treegrid": "AXGrid",
    "row": "AXRow",
    "cell": "AXCell",
    "gridcell": "AXCell",
    "columnheader": "AXCell",
    "rowheader": "AXCell",
    "tree": "AXOutline",
    "treeitem": "AXRow",
    "progressbar": "AXProgressIndicator",
    "separator": "AXSplitter",
    "disclosuretriangle": "AXDisclosureTriangle",
    "tablist": "AXTabGroup",
}

#: Web roles that activate on click but carry no AX action list — we synthesize
#: AXPress so ``observe._flags`` marks them clickable.
_INTERACTIVE = frozenset(
    {"button", "link", "checkbox", "switch", "radio", "menuitem", "menuitemcheckbox",
     "menuitemradio", "tab", "combobox", "option", "slider", "spinbutton", "treeitem",
     "disclosuretriangle"}
)

_SECURE_ROLE = "AXSecureTextField"


def _prop(node: dict) -> dict:
    """Flatten an AX node's ``properties`` list into ``{name: value}``."""
    out: dict[str, object] = {}
    for p in node.get("properties", ()):
        out[p.get("name", "")] = p.get("value", {}).get("value")
    return out


class CDPAccessor:
    """`TreeAccessor` over one `Accessibility.getFullAXTree` result.

    Nodes are the raw AX dicts. ``geometry`` maps ``backendDOMNodeId`` ->
    ``(x, y, w, h)`` (document CSS pixels); ``secure_ids`` is the set of backend
    ids that are password inputs. Both come from `parse_dom_snapshot`.
    """

    def __init__(self, nodes: Sequence[dict], geometry: dict[int, tuple[float, float, float, float]],
                 secure_ids: frozenset[int]) -> None:
        self._by_id = {n["nodeId"]: n for n in nodes}
        self._geometry = geometry
        self._secure_ids = secure_ids

    # -- TreeAccessor -------------------------------------------------------
    def read(self, node: dict) -> RawNode:
        raw_role = node.get("role", {}).get("value") or "generic"
        props = _prop(node)
        backend = node.get("backendDOMNodeId")
        role = _ROLE.get(raw_role, "AXGroup")

        editable_prop = props.get("editable")
        is_secure = backend in self._secure_ids
        if role == "AXTextField":
            if is_secure:
                role = _SECURE_ROLE
            elif props.get("multiline") is True:
                role = "AXTextArea"

        actions: tuple[str, ...] = ("AXPress",) if raw_role in _INTERACTIVE else ()

        box = self._geometry.get(backend) if backend is not None else None
        position = (box[0], box[1]) if box else None
        size = (box[2], box[3]) if box else None

        checked = _checked(props.get("checked"))
        name = node.get("name", {}).get("value") or ""
        value = node.get("value", {}).get("value")
        if value is None and editable_prop and not name:
            value = ""  # a focused-but-empty editable still reads as a field

        return RawNode(
            role=role,
            title=str(name),
            value=value,
            description=str(node.get("description", {}).get("value") or ""),
            enabled=props.get("disabled") is not True,
            focused=props.get("focused") is True,
            position=position,
            size=size,
            actions=actions,
            checked=checked,
            selected=props.get("selected") is True,
            expanded=_tristate(props.get("expanded")),
            stable_id=str(backend) if backend is not None else None,
        )

    def children(self, node: dict) -> Sequence[dict]:
        return [self._by_id[cid] for cid in node.get("childIds", ()) if cid in self._by_id]

    # -- roots --------------------------------------------------------------
    def root(self) -> dict | None:
        """The RootWebArea (the node with no parent), or the first node."""
        for n in self._by_id.values():
            if not n.get("parentId"):
                return n
        return next(iter(self._by_id.values()), None)


def _checked(v: object) -> bool | None:
    if v in ("true", "mixed"):
        return True
    if v == "false":
        return False
    return None


def _tristate(v: object) -> bool | None:
    if v is True or v == "true":
        return True
    if v is False or v == "false":
        return False
    return None


def parse_dom_snapshot(
    snapshot: dict,
) -> tuple[dict[int, tuple[float, float, float, float]], frozenset[int]]:
    """Join `DOMSnapshot.captureSnapshot` into geometry + password-field ids.

    Returns ``(geometry, secure_ids)`` where ``geometry`` maps each laid-out
    node's ``backendNodeId`` to its ``(x, y, w, h)`` box in document CSS pixels,
    and ``secure_ids`` is the set of backend ids for ``<input type=password>``
    (so BrowserDriver never types into a secret). Layout/DOM come as parallel
    index arrays with a shared ``strings`` table; only rendered nodes appear in
    ``layout``, so nodes absent from ``geometry`` are correctly pruned as
    off-layout.
    """
    strings = snapshot.get("strings", [])
    geometry: dict[int, tuple[float, float, float, float]] = {}
    secure: set[int] = set()

    def s(idx: int) -> str:
        return strings[idx] if 0 <= idx < len(strings) else ""

    for doc in snapshot.get("documents", []):
        nodes = doc.get("nodes", {})
        backend_ids = nodes.get("backendNodeId", [])
        layout = doc.get("layout", {})
        node_index = layout.get("nodeIndex", [])
        bounds = layout.get("bounds", [])
        for i, dom_idx in enumerate(node_index):
            if i >= len(bounds) or dom_idx >= len(backend_ids):
                continue
            box = bounds[i]
            if len(box) == 4 and (box[2] > 0 or box[3] > 0):
                geometry[backend_ids[dom_idx]] = (box[0], box[1], box[2], box[3])

        # Password inputs: scan the parallel node attributes for type=password.
        node_names = nodes.get("nodeName", [])
        attributes = nodes.get("attributes", [])
        for dom_idx, attrs in enumerate(attributes):
            if dom_idx >= len(node_names) or dom_idx >= len(backend_ids):
                continue
            if s(node_names[dom_idx]).upper() != "INPUT":
                continue
            for j in range(0, len(attrs) - 1, 2):
                if s(attrs[j]).lower() == "type" and s(attrs[j + 1]).lower() == "password":
                    secure.add(backend_ids[dom_idx])

    return geometry, frozenset(secure)


__all__ = ["CDPAccessor", "parse_dom_snapshot"]
