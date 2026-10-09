"""Out-of-process iframe sessions on a flattened CDP connection.

Chrome does not list a cross-origin iframe in the parent ``Page.getFrameTree``,
and the parent's accessibility tree stops at an empty ``Iframe`` node.
``Target.setAutoAttach`` with ``flatten`` delivers a ``sessionId`` for that
iframe on the page's existing socket. Commands for the inner document pass
that session id; ``DOM.getFrameOwner`` on the parent session still names the
``<iframe>`` element, whose box is the origin for the child's layout.

Session ids die with the socket. The browser driver remembers them for the
life of its page connection (a second ``setAutoAttach`` does not re-emit
targets that are already attached). The Linux AT-SPI walk already exposes
these frames on its own and does not use this module.
"""

from __future__ import annotations

from a11y_computer_use.schema import ComputerUseError, ErrorCode

_AUTO_ATTACH = {
    "autoAttach": True,
    "waitForDebuggerOnStart": False,
    "flatten": True,
}

_CHILD_DOMAINS = ("DOM", "Page", "Runtime", "Accessibility")


def attach_oopif_frames(sess, frames: list[dict], *, limit: int, remembered: dict[str, dict]) -> None:
    """Append cross-origin iframes onto ``frames``. Never raises.

    ``remembered`` maps target id -> ``{session_id, parent_id, url}`` for this
    page connection. Attach events update it. Targets already remembered are
    read again even when this call does not re-emit them.
    """
    try:
        sess.call("Target.setAutoAttach", _AUTO_ATTACH)
    except Exception:
        return
    for event in sess.pop_events("Target.detachedFromTarget"):
        params = event.get("params") or {}
        target_id = params.get("targetId")
        session_id = params.get("sessionId")
        if target_id:
            remembered.pop(target_id, None)
        if session_id:
            for key, info in list(remembered.items()):
                if info.get("session_id") == session_id:
                    remembered.pop(key, None)
    for event in sess.pop_events("Target.attachedToTarget"):
        params = event.get("params") or {}
        info = params.get("targetInfo") or {}
        if info.get("type") != "iframe":
            continue
        target_id = info.get("targetId")
        session_id = params.get("sessionId")
        if not target_id or not session_id:
            continue
        remembered[target_id] = {
            "session_id": session_id,
            "parent_id": info.get("parentFrameId"),
            "url": info.get("url") or "",
        }
    known_ids = {f.get("id") for f in frames}
    pending = [
        (target_id, info) for target_id, info in remembered.items()
        if target_id not in known_ids
    ]
    ordered: list[tuple[str, dict]] = []
    guard = 0
    while pending and guard <= len(pending) + 1:
        guard += 1
        rest = []
        placed = known_ids | {target_id for target_id, _info in ordered}
        for target_id, info in pending:
            parent = info.get("parent_id")
            if parent and parent not in placed:
                rest.append((target_id, info))
            else:
                ordered.append((target_id, info))
        if len(rest) == len(pending):
            ordered.extend(rest)
            break
        pending = rest
    for target_id, info in ordered:
        if len(frames) >= limit:
            break
        session_id = info.get("session_id")
        if not session_id:
            continue
        try:
            frame = _read_iframe(sess, target_id, info, frames)
        except Exception:
            remembered.pop(target_id, None)
            continue
        if frame is None:
            continue
        frames.append(frame)


def _read_iframe(sess, target_id: str, info: dict, frames: list[dict]) -> dict | None:
    session_id = info["session_id"]
    for domain in _CHILD_DOMAINS:
        try:
            sess.call(f"{domain}.enable", session_id=session_id)
        except ComputerUseError as exc:
            if exc.code is not ErrorCode.UNSUPPORTED:
                raise
    nodes = sess.call("Accessibility.getFullAXTree", session_id=session_id).get("nodes", [])
    dom = sess.call("DOMSnapshot.captureSnapshot", {"computedStyles": []}, session_id=session_id)
    parent = next((f for f in frames if f.get("id") == info.get("parent_id")), None)
    parent_session = (parent or {}).get("session_id") or None
    owner = sess.call(
        "DOM.getFrameOwner", {"frameId": target_id}, session_id=parent_session,
    ).get("backendNodeId")
    if not isinstance(owner, int):
        return None
    if any(f.get("owner_backend") == owner and not f.get("session_id") for f in frames):
        # Same-process stitch already owns this iframe element.
        return None
    return {
        "id": target_id,
        "parent_id": info.get("parent_id"),
        "owner_backend": owner,
        "nodes": nodes if isinstance(nodes, list) else [],
        "session_id": session_id,
        "parent_session_id": parent_session or "",
        "dom": dom if isinstance(dom, dict) else {},
        "url": info.get("url") or "",
    }


def place_oopif_offsets(frames: list[dict], raw_geometry: dict, offsets: dict[str, tuple[float, float]]) -> dict[str, dict]:
    """Point each OOPIF document at its iframe element, parent before child.

    Returns frame id -> frame-local geometry (integer backend ids).
    """
    from a11y_computer_use.drivers import _cdp_ax

    local: dict[str, dict] = {}
    for frame in frames:
        dom = frame.get("dom")
        if frame.get("session_id") and isinstance(dom, dict):
            local[frame["id"]] = _cdp_ax.parse_dom_snapshot(dom)[0]
    for frame in frames:
        if not frame.get("session_id"):
            continue
        parent = frame.get("parent_id")
        px, py = offsets.get(parent, (0.0, 0.0))
        owner = frame.get("owner_backend")
        box = None
        if parent in local and isinstance(owner, int):
            box = local[parent].get(owner)
        elif isinstance(owner, int):
            box = raw_geometry.get(owner)
        offsets[frame["id"]] = (px + box[0], py + box[1]) if box else (px, py)
    return local


def merge_oopif_geometry(
    frames: list[dict],
    local: dict[str, dict],
    offsets: dict[str, tuple[float, float]],
    geometry: dict,
    secure: set,
    empty: set,
) -> None:
    """Shift each OOPIF's boxes into the top document and key them by session."""
    from a11y_computer_use.drivers import _cdp_ax

    for frame in frames:
        session = frame.get("session_id")
        boxes = local.get(frame.get("id"))
        dom = frame.get("dom")
        if not session or boxes is None or not isinstance(dom, dict):
            continue
        ox, oy = offsets.get(frame["id"], (0.0, 0.0))
        for backend, (x, y, w, h) in boxes.items():
            geometry[(session, backend)] = (x + ox, y + oy, w, h)
        _geom, child_secure, child_empty = _cdp_ax.parse_dom_snapshot(dom)
        for backend in child_secure:
            secure.add((session, backend))
        for backend in child_empty:
            empty.add((session, backend))

