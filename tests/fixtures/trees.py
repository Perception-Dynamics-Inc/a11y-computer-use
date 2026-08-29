"""Dict-backed synthetic a11y trees standing in for live AXUIElement handles.

Nodes are plain dicts (`ax` builds them); `DictAccessor` adapts them to the
`computeruse.observe.TreeAccessor` protocol so `build_snapshot` can walk them
without any TCC grant. Geometry is authored in the same global *point* space
the AX APIs use; `GEOMETRY` is one 2x display so tests also exercise the
point -> physical-pixel projection.
"""

from __future__ import annotations

from collections.abc import Sequence

from computeruse.observe import DisplayGeometry, RawNode
from computeruse.schema import Display

#: One Retina display: 1440x900 points at scale 2.0 -> 2880x1800 physical px.
DISPLAY = Display(display_id=1, width=2880, height=1800, scale=2.0, is_main=True)
GEOMETRY = (DisplayGeometry(display=DISPLAY, origin=(0.0, 0.0)),)


def ax(
    role: str,
    *,
    at: tuple[float, float] | None = None,
    size: tuple[float, float] | None = None,
    children: Sequence[dict] = (),
    **attrs: object,
) -> dict:
    """Build one synthetic node; ``attrs`` maps straight onto `RawNode` fields."""
    return {"role": role, "at": at, "size": size, "children": list(children), **attrs}


def button(title: str, at: tuple[float, float], *, enabled: bool = True) -> dict:
    return ax("AXButton", title=title, at=at, size=(80.0, 30.0), actions=("AXPress",), enabled=enabled)


class DictAccessor:
    """`TreeAccessor` over `ax`-built dict nodes."""

    def read(self, node: object) -> RawNode:
        assert isinstance(node, dict)
        return RawNode(
            role=node["role"],
            subrole=node.get("subrole"),
            title=node.get("title", ""),
            value=node.get("value"),
            description=node.get("description", ""),
            enabled=node.get("enabled", True),
            focused=node.get("focused", False),
            position=node.get("at"),
            size=node.get("size"),
            actions=tuple(node.get("actions", ())),
            checked=node.get("checked"),
            selected=node.get("selected", False),
            expanded=node.get("expanded"),
            placeholder=node.get("placeholder", ""),
            stable_id=node.get("stable_id"),
        )

    def children(self, node: object) -> Sequence[object]:
        assert isinstance(node, dict)
        return node["children"]


def typical_app_window() -> dict:
    """A realistic mid-size app window plus the noise the pruner must eat.

    Contains: a toolbar of 8 buttons; a decorative (dropped) and a described
    (kept) image; a zero-size node; a fully-offscreen and a half-offscreen
    button; a group>scrollarea wrapper chain around the text editor
    (collapsed); a 40-row sidebar list with 2 interactive rows (fan-out cap);
    a secure password field; a disabled button; an empty static text.
    """
    toolbar_buttons = [
        button(title, (110.0 + 90.0 * i, 57.0))
        for i, title in enumerate(
            ["Bold", "Italic", "Underline", "Bigger", "Smaller", "Left", "Center", "Right"]
        )
    ]
    toolbar = ax(
        "AXToolbar",
        at=(100.0, 50.0),
        size=(1200.0, 44.0),
        children=[
            *toolbar_buttons,
            ax("AXImage", at=(1000.0, 57.0), size=(24.0, 24.0)),  # decorative: dropped
            ax("AXImage", at=(1030.0, 57.0), size=(24.0, 24.0), description="Sync status"),
            ax("AXGroup", at=(110.0, 57.0), size=(0.0, 0.0)),  # zero-size: dropped
            button("Ghost", (3000.0, 57.0)),  # fully offscreen: dropped
        ],
    )
    editor = ax(
        "AXGroup",
        at=(100.0, 100.0),
        size=(900.0, 700.0),
        children=[
            ax(
                "AXScrollArea",
                at=(100.0, 100.0),
                size=(900.0, 700.0),
                children=[
                    ax(
                        "AXTextArea",
                        title="Document body",
                        value="hello world",
                        at=(100.0, 100.0),
                        size=(900.0, 700.0),
                        focused=True,
                    )
                ],
            )
        ],
    )
    rows: list[dict] = [
        ax("AXStaticText", value=f"Note {i}", at=(1015.0, 105.0 + 17.0 * i), size=(260.0, 15.0))
        for i in range(38)
    ]
    rows.insert(5, button("Pin note", (1015.0, 190.0)))
    rows.append(button("Load more", (1015.0, 790.0)))
    sidebar = ax("AXList", at=(1010.0, 100.0), size=(280.0, 700.0), children=rows)
    return ax(
        "AXWindow",
        title="Untitled",
        at=(100.0, 50.0),
        size=(1200.0, 800.0),
        children=[
            toolbar,
            editor,
            sidebar,
            ax(
                "AXSecureTextField",
                title="Password",
                value="hunter2",  # must never surface in the snapshot
                at=(100.0, 810.0),
                size=(300.0, 30.0),
                actions=("AXConfirm",),
            ),
            button("Publish", (420.0, 810.0), enabled=False),
            button("Half off", (1400.0, 400.0)),  # partially visible: kept
            ax("AXStaticText", value="", at=(600.0, 810.0), size=(100.0, 15.0)),  # dropped
        ],
    )


def deep_chain(levels: int) -> dict:
    """A window over ``levels`` nested titled groups ending in a button.

    Titled groups are never collapsed, so the chain's pruned depth equals its
    raw depth — exercising the depth cap deterministically.
    """
    node = button("Bottom", (120.0, 120.0))
    for i in range(levels, 0, -1):
        node = ax(
            "AXGroup",
            title=f"level {i}",
            at=(100.0 + i, 100.0),
            size=(600.0, 400.0),
            children=[node],
        )
    return ax("AXWindow", title="Deep", at=(100.0, 50.0), size=(800.0, 600.0), children=[node])
