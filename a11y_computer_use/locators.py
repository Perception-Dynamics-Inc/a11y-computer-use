"""Durable locators beside snapshot refs.

A locator is the same object a skill replay stores for a step: ``role``,
``name``, an optional ``stable_id``, the role ``path``, and the ``ref`` that
issued it. The ref is a hint for one snapshot. The selector string is the
path of role, name, and stable id from the root, so the same control can be
found after a re-snapshot gives it a new ref.

``render_with_locators`` appends that selector to each element line.
``resolve_locator`` and ``apply_selector`` turn a selector or a locator
dict back into the live ref. A stored point is not a locator and is not
used when a selector is present.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from a11y_computer_use.observe import render_text
from a11y_computer_use.schema import ComputerUseError, Element, ErrorCode, Snapshot

_REF_LINE = re.compile(r"^(\s*)(e\d+)\b")
_SEGMENT = re.compile(
    r'^(?P<role>.+?):"(?P<name>(?:\\.|[^"\\])*)"(@(?P<id>[\w.-]+|"(?:\\.|[^"\\])*"))?$'
)
_ROLE_ALIASES = {
    "push button": "button",
    "pushbutton": "button",
    "text field": "textfield",
    "static text": "statictext",
}


@dataclass(frozen=True)
class _Segment:
    role: str
    name: str
    stable_id: str | None


def locator_for(element: Element, snap: Snapshot) -> dict:
    """The replay locator for ``element``, plus its selector string.

    ``path`` is the role path ``Element.path`` already carries. ``selector``
    is the same path with each ancestor's name and stable id, which is what
    a later snapshot matches. ``ref`` is only the id inside ``snap``.
    """
    locator: dict = {
        "role": element.role,
        "name": element.title or "",
        "ref": element.ref,
        "path": list(element.path) if element.path else [item.role for item in _chain(element, snap)],
        "selector": format_selector(element, snap),
    }
    if element.stable_id:
        locator["stable_id"] = element.stable_id
    return locator


def format_selector(element: Element, snap: Snapshot) -> str:
    """``AXDialog:"Save As" >> AXButton:"Save"@save-main`` for ``element``."""
    return " >> ".join(_format_segment(item) for item in _chain(element, snap))


def render_with_locators(snap: Snapshot, **kwargs: object) -> str:
    """``render_text`` with a ``locator=`` selector after every element ref.

    The header and elision markers are unchanged. Existing ``render_text``
    output is unchanged because this is a separate call.
    """
    text = render_text(snap, **kwargs)  # type: ignore[arg-type]
    by_ref = {element.ref: element for element in snap.elements}
    lines = []
    for line in text.split("\n"):
        match = _REF_LINE.match(line)
        element = by_ref.get(match.group(2)) if match else None
        if element is None:
            lines.append(line)
            continue
        lines.append(f"{line}  locator={format_selector(element, snap)}")
    return "\n".join(lines)


def resolve_locator(snap: Snapshot, locator: dict | str) -> Element:
    """The live element for a locator dict or a selector string.

    A dict is the replay shape. ``stable_id`` wins when it still has the
    same role and name. Otherwise the role, name, and role path must match
    one element. A ref that still names that control is that element. A ref
    that names something else is not used. When several controls share the
    role path, the selector string's name path picks one. Zero matches and
    more than one match raise ``stale_ref``.
    """
    if isinstance(locator, str):
        return _from_selector(snap, locator)
    if not isinstance(locator, dict):
        raise ComputerUseError(ErrorCode.STALE_REF, "locator must be an object or a selector string")
    # The selector is the role/name/stable_id path. It is authoritative when
    # it was emitted with the dict, so a reused ref cannot win over it.
    selector = locator.get("selector")
    if isinstance(selector, str) and selector.strip():
        return _from_selector(snap, selector)
    found = _resolve_dict(snap, locator)
    if isinstance(found, Element):
        return found
    raise found


def apply_selector(args: dict, snap: Snapshot) -> dict:
    """Copy ``args`` with ``selector`` resolved to the live ``ref``.

    ``x`` and ``y`` are dropped when a selector is present, so a stored
    point cannot replace the located control. Arguments without a selector
    are returned unchanged.
    """
    if not isinstance(args, dict):
        raise ComputerUseError(ErrorCode.STALE_REF, "action arguments must be an object")
    selector = args.get("selector")
    if selector is None and not isinstance(args.get("locator"), dict):
        return dict(args)
    locator = selector if selector is not None else args["locator"]
    element = resolve_locator(snap, locator)
    if _is_opaque(element.role):
        raise ComputerUseError(
            ErrorCode.STALE_REF,
            "opaque target has no durable control; refusing stored coordinates",
        )
    bound = {
        key: value
        for key, value in args.items()
        if key not in {"selector", "locator", "x", "y", "display_id", "ref"}
    }
    bound["ref"] = element.ref
    return bound


def parse_selector(selector: str) -> list[_Segment]:
    """Split a selector into root-to-leaf segments."""
    text = selector.strip()
    if not text:
        raise ComputerUseError(ErrorCode.STALE_REF, "selector is empty")
    parts = _split_selector(text)
    segments = []
    for part in parts:
        match = _SEGMENT.match(part.strip())
        if match is None:
            raise ComputerUseError(ErrorCode.STALE_REF, f"selector segment {part!r} is not role:\"name\"")
        stable = match.group("id")
        if stable and stable.startswith('"'):
            stable = _unescape(stable[1:-1])
        segments.append(_Segment(
            role=match.group("role").strip(),
            name=_unescape(match.group("name")),
            stable_id=stable or None,
        ))
    return segments


def _resolve_dict(snap: Snapshot, locator: dict) -> Element | ComputerUseError:
    role = str(locator.get("role") or "")
    name = str(locator.get("name") or "")
    stable = str(locator.get("stable_id") or "")
    recorded_ref = str(locator.get("ref") or "")
    path = locator.get("path")
    if _is_opaque(role):
        return ComputerUseError(
            ErrorCode.STALE_REF,
            "opaque target has no durable control; refusing stored coordinates",
        )
    if stable:
        hits = [element for element in snap.elements if element.stable_id == stable]
        if len(hits) > 1:
            return ComputerUseError(ErrorCode.STALE_REF, "stable_id matched more than one element")
        if len(hits) == 1:
            element = hits[0]
            if role and not _roles_match(element.role, role):
                return ComputerUseError(ErrorCode.STALE_REF, "stable_id role changed")
            if name and _exact_name(element.title) != _exact_name(name):
                return ComputerUseError(ErrorCode.STALE_REF, "stable_id name changed")
            return element
    if not role or not name:
        return ComputerUseError(ErrorCode.STALE_REF, "no durable locator; refusing stored coordinates")
    found = [
        element for element in snap.elements
        if _roles_match(element.role, role) and _exact_name(element.title) == _exact_name(name)
    ]
    if recorded_ref:
        live = _by_ref(snap, recorded_ref)
        if live is not None and _roles_match(live.role, role) and _exact_name(live.title) == _exact_name(name):
            if any(element.ref == recorded_ref for element in found):
                return live
            return ComputerUseError(ErrorCode.STALE_REF, "recorded ref is no longer that control")
        if live is not None:
            found = [element for element in found if element.ref != recorded_ref]
    if isinstance(path, list) and path:
        pathed = [element for element in found if list(element.path) == [str(item) for item in path]]
        if len(pathed) == 1:
            return pathed[0]
        if not pathed:
            return ComputerUseError(ErrorCode.STALE_REF, "locator path no longer matches")
        found = pathed
    if len(found) == 1:
        return found[0]
    if not found:
        return ComputerUseError(ErrorCode.STALE_REF, "no element matches the locator")
    return ComputerUseError(ErrorCode.STALE_REF, "locator matched more than one element")


def _match_chain(snap: Snapshot, segments: list[_Segment]) -> list[Element]:
    children: dict[str | None, list[Element]] = {}
    for element in snap.elements:
        children.setdefault(element.parent, []).append(element)

    def walk(parent: str | None, index: int) -> list[Element]:
        found: list[Element] = []
        for element in children.get(parent, []):
            if not _segment_matches(element, segments[index]):
                continue
            if index == len(segments) - 1:
                found.append(element)
            else:
                found.extend(walk(element.ref, index + 1))
        return found

    return walk(None, 0)


def _segment_matches(element: Element, segment: _Segment) -> bool:
    if not _roles_match(element.role, segment.role):
        return False
    if _exact_name(element.title) != _exact_name(segment.name):
        return False
    if segment.stable_id and element.stable_id != segment.stable_id:
        return False
    return True


def _from_selector(snap: Snapshot, selector: str) -> Element:
    return _unique(snap, _match_chain(snap, parse_selector(selector)), selector)


def _unique(snap: Snapshot, found: list[Element], source: object) -> Element:
    del snap
    if len(found) == 1:
        if _is_opaque(found[0].role):
            raise ComputerUseError(
                ErrorCode.STALE_REF,
                "opaque target has no durable control; refusing stored coordinates",
            )
        return found[0]
    if not found:
        raise ComputerUseError(ErrorCode.STALE_REF, f"no element matches the locator {source!r}")
    raise ComputerUseError(ErrorCode.STALE_REF, "locator matched more than one element")


def _chain(element: Element, snap: Snapshot) -> list[Element]:
    by_ref = {item.ref: item for item in snap.elements}
    chain: list[Element] = []
    current: Element | None = element
    seen: set[str] = set()
    while current is not None and current.ref not in seen:
        seen.add(current.ref)
        chain.append(current)
        parent_ref = current.parent
        current = by_ref.get(parent_ref) if parent_ref else None
    chain.reverse()
    return chain


def _format_segment(element: Element) -> str:
    text = f'{element.role}:"{_escape(element.title or "")}"'
    stable = element.stable_id or ""
    if not stable:
        return text
    if re.fullmatch(r"[\w.-]+", stable):
        return text + "@" + stable
    return text + '@"' + _escape(stable) + '"'


def _split_selector(text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    quoted = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif not quoted and text.startswith(">>", index):
            parts.append(text[start:index])
            index += 2
            start = index
            continue
        index += 1
    parts.append(text[start:])
    return [part.strip() for part in parts if part.strip()]


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _unescape(text: str) -> str:
    out = []
    escaped = False
    for char in text:
        if escaped:
            out.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            out.append(char)
    if escaped:
        out.append("\\")
    return "".join(out)


def _by_ref(snap: Snapshot, ref: str) -> Element | None:
    for element in snap.elements:
        if element.ref == ref:
            return element
    return None


def _exact_name(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def _roles_match(actual: str, wanted: str) -> bool:
    return _role_key(actual) == _role_key(wanted)


def _role_key(role: str) -> str:
    text = " ".join(str(role or "").split()).casefold()
    if text.startswith("ax"):
        text = text[2:]
    return _ROLE_ALIASES.get(text, text)


def _is_opaque(role: str) -> bool:
    key = _role_key(role)
    return key in {"opaque_region", "canvas", "opaqueregion"}


__all__ = [
    "apply_selector",
    "format_selector",
    "locator_for",
    "parse_selector",
    "render_with_locators",
    "resolve_locator",
]
