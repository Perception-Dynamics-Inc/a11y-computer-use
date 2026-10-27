"""Chrome's own UI reports DIP bounds at a device scale factor above 1 (#1208).

Chromium on Linux exposes its web content in device pixels but its Views UI
(tab strip, toolbar, menus, the frame itself) in device-independent pixels.
At ``--force-device-scale-factor=1.5`` a tab reports a 224x41 box at 646,0
while the pixel it occupies is 969,0. Every ref click on browser UI landed
short and still read as confirmed.

The factor is measured, never assumed: a top-level web document is in device
pixels and sits inside a Views container that is in DIP, so their widths give
the factor. Without such a pair the snapshot is returned unchanged, which
keeps GTK, Firefox and a Chrome at scale 1 exactly as they were.
"""
from __future__ import annotations

from dataclasses import replace

from a11y_computer_use.schema import Bounds, Element, Snapshot

#: Factors Chromium is run at. A measured ratio snaps to the nearest one when
#: it is within ``_SNAP_TOLERANCE`` (a scrollbar trims the document a little).
_STEP = 0.25
_SNAP_TOLERANCE = 0.04
_MIN_FACTOR = 1.25
_MAX_FACTOR = 4.0


def _is_document(element: Element) -> bool:
    identity = element.context_identity
    return bool(identity) and str(identity[0]).startswith("document")


def device_scale(snap: Snapshot) -> float:
    """The DIP-to-pixel factor of this snapshot's Chromium UI; 1.0 when unknown."""
    by_ref = {el.ref: el for el in snap.elements}
    factors: list[float] = []
    for element in snap.elements:
        if not _is_document(element) or element.bounds is None or element.bounds.width <= 0:
            continue
        # Top-level documents only: a frame inside a page is in pixels too.
        if _inside_document(element, by_ref):
            continue
        parent = by_ref.get(element.parent) if element.parent else None
        while parent is not None and (parent.bounds is None or parent.bounds.width <= 0):
            parent = by_ref.get(parent.parent) if parent.parent else None
        if parent is None or parent.bounds is None:
            continue
        ratio = element.bounds.width / parent.bounds.width
        snapped = round(ratio / _STEP) * _STEP
        if (_MIN_FACTOR <= snapped <= _MAX_FACTOR
                and abs(ratio - snapped) <= snapped * _SNAP_TOLERANCE):
            factors.append(snapped)
    if not factors or len(set(factors)) != 1:
        # No document, or documents that disagree: do not guess.
        return 1.0
    return factors[0]


def _inside_document(element: Element, by_ref: dict[str, Element]) -> bool:
    seen = {element.ref}
    parent = by_ref.get(element.parent) if element.parent else None
    while parent is not None and parent.ref not in seen:
        if _is_document(parent):
            return True
        seen.add(parent.ref)
        parent = by_ref.get(parent.parent) if parent.parent else None
    return False


def scale_browser_ui(snap: Snapshot) -> Snapshot:
    """``snap`` with every non-web element's bounds multiplied into device pixels."""
    factor = device_scale(snap)
    if factor == 1.0:
        return snap
    by_ref = {el.ref: el for el in snap.elements}

    def scaled(element: Element) -> Element:
        box = element.bounds
        if box is None or _is_document(element) or _inside_document(element, by_ref):
            return element
        return replace(element, bounds=Bounds(
            box.display_id,
            round(box.x * factor),
            round(box.y * factor),
            round(box.width * factor),
            round(box.height * factor),
        ))

    return replace(snap, elements=tuple(scaled(el) for el in snap.elements))
