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
which gives `observe._match_anchor` a deterministic re-resolution key. A node
from an out-of-process iframe is ``"<session>:<backend>"`` so it does not
collide with a parent node that reused the same backend id.
"""

from __future__ import annotations

from collections.abc import Sequence

from a11y_computer_use.observe import RawNode

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
    "canvas": "AXCanvas",
    "Canvas": "AXCanvas",
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

#: Controls inside a paragraph are not spliced into the paragraph's sentence.
#: A button paragraph stays empty and collapses onto the button. A text field
#: keeps its own name instead of being copied into the surrounding words.
_INLINE_SKIP = frozenset({
    "textbox", "searchbox", "button", "checkbox", "switch", "radio",
    "combobox", "listbox", "ListBox", "slider", "spinbutton", "option",
    "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "treeitem",
    "disclosuretriangle",
})

#: Pieces of a div that is one inline sentence. Anything else (a paragraph,
#: a heading, a control) means the div is layout and keeps no joined value.
_INLINE_PIECE = frozenset({
    "StaticText", "InlineTextBox", "text", "link",
    "emphasis", "strong", "mark", "span", "generic", "group",
})


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
                 secure_ids: frozenset[int], empty_number_ids: frozenset[int] = frozenset()) -> None:
        self._by_id = {n["nodeId"]: n for n in nodes}
        self._geometry = geometry
        self._secure_ids = secure_ids
        self._empty_number_ids = empty_number_ids

    # -- TreeAccessor -------------------------------------------------------
    def read(self, node: dict) -> RawNode:
        raw_role = node.get("role", {}).get("value") or "generic"
        props = _prop(node)
        backend = node.get("backendDOMNodeId")
        session = node.get("_sessionId") or ""
        role = _ROLE.get(raw_role, "AXGroup")

        editable_prop = props.get("editable")
        is_secure = _marked(self._secure_ids, backend, session)
        if role == "AXTextField":
            if is_secure:
                role = _SECURE_ROLE
            elif props.get("multiline") is True:
                role = "AXTextArea"

        actions: tuple[str, ...] = ("AXPress",) if raw_role in _INTERACTIVE else ()

        box = _box(self._geometry, backend, session)
        position = (box[0], box[1]) if box else None
        size = (box[2], box[3]) if box else None

        checked = _checked(props.get("checked"))
        # aria-pressed is a separate AX property from checked. A pressed toggle
        # shows as checked, the same as a switch. False stays unchecked.
        if checked is None and "pressed" in props:
            checked = _checked(props.get("pressed"))
        name = node.get("name", {}).get("value") or ""
        if isinstance(name, str) and "\ufffc" in name:
            name = name.replace("\ufffc", "").strip()
        value = node.get("value", {}).get("value")
        if isinstance(value, str) and "\ufffc" in value:
            value = value.replace("\ufffc", "").strip() or None
        if raw_role == "paragraph":
            sentence = self._inline_sentence(node).replace("\u00a0", " ").strip()
            if sentence:
                value = sentence
        elif raw_role in {"generic", "group", "section"} and not value:
            # A div is generic. Its words are child static texts and links,
            # the same fragments a paragraph used to be. A container that
            # also holds a control or a block stays valueless, so a button
            # wrapper still collapses onto the button.
            sentence = self._flat_inline_sentence(node)
            if sentence:
                value = sentence
        if value in (None, "") and raw_role in {"combobox", "listbox", "ListBox"}:
            selected = self._selected_option_text(node)
            if selected:
                value = selected
        if value is None and editable_prop and not name:
            value = ""  # a focused-but-empty editable still reads as a field
        if (
            _marked(self._empty_number_ids, backend, session)
            and value in (0, 0.0, "0", "0.0", "0.00")
        ):
            # An empty <input type=number> has no DOM value. Chrome's AX value
            # is the spin button's numeric default, not a value the user set.
            value = None

        if isinstance(backend, int) and session:
            # Backend ids restart in each OOPIF session. A bare id would
            # collide with a node in the parent document.
            stable = f"{session}:{backend}"
        else:
            stable = str(backend) if isinstance(backend, int) else None
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
            stable_id=stable,
        )

    def children(self, node: dict) -> Sequence[dict]:
        return [self._by_id[cid] for cid in node.get("childIds", ()) if cid in self._by_id]

    def _inline_sentence(self, node: dict, depth: int = 0) -> str:
        """The paragraph text with each inline child's words in place.

        Static text and link names are the words. Emphasis and other
        non-controls are walked. A text field, button, or checkbox is left
        out so ``<p><label>Bravo <input></label></p>`` does not read
        "Bravo Bravo" and a paragraph that is only a button stays empty.
        """
        if depth > 6:
            return ""
        parts: list[str] = []
        for child in self.children(node):
            role = child.get("role", {}).get("value") or "generic"
            if role in _INLINE_SKIP:
                continue
            name = child.get("name", {}).get("value") or ""
            if not isinstance(name, str):
                name = ""
            name = name.replace("\ufffc", "")
            if role in {"StaticText", "InlineTextBox", "text", "link"}:
                if name:
                    parts.append(name)
                else:
                    parts.append(self._inline_sentence(child, depth + 1))
                continue
            parts.append(self._inline_sentence(child, depth + 1))
        return "".join(parts)

    def _flat_inline_sentence(self, node: dict, depth: int = 0) -> str | None:
        """The joined text of a div whose children are only inline pieces.

        Static text, links, emphasis, and a span (itself a generic) count.
        A button, a field, a paragraph, or any other block means this node
        is layout, and it gets no sentence of its own. ``None`` is that
        case, and also a node with no inline words.
        """
        if depth > 6:
            return None
        children = list(self.children(node))
        if not children:
            return None
        parts: list[str] = []
        for child in children:
            role = child.get("role", {}).get("value") or "generic"
            if role in _INLINE_SKIP or role not in _INLINE_PIECE:
                return None
            if role in {"StaticText", "InlineTextBox", "text", "link"}:
                name = child.get("name", {}).get("value") or ""
                if not isinstance(name, str):
                    name = ""
                name = name.replace("\ufffc", "")
                if name:
                    parts.append(name)
                    continue
                inner = self._flat_inline_sentence(child, depth + 1)
                if not inner:
                    return None
                parts.append(inner)
                continue
            inner = self._flat_inline_sentence(child, depth + 1)
            if not inner:
                return None
            parts.append(inner)
        sentence = "".join(parts).replace("\u00a0", " ").strip()
        return sentence or None

    def _selected_option_text(self, node: dict) -> str | None:
        labels: list[str] = []

        def walk(current: dict, depth: int) -> None:
            if depth > 4:
                return
            for child_id in current.get("childIds", ()):
                child = self._by_id.get(child_id)
                if child is None:
                    continue
                props = _prop(child)
                if props.get("selected") is True:
                    label = str(child.get("name", {}).get("value") or "")
                    if label and label not in labels:
                        labels.append(label)
                    continue
                walk(child, depth + 1)

        walk(node, 0)
        if not labels:
            return None
        return ", ".join(labels)

    # -- roots --------------------------------------------------------------
    def root(self) -> dict | None:
        """The RootWebArea (the node with no parent), or the first node."""
        for n in self._by_id.values():
            if not n.get("parentId"):
                return n
        return next(iter(self._by_id.values()), None)


def _box(geometry: dict, backend: object, session: str):
    """Layout box for ``backend``.

    The main session is keyed by the integer backend id, which is what every
    same-process snapshot stores. An OOPIF node is keyed by
    ``(session_id, backend)`` because those ids are not unique across targets.
    A session node does not fall back to the integer: that box belongs to a
    different document.
    """
    if not isinstance(backend, int):
        return None
    if session:
        return geometry.get((session, backend))
    return geometry.get(backend)


def _marked(ids: frozenset, backend: object, session: str) -> bool:
    if not isinstance(backend, int):
        return False
    if session:
        return (session, backend) in ids
    return backend in ids


def _checked(v: object) -> bool | None:
    # like the boolean tristate, but a checkbox's "mixed" (indeterminate) is truthy
    return True if v == "mixed" else _tristate(v)


def _tristate(v: object) -> bool | None:
    if v is True or v == "true":
        return True
    if v is False or v == "false":
        return False
    return None


def parse_dom_snapshot(
    snapshot: dict,
    frame_offsets: dict[str, tuple[float, float]] | None = None,
) -> tuple[dict[int, tuple[float, float, float, float]], frozenset[int], frozenset[int]]:
    """Join `DOMSnapshot.captureSnapshot` into geometry + password-field ids.

    Returns ``(geometry, secure_ids, empty_number_ids)`` where ``geometry`` maps
    each laid-out node's ``backendNodeId`` to its ``(x, y, w, h)`` box,
    ``secure_ids`` is the set of backend ids for ``<input type=password>`` (so
    BrowserDriver never types into a secret), and ``empty_number_ids`` is the
    set of ``<input type=number>`` nodes whose value attribute is empty.
    Layout/DOM come as parallel index arrays with a shared ``strings`` table;
    only rendered nodes appear in ``layout``, so nodes absent from ``geometry``
    are correctly pruned as off-layout.

    One ``document`` is returned per frame; ``frame_offsets`` (frame id ->
    ``(dx, dy)``, from `build_frame_offsets`) shifts each frame's boxes into the
    top document's coordinate space so an iframe's content sits where it renders.
    Absent/None -> frame-local (identity) coordinates.
    """
    strings = snapshot.get("strings", [])
    offsets = frame_offsets or {}
    geometry: dict[int, tuple[float, float, float, float]] = {}
    secure: set[int] = set()
    empty_numbers: set[int] = set()

    def s(idx: object) -> str:
        return strings[idx] if isinstance(idx, int) and 0 <= idx < len(strings) else ""

    for doc in snapshot.get("documents", []):
        dx, dy = offsets.get(s(doc.get("frameId")), (0.0, 0.0))
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
                geometry[backend_ids[dom_idx]] = (box[0] + dx, box[1] + dy, box[2], box[3])

        # Password inputs: scan the parallel node attributes for type=password.
        node_names = nodes.get("nodeName", [])
        attributes = nodes.get("attributes", [])
        for dom_idx, attrs in enumerate(attributes):
            if dom_idx >= len(node_names) or dom_idx >= len(backend_ids):
                continue
            if s(node_names[dom_idx]).upper() != "INPUT":
                continue
            fields: dict[str, str] = {}
            for j in range(0, len(attrs) - 1, 2):
                fields[s(attrs[j]).lower()] = s(attrs[j + 1])
            if fields.get("type", "").lower() == "password":
                secure.add(backend_ids[dom_idx])
            if fields.get("type", "").lower() == "number" and fields.get("value", "") == "":
                empty_numbers.add(backend_ids[dom_idx])

    return geometry, frozenset(secure), frozenset(empty_numbers)


def build_frame_offsets(
    raw_geometry: dict[int, tuple[float, float, float, float]],
    frames: list[dict],
) -> dict[str, tuple[float, float]]:
    """Absolute pixel offset for each frame's document, top-down.

    ``raw_geometry`` is the frame-LOCAL geometry (``parse_dom_snapshot`` with no
    offsets); ``frames`` is the flattened frame tree in parent-before-child order,
    each ``{"id", "parent_id", "owner_backend"}`` (``owner_backend`` is the
    ``<iframe>`` element's backend id, from ``DOM.getFrameOwner``; None for the
    main frame). A child's offset is its parent's offset plus the owner iframe's
    (frame-local) position — so nested frames compose correctly.
    """
    offsets: dict[str, tuple[float, float]] = {}
    for f in frames:
        owner = f.get("owner_backend")
        if owner is None:  # main frame
            offsets[f["id"]] = (0.0, 0.0)
            continue
        px, py = offsets.get(f.get("parent_id"), (0.0, 0.0))
        parent_session = f.get("parent_session_id") or ""
        if parent_session:
            box = raw_geometry.get((parent_session, owner))
        else:
            box = raw_geometry.get(owner)
        offsets[f["id"]] = (px + box[0], py + box[1]) if box else (px, py)
    return offsets


def stitch_frames(frame_nodes: list[dict]) -> list[dict]:
    """Splice per-frame AX trees into one tree the accessor can walk.

    ``getFullAXTree`` stops at an ``Iframe`` node (empty ``childIds``); each child
    frame's tree is fetched separately. This namespaces every frame's node ids
    (``"<frame index>:<node id>"``) so they never collide, then grafts each child
    frame's root under the ``Iframe`` node whose ``backendDOMNodeId`` matches the
    frame's owner element. Input is one dict per frame, in tree order:
    ``{"nodes": [...], "owner_backend": int|None}`` (None = the main frame).
    """
    if len(frame_nodes) == 1:  # no child frames (the common case): nothing to graft
        return frame_nodes[0]["nodes"]  # ids can't collide, so skip namespacing/copy
    pooled: list[dict] = []
    # (session id, backend id) -> node. OOPIF backend ids restart at 1, so a
    # shared int map would let a child node replace the parent <iframe> before
    # the graft looks it up.
    by_backend: dict[tuple[str, int], dict] = {}
    for fi, frame in enumerate(frame_nodes):
        session = str(frame.get("session_id") or "")
        parent_session = str(frame.get("parent_session_id") or "")
        prefix = f"{fi}:"
        owner = frame.get("owner_backend")
        # Resolve the host before this frame's own nodes are indexed. A child
        # backend id can equal the owner iframe's id when the child is an OOPIF.
        host = by_backend.get((parent_session, owner)) if isinstance(owner, int) else None
        root = None
        for n in frame["nodes"]:
            m = dict(n)
            m["nodeId"] = prefix + str(n["nodeId"])
            if n.get("parentId"):
                m["parentId"] = prefix + str(n["parentId"])
            m["childIds"] = [prefix + str(c) for c in n.get("childIds", ())]
            if session:
                m["_sessionId"] = session
            pooled.append(m)
            if not n.get("parentId"):
                root = m
            backend = n.get("backendDOMNodeId")
            if isinstance(backend, int):
                by_backend[(session, backend)] = m
        if root is not None and host is not None:
            host.setdefault("childIds", []).append(root["nodeId"])
            root["parentId"] = host["nodeId"]
    return pooled


__all__ = ["CDPAccessor", "parse_dom_snapshot", "build_frame_offsets", "stitch_frames"]
