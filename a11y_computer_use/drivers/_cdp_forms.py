"""DOM form controls for a Chrome or Electron window that already exposes CDP.

The AT-SPI snapshot stays the tree: windows, menus, native dialogs, and
every on-screen box. When that process was started with
``--remote-debugging-port``, form-control values and checked state are
read and written through the page DOM and merged onto the matching
elements. Refs and bounds are unchanged. A missing port leaves the
AT-SPI snapshot and the AT-SPI write path as they were. A missing
optional websocket-client dependency also keeps that native path. This module
does not launch a browser and does not add debugging flags.

Geometry from the DOM is ignored. A control the snapshot does not
already contain is not added.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, replace

from a11y_computer_use.observe import _MAX_VALUE_CHARS
from a11y_computer_use.schema import ComputerUseError, Element, ErrorCode, Snapshot

_KINDS = frozenset({"text", "number", "select", "listbox", "checkbox", "radio"})
_KIND_ROLES = {
    "text": frozenset({"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"}),
    "number": frozenset({"AXTextField", "AXTextArea", "AXSpinButton"}),
    "select": frozenset({"AXComboBox", "AXPopUpButton", "AXList"}),
    "listbox": frozenset({"AXList", "AXListBox", "AXComboBox"}),
    "checkbox": frozenset({"AXCheckBox"}),
    "radio": frozenset({"AXRadioButton"}),
}
_FORM_ROLES = frozenset(role for roles in _KIND_ROLES.values() for role in roles)
_OPTION_ROLES = frozenset({"AXRow", "AXListItem", "AXMenuItem", "AXRadioButton"})
_CHECK_WORDS = {"true": True, "false": False, "on": True, "off": False, "1": True, "0": False}
_PORT_FLAG = re.compile(rb"(?:^|\x00)--remote-debugging-port(?:=([0-9]+))?(?=\x00|$)")
_USER_DATA = re.compile(rb"(?:^|\x00)--user-data-dir=([^\x00]*)")
_INSTANCE_PID = re.compile(r"^pid:([1-9][0-9]*)\|")

_READ_JS = r"""
(() => {
  const labelOf = (el) => {
    const aria = el.getAttribute && el.getAttribute('aria-label');
    if (aria) return aria;
    if (el.id && el.ownerDocument) {
      const lab = el.ownerDocument.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lab) return (lab.innerText || '').trim();
    }
    const wrap = el.closest && el.closest('label');
    if (wrap) {
      const clone = wrap.cloneNode(true);
      clone.querySelectorAll('input,select,textarea,button').forEach((node) => node.remove());
      const text = (clone.innerText || '').trim();
      if (text) return text;
    }
    return (el.getAttribute && el.getAttribute('name')) || '';
  };
  const kindOf = (el) => {
    const tag = String(el.tagName || '').toUpperCase();
    const type = String((el.getAttribute && el.getAttribute('type')) || '').toLowerCase();
    const role = String((el.getAttribute && el.getAttribute('role')) || '').toLowerCase();
    if (tag === 'INPUT' && (type === 'password' || type === 'hidden' || type === 'file')) return null;
    if (tag === 'TEXTAREA') return 'text';
    if (tag === 'INPUT' && (type === '' || type === 'text' || type === 'search' || type === 'email' || type === 'url' || type === 'tel')) return 'text';
    if (tag === 'INPUT' && type === 'number') return 'number';
    if (tag === 'INPUT' && type === 'checkbox') return 'checkbox';
    if (tag === 'INPUT' && type === 'radio') return 'radio';
    if (tag === 'SELECT') {
      const size = Number(el.size || 0);
      if (el.multiple || size > 1) return 'listbox';
      return 'select';
    }
    if (role === 'listbox') return 'listbox';
    return null;
  };
  const selectedOf = (el, kind) => {
    const selected = [];
    const options = [];
    if (String(el.tagName || '').toUpperCase() === 'SELECT') {
      for (const opt of el.options) {
        const label = String(opt.label || opt.text || '').trim();
        options.push(label);
        if (opt.selected) selected.push(label);
      }
      return {selected, options};
    }
    if (kind === 'listbox') {
      const root = el.ownerDocument || document;
      const nodes = el.querySelectorAll('[role="option"]');
      for (const opt of nodes) {
        const label = String(opt.getAttribute('aria-label') || opt.innerText || '').trim();
        options.push(label);
        const flag = String(opt.getAttribute('aria-selected') || '').toLowerCase();
        if (flag === 'true') selected.push(label);
      }
    }
    return {selected, options};
  };
  const controls = [];
  const visit = (root) => {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('input, textarea, select, [role="listbox"]').forEach((el) => {
      const kind = kindOf(el);
      if (!kind) return;
      const picked = selectedOf(el, kind);
      const checked = (kind === 'checkbox' || kind === 'radio') ? el.checked === true : null;
      const value = (kind === 'checkbox' || kind === 'radio') ? null : (el.value == null ? null : String(el.value));
      controls.push({
        kind: kind,
        id: el.id ? String(el.id) : '',
        name: String(labelOf(el) || ''),
        value: value,
        checked: checked,
        selected: picked.selected,
        options: picked.options,
        min: el.getAttribute ? el.getAttribute('min') : null,
        max: el.getAttribute ? el.getAttribute('max') : null
      });
    });
    root.querySelectorAll('iframe').forEach((frame) => {
      try {
        if (frame.contentDocument) visit(frame.contentDocument);
      } catch (err) {}
    });
  };
  visit(document);
  return controls;
})()
"""

_WRITE_JS = r"""
((spec) => {
  const labelOf = (el) => {
    const aria = el.getAttribute && el.getAttribute('aria-label');
    if (aria) return aria;
    if (el.id && el.ownerDocument) {
      const lab = el.ownerDocument.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lab) return (lab.innerText || '').trim();
    }
    const wrap = el.closest && el.closest('label');
    if (wrap) {
      const clone = wrap.cloneNode(true);
      clone.querySelectorAll('input,select,textarea,button').forEach((node) => node.remove());
      const text = (clone.innerText || '').trim();
      if (text) return text;
    }
    return (el.getAttribute && el.getAttribute('name')) || '';
  };
  const kindOf = (el) => {
    const tag = String(el.tagName || '').toUpperCase();
    const type = String((el.getAttribute && el.getAttribute('type')) || '').toLowerCase();
    const role = String((el.getAttribute && el.getAttribute('role')) || '').toLowerCase();
    if (tag === 'INPUT' && (type === 'password' || type === 'hidden' || type === 'file')) return null;
    if (tag === 'TEXTAREA') return 'text';
    if (tag === 'INPUT' && (type === '' || type === 'text' || type === 'search' || type === 'email' || type === 'url' || type === 'tel')) return 'text';
    if (tag === 'INPUT' && type === 'number') return 'number';
    if (tag === 'INPUT' && type === 'checkbox') return 'checkbox';
    if (tag === 'INPUT' && type === 'radio') return 'radio';
    if (tag === 'SELECT') {
      const size = Number(el.size || 0);
      if (el.multiple || size > 1) return 'listbox';
      return 'select';
    }
    if (role === 'listbox') return 'listbox';
    return null;
  };
  const all = [];
  const visit = (root) => {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('input, textarea, select, [role="listbox"]').forEach((el) => all.push(el));
    root.querySelectorAll('iframe').forEach((frame) => {
      try { if (frame.contentDocument) visit(frame.contentDocument); } catch (err) {}
    });
  };
  visit(document);
  const hits = all.filter((el) => {
    const kind = kindOf(el);
    if (!kind) return false;
    if (spec.id) return String(el.id || '') === spec.id;
    return kind === spec.kind && spec.name && labelOf(el) === spec.name;
  });
  if (hits.length !== 1) return {found: false, ambiguous: hits.length > 1};
  const el = hits[0];
  const kind = kindOf(el);
  const hint = spec.kind;
  const compatible = kind === hint
    || (hint === 'text' && kind === 'number')
    || (hint === 'select' && kind === 'listbox')
    || (hint === 'listbox' && kind === 'select');
  if (!compatible) return {found: false};
  if (spec.action === 'locate') return {found: true, wrote: false};
  const optionLabels = () => {
    if (String(el.tagName || '').toUpperCase() !== 'SELECT') return [];
    return [...el.options].map((opt) => String(opt.label || opt.text || '').trim());
  };
  const fire = () => {
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };
  const mark = () => {
    el.__a11yCuDom = 1;
    el.setAttribute('data-a11y-cu-dom', '1');
  };
  if (spec.action === 'toggle') {
    if (kind === 'radio') el.checked = true;
    else el.checked = !el.checked;
    fire();
    mark();
    return {found: true, wrote: true, checked: el.checked === true, value: null, options: []};
  }
  if (kind === 'checkbox' || kind === 'radio') {
    if (typeof spec.checked !== 'boolean') return {found: true, wrote: false, reason: 'bad_checked'};
    el.checked = spec.checked;
    fire();
    mark();
    return {found: true, wrote: el.checked === spec.checked, checked: el.checked === true, value: null, options: []};
  }
  if (kind === 'number') {
    const minAttr = el.getAttribute('min');
    const maxAttr = el.getAttribute('max');
    if (spec.value === '') {
      el.value = '';
      fire();
      mark();
      return {found: true, wrote: el.value === '', value: String(el.value), checked: null, options: [], min: minAttr, max: maxAttr};
    }
    const number = Number(spec.value);
    if (!Number.isFinite(number)) {
      return {found: true, wrote: false, reason: 'not_a_number', min: minAttr, max: maxAttr, options: []};
    }
    if (minAttr !== null && minAttr !== '' && Number.isFinite(Number(minAttr)) && number < Number(minAttr)) {
      return {found: true, wrote: false, reason: 'out_of_range', min: minAttr, max: maxAttr, options: []};
    }
    if (maxAttr !== null && maxAttr !== '' && Number.isFinite(Number(maxAttr)) && number > Number(maxAttr)) {
      return {found: true, wrote: false, reason: 'out_of_range', min: minAttr, max: maxAttr, options: []};
    }
    el.value = spec.value;
    fire();
    mark();
    return {found: true, wrote: true, kind: 'number', value: String(el.value), checked: null, options: [], min: minAttr, max: maxAttr};
  }
  if (kind === 'select' || kind === 'listbox') {
    const labels = optionLabels();
    if (String(el.tagName || '').toUpperCase() === 'SELECT') {
      const index = labels.indexOf(spec.value);
      if (index < 0) return {found: true, wrote: false, reason: 'not_an_option', options: labels};
      for (let i = 0; i < el.options.length; i += 1) el.options[i].selected = i === index;
      fire();
      mark();
      const chosen = [...el.selectedOptions].map((opt) => String(opt.label || opt.text || '').trim());
      return {found: true, wrote: chosen.length === 1 && chosen[0] === spec.value, value: chosen.join(', '), options: labels};
    }
    return {found: true, wrote: false, reason: 'not_an_option', options: []};
  }
  el.value = spec.value;
  fire();
  mark();
  return {found: true, wrote: String(el.value) === spec.value, value: String(el.value), checked: null, options: []};
})
"""


@dataclass(frozen=True, slots=True)
class FormControl:
    """One DOM form control. There is no box: the snapshot already has one."""

    kind: str
    dom_id: str
    name: str
    value: str | None
    checked: bool | None
    selected: tuple[str, ...]
    options: tuple[str, ...]
    minimum: float | None = None
    maximum: float | None = None

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"kind must be one of {sorted(_KINDS)}")
        if not isinstance(self.dom_id, str) or not isinstance(self.name, str):
            raise TypeError("dom_id and name must be strings")
        if self.value is not None and not isinstance(self.value, str):
            raise TypeError("value must be a string or None")
        if self.checked is not None and type(self.checked) is not bool:
            raise TypeError("checked must be True, False, or None")
        if not isinstance(self.selected, tuple) or not isinstance(self.options, tuple):
            raise TypeError("selected and options must be tuples of strings")
        if any(not isinstance(item, str) for item in self.selected + self.options):
            raise TypeError("selected and options must be tuples of strings")
        for bound in (self.minimum, self.maximum):
            if bound is None:
                continue
            if isinstance(bound, bool) or not isinstance(bound, (int, float)) or not math.isfinite(bound):
                raise ValueError("minimum and maximum must be finite numbers or None")


def _argv_cmdline(raw: bytes) -> bytes:
    """NUL-separated arguments, including a title Chrome rewrote with spaces.

    ``/proc/<pid>/cmdline`` is NUL-separated. Chrome replaces those NULs
    with spaces when it sets the process title, so a NUL split never sees
    ``--remote-debugging-port``. A buffer with no interior NUL is that
    title: split it on spaces and put the NULs back. A buffer that still
    has an interior NUL is the kernel's argv and is left alone, because an
    argument can contain a space.
    """
    body = raw.rstrip(b"\x00")
    if b"\x00" in body or not body:
        return raw
    return b"\x00".join(body.split(b" ")) + b"\x00"


def debug_port_from_cmdline(raw: object) -> int | None:
    """The DevTools port on this process command line, or None.

    The flag has to be present. Chrome keeps the last value when the
    flag is repeated, which is the port that actually listens. Port ``0``
    is the ephemeral port Chrome writes into ``DevToolsActivePort`` under
    the last ``--user-data-dir``. A process that was not started with the
    flag is not a CDP target, even when a default port is open on the
    machine.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("cmdline must be bytes")
    blob = _argv_cmdline(bytes(raw))
    flags = list(_PORT_FLAG.finditer(blob))
    if not flags:
        return None
    number = flags[-1].group(1)
    if number is not None:
        port = int(number)
        if 1 <= port <= 65535:
            return port
        if port != 0:
            return None
    users = list(_USER_DATA.finditer(blob))
    if not users:
        return None
    folder = os.fsdecode(users[-1].group(1))
    return _port_file(folder)


def port_from_process_chain(cmdlines: object) -> int | None:
    """The first cmdline that opted in, process first and ancestors after.

    A renderer or zygote often has no ``--remote-debugging-port``. The
    browser process that started it does. An ancestor is not a guess when
    the process itself has no flag.
    """
    if isinstance(cmdlines, (bytes, bytearray)) or not isinstance(cmdlines, tuple):
        raise TypeError("cmdlines must be a tuple of bytes")
    for raw in cmdlines:
        if not isinstance(raw, (bytes, bytearray)):
            raise TypeError("cmdlines must be a tuple of bytes")
        port = debug_port_from_cmdline(raw)
        if port is not None:
            return port
    return None


def port_for_pid(pid: object) -> int | None:
    """DevTools port of process ``pid``, or of an ancestor, or None.

    None means this process was not started with the flag and neither was
    an ancestor. A missing flag is not a default port. When several
    processes in the chain name a port, this is the process's own port.
    """
    ports = ports_for_pid(pid)
    return ports[0] if ports else None


def ports_for_pid(pid: object) -> tuple[int, ...]:
    """Distinct DevTools ports on ``pid`` and its ancestors, process first.

    A renderer often copies a port that is not the one the browser bound.
    The browser's port is later in the chain. On one process the profile's
    ``DevToolsActivePort`` is tried before a numeric flag: a launcher can
    leave an earlier port on the command line while the profile bound
    another. Callers try them in order.
    """
    if isinstance(pid, bool) or not isinstance(pid, int):
        raise TypeError("pid must be a positive int")
    if pid <= 0:
        raise ValueError("pid must be a positive int")
    found: list[int] = []
    for raw in _cmdline_chain(pid):
        for port in _ports_on_cmdline(raw):
            if port not in found:
                found.append(port)
    return tuple(found)


def _ports_on_cmdline(raw: bytes) -> tuple[int, ...]:
    """Ports this command line opted into, active profile file first."""
    raw = _argv_cmdline(raw)
    flags = list(_PORT_FLAG.finditer(raw))
    if not flags:
        return ()
    ordered: list[int] = []
    users = list(_USER_DATA.finditer(raw))
    if users:
        active = _port_file(os.fsdecode(users[-1].group(1)))
        if active is not None:
            ordered.append(active)
    number = flags[-1].group(1)
    if number is not None:
        port = int(number)
        if 1 <= port <= 65535 and port not in ordered:
            ordered.append(port)
    return tuple(ordered)


def _cmdline_chain(pid: int) -> tuple[bytes, ...]:
    found: list[bytes] = []
    current = pid
    seen: set[int] = set()
    for _ in range(16):
        if current in seen or current <= 1:
            break
        seen.add(current)
        raw = _read_cmdline(current)
        if raw is not None:
            found.append(raw)
        parent = _parent_pid(current)
        if parent is None:
            break
        current = parent
    return tuple(found)


def _read_cmdline(pid: int) -> bytes | None:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _parent_pid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.startswith("PPid:"):
                    continue
                text = line.split(":", 1)[1].strip()
                if text.isdigit():
                    parent = int(text)
                    return parent if parent > 1 else None
                return None
    except OSError:
        return None
    return None


def pid_from_instance(instance_id: object) -> int | None:
    """The pid recorded on an element, or None when the token has none."""
    if not isinstance(instance_id, str):
        return None
    match = _INSTANCE_PID.match(instance_id)
    if match is None:
        return None
    return int(match.group(1))


def choose_socket(pages: object, window_title: object) -> str | None:
    """The page socket for ``window_title``.

    One page is that page. Several pages use the title the window is
    showing, the same prefix rule as a Chrome window title. No match is
    None: another tab is not this window.
    """
    if not isinstance(pages, list) or any(not isinstance(page, dict) for page in pages):
        raise TypeError("pages must be a list of target objects")
    if not isinstance(window_title, str):
        raise TypeError("window_title must be a string")
    sockets = [str(page["webSocketDebuggerUrl"]) for page in pages if page.get("webSocketDebuggerUrl")]
    if not sockets:
        return None
    if len(pages) == 1:
        return sockets[0]
    matches = title_matches(pages, window_title)
    # Two pages that equally match the window title are two windows (or tabs)
    # showing the same title. Picking one wrote into the wrong window (#1372):
    # no guess, the caller keeps the ref's own accessibility handle.
    return matches[0] if len(matches) == 1 else None


def title_matches(pages: list, window_title: str) -> list[str]:
    """Sockets of the pages whose title best matches ``window_title``.

    The longest matching title wins; every page at that length is returned
    so a caller can tell a unique match from an ambiguous one.
    """
    best_len = -1
    best: list[str] = []
    for page in pages:
        ws = page.get("webSocketDebuggerUrl") if isinstance(page, dict) else None
        title = page.get("title") if isinstance(page, dict) else None
        if not isinstance(ws, str) or not ws or not isinstance(title, str) or not title or not window_title:
            continue
        if window_title != title and not (
            window_title.startswith(title) and window_title[len(title):len(title) + 1] in " -—–"
        ):
            continue
        if len(title) > best_len:
            best_len, best = len(title), [ws]
        elif len(title) == best_len and ws not in best:
            best.append(ws)
    return best


def parse_control(raw: object) -> FormControl | None:
    """One control from a DOM payload. Malformed rows and geometry are dropped."""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    if kind not in _KINDS:
        return None
    dom_id = raw.get("id", "")
    name = raw.get("name", "")
    if not isinstance(dom_id, str) or not isinstance(name, str):
        return None
    value = raw.get("value")
    if value is not None and not isinstance(value, str):
        return None
    checked = raw.get("checked")
    if checked is not None and type(checked) is not bool:
        return None
    selected = _string_tuple(raw.get("selected"))
    options = _string_tuple(raw.get("options"))
    if selected is None or options is None:
        return None
    if isinstance(value, str) and len(value) > _MAX_VALUE_CHARS:
        return None
    try:
        return FormControl(
            kind=kind,
            dom_id=dom_id,
            name=name,
            value=value,
            checked=checked,
            selected=selected,
            options=options,
            minimum=_bound(raw.get("min")),
            maximum=_bound(raw.get("max")),
        )
    except (TypeError, ValueError):
        return None


def apply_form_controls(snapshot: Snapshot, controls: object) -> Snapshot:
    """Copy DOM values onto the matching elements. Bounds and refs stay put.

    A control that matches nothing is not inserted. Two matches for one
    control, or two controls for one element, are left on the AT-SPI
    reading. The same snapshot object is returned when nothing changes.
    """
    if not isinstance(snapshot, Snapshot):
        raise TypeError("snapshot must be a Snapshot")
    if isinstance(controls, (str, bytes)) or not isinstance(controls, tuple):
        raise TypeError("controls must be a tuple of FormControl")
    if any(not isinstance(item, FormControl) for item in controls):
        raise TypeError("controls must be a tuple of FormControl")
    if not controls:
        return snapshot
    mapping = _match(snapshot.elements, controls)
    if not mapping:
        return snapshot
    parents = {el.ref: el for el in snapshot.elements}
    updated: list[Element] = []
    changed = False
    for index, element in enumerate(snapshot.elements):
        current = element
        control = mapping.get(index)
        if control is not None:
            current = _with_control(element, control)
            changed = changed or current is not element
        selected = _option_selected(element, snapshot.elements, parents, mapping)
        if selected is not None and selected is not element.selected:
            current = replace(current, selected=selected)
            changed = True
        updated.append(current)
    if not changed:
        return snapshot
    return replace(snapshot, elements=tuple(updated))


def overlay_snapshot(snapshot: Snapshot) -> Snapshot:
    """Merge DOM form state when this snapshot's process opted into CDP.

    CDP errors leave the AT-SPI snapshot in place. They are not an empty
    tree and they are not a successful DOM read.
    """
    if not isinstance(snapshot, Snapshot):
        raise TypeError("snapshot must be a Snapshot")
    pid = snapshot.pid
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return snapshot
    try:
        port = port_for_pid(pid)
    except OSError:
        return snapshot
    if port is None:
        return snapshot
    try:
        controls = fetch_form_controls(port, _window_title(snapshot))
    except (OSError, TimeoutError, ComputerUseError, ValueError):
        return snapshot
    return apply_form_controls(snapshot, controls)


def fetch_form_controls(port: object, window_title: object) -> tuple[FormControl, ...]:
    """Form controls of the page this window is showing. No boxes."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise TypeError("port must be an int from 1 to 65535")
    if not isinstance(window_title, str):
        raise TypeError("window_title must be a string")
    value = _evaluate(port, window_title, _READ_JS)
    if not isinstance(value, list):
        return ()
    found = []
    for item in value:
        control = parse_control(item)
        if control is not None:
            found.append(control)
    return tuple(found)


def write_element(
    element: Element, value: object, window_title: str = "", pids: tuple[int, ...] = (),
) -> bool | None:
    """Write one form control through the DOM.

    None means this element is not a CDP form write (no port, no unique
    match). The caller keeps the AT-SPI path. True means the DOM read
    back the value. A rejected number or an unknown option raises before
    the node is changed. A write that does not read back raises and is
    not success.
    """
    if not isinstance(element, Element):
        raise TypeError("element must be an Element")
    if isinstance(value, bool) or not isinstance(value, str):
        raise TypeError("value must be a string")
    if element.role not in _FORM_ROLES or element.secure is True or element.enabled is not True:
        return None
    return _commit(element, _write_spec(element, value), window_title, pids)


def press_checkable(
    element: Element, window_title: str = "", pids: tuple[int, ...] = (),
) -> bool | None:
    """Toggle a checkbox, or select a radio, through the DOM.

    None means CDP is not used for this element. True means the DOM
    checked bit is the bit that was requested. A missed read-back raises.
    """
    if not isinstance(element, Element):
        raise TypeError("element must be an Element")
    if element.role not in {"AXCheckBox", "AXRadioButton"}:
        return None
    if element.secure is True or element.enabled is not True:
        return None
    kind = "checkbox" if element.role == "AXCheckBox" else "radio"
    spec = {
        "id": element.stable_id or "",
        "name": element.title,
        "kind": kind,
        "action": "toggle",
    }
    reply = _commit_raw(element, spec, window_title, pids)
    if reply is None:
        return None
    if reply.get("wrote") is True and type(reply.get("checked")) is bool:
        return True
    raise _mismatch(element, "the checked state did not change")


def _write_spec(element: Element, value: str) -> dict:
    kind = _kind_for_element(element)
    spec = {
        "id": element.stable_id or "",
        "name": element.title,
        "kind": kind,
        "action": "set",
        "value": value,
    }
    if kind in {"checkbox", "radio"}:
        if value not in _CHECK_WORDS:
            raise ValueError(f"value {value!r} is not a checked state")
        spec["checked"] = _CHECK_WORDS[value]
        spec["kind"] = kind
    return spec


def _kind_for_element(element: Element) -> str:
    if element.role == "AXCheckBox":
        return "checkbox"
    if element.role == "AXRadioButton":
        return "radio"
    if element.role == "AXSpinButton":
        return "number"
    if element.role in {"AXList", "AXListBox"}:
        return "listbox"
    if element.role in {"AXComboBox", "AXPopUpButton"}:
        return "select"
    return "text"


def _commit(
    element: Element, spec: dict, window_title: str = "", pids: tuple[int, ...] = (),
) -> bool | None:
    reply = _commit_raw(element, spec, window_title, pids)
    if reply is None:
        return None
    _raise_rejected(element, reply, spec)
    if reply.get("wrote") is not True:
        raise _mismatch(element, f"the value read back does not match {spec.get('value')!r}")
    if reply.get("kind") == "number" and spec.get("value") not in ("", None):
        if not _same_number(spec.get("value"), reply.get("value")):
            raise _mismatch(element, f"the value read back does not match {spec.get('value')!r}")
    return True


def _commit_raw(
    element: Element, spec: dict, window_title: str = "", pids: tuple[int, ...] = (),
) -> dict | None:
    if not isinstance(window_title, str):
        raise TypeError("window_title must be a string")
    if isinstance(pids, (str, bytes)) or not isinstance(pids, tuple):
        raise TypeError("pids must be a tuple of ints")
    if any(isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 for pid in pids):
        raise TypeError("pids must be a tuple of positive ints")
    ordered: list[int] = []
    primary = pid_from_instance(element.instance_id)
    if primary is not None:
        ordered.append(primary)
    for pid in pids:
        if pid not in ordered:
            ordered.append(pid)
    if not ordered:
        return None
    ports: list[int] = []
    for pid in ordered:
        try:
            found = ports_for_pid(pid)
        except OSError:
            continue
        for port in found:
            if port not in ports:
                ports.append(port)
    if not ports:
        return None
    expression = f"{_WRITE_JS}({json.dumps(spec)})"
    last_error: BaseException | None = None
    answered = False
    for port in ports:
        try:
            reply = _evaluate(port, window_title, expression)
        except (OSError, TimeoutError, ComputerUseError) as exc:
            last_error = exc
            continue
        answered = True
        if isinstance(reply, dict) and reply.get("found") is True:
            return reply
    if not answered and last_error is not None:
        raise last_error
    return None


def _raise_rejected(element: Element, reply: dict, spec: dict) -> None:
    reason = reply.get("reason")
    if reason == "not_a_number":
        low, high = reply.get("min"), reply.get("max")
        if isinstance(low, str) and low and isinstance(high, str) and high:
            raise ValueError(f"value {spec.get('value')!r} is not a number; valid range is {low}..{high}")
        if isinstance(low, str) and low:
            raise ValueError(f"value {spec.get('value')!r} is not a number; minimum is {low}")
        raise ValueError(f"value {spec.get('value')!r} is not a number")
    if reason == "out_of_range":
        low = reply.get("min") if isinstance(reply.get("min"), str) else ""
        high = reply.get("max") if isinstance(reply.get("max"), str) else ""
        raise ValueError(f"value {spec.get('value')!r} is outside {low}..{high}")
    if reason == "not_an_option":
        options = reply.get("options")
        labels = [item for item in options if isinstance(item, str)] if isinstance(options, list) else []
        raise ValueError(f"value {spec.get('value')!r} is not one of: {', '.join(labels)}")
    if reason == "bad_checked":
        raise ValueError(f"value {spec.get('value')!r} is not a checked state")


def _evaluate(port: int, window_title: str, expression: str) -> object:
    from a11y_computer_use.drivers._cdp import page_targets

    pages = page_targets(f"http://127.0.0.1:{port}")
    locate = _locate_expression(expression)
    if isinstance(pages, list) and len(pages) > 1 and (
        not window_title or len(title_matches(pages, window_title)) > 1
    ):
        # Several pages and no unique title match: the DOM cannot tell which
        # window the ref belongs to. Do not evaluate in any of them, not even
        # the "only page with this control" fallback, which finds the same
        # control in a twin window (#1372). None keeps the AT-SPI handle path.
        return None
    socket = choose_socket(pages, window_title)
    reply = None
    if socket is not None:
        reply = _eval_socket(socket, expression)
        # A title match that does not contain this control is not the tab
        # the snapshot showed. Chrome can publish a UI page whose title is
        # the whole window title, ahead of the document title.
        if locate is None or not _page_missed_control(reply):
            return reply
    if locate is None:
        return reply
    fallback = _only_form_page(pages, locate)
    if fallback is None or fallback == socket:
        return reply
    return _eval_socket(fallback, expression)


def _page_missed_control(reply: object) -> bool:
    """True when a write ran and that page does not have the control.

    An ambiguous match stays on this page. A read result is not a miss.
    """
    if not isinstance(reply, dict):
        return False
    if reply.get("found") is True or reply.get("ambiguous") is True:
        return False
    return True


def _locate_expression(expression: str) -> str | None:
    """The same write script with ``action`` set to ``locate``, or None."""
    for action in ("set", "toggle"):
        needle = f'"action": "{action}"'
        if needle in expression:
            return expression.replace(needle, '"action": "locate"', 1)
    return None


def _only_form_page(pages: list, expression: str | None) -> str | None:
    """The one page socket whose locate result found the control."""
    if expression is None:
        return None
    sockets = [
        str(page["webSocketDebuggerUrl"])
        for page in pages
        if isinstance(page, dict) and page.get("webSocketDebuggerUrl")
    ]
    found: list[str] = []
    last_error: BaseException | None = None
    answered = False
    for socket in sockets:
        try:
            value = _eval_socket(socket, expression)
        except (OSError, TimeoutError, ComputerUseError) as exc:
            last_error = exc
            continue
        answered = True
        if isinstance(value, dict) and value.get("found") is True:
            found.append(socket)
    if len(found) == 1:
        return found[0]
    # One document can show up as two page targets while it loads. Same
    # URL is that document. Two different URLs are not a guess.
    urls: list[str] = []
    for page in pages:
        if not isinstance(page, dict) or page.get("webSocketDebuggerUrl") not in found:
            continue
        url = page.get("url")
        if not isinstance(url, str) or not url:
            urls = []
            break
        urls.append(url)
    if urls and len(urls) == len(found) and len(set(urls)) == 1:
        return found[0]
    if not answered and last_error is not None:
        raise last_error
    return None


def _eval_socket(socket: str, expression: str) -> object:
    from a11y_computer_use.drivers._cdp import CDPSession, connect

    try:
        transport = connect(socket, timeout=3.0)
    except ComputerUseError as exc:
        # No connection or DOM write has happened when the dependency is
        # absent. Other CDP failures must remain errors: native retry could
        # repeat an action whose effect is unknown.
        if (exc.code is ErrorCode.UNSUPPORTED
                and exc.detail.get("reason") == "missing_browser_dependency"):
            return None
        raise
    session = CDPSession(transport, default_timeout=3.0)
    try:
        reply = session.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
        )
    finally:
        transport.close()
    if not isinstance(reply, dict):
        raise ComputerUseError(
            ErrorCode.UNSUPPORTED,
            "CDP did not return a form-control result",
            detail={"reason": "cdp_result"},
        )
    if reply.get("exceptionDetails"):
        raise ComputerUseError(
            ErrorCode.UNSUPPORTED,
            "CDP form-control script failed",
            detail={"reason": "cdp_exception"},
        )
    result = reply.get("result")
    if not isinstance(result, dict):
        return None
    return result.get("value")


def _match(elements: tuple[Element, ...], controls: tuple[FormControl, ...]) -> dict[int, FormControl]:
    by_id: dict[str, list[int]] = {}
    for index, element in enumerate(elements):
        if element.stable_id and element.secure is not True:
            by_id.setdefault(element.stable_id, []).append(index)
    mapping: dict[int, FormControl] = {}
    used: set[int] = set()
    for control in controls:
        if not control.dom_id:
            continue
        hits = [
            index for index in by_id.get(control.dom_id, [])
            if index not in mapping and _role_ok(elements[index], control)
        ]
        if len(hits) == 1:
            mapping[hits[0]] = control
            used.add(id(control))
    claimed = set(mapping)
    for control in controls:
        if id(control) in used or not control.name:
            continue
        hits = [
            index for index, element in enumerate(elements)
            if index not in claimed
            and element.secure is not True
            and element.title == control.name
            and _role_ok(element, control)
        ]
        if len(hits) == 1:
            mapping[hits[0]] = control
            claimed.add(hits[0])
    return mapping


def _role_ok(element: Element, control: FormControl) -> bool:
    return element.role in _KIND_ROLES[control.kind]


def _with_control(element: Element, control: FormControl) -> Element:
    if control.kind in {"checkbox", "radio"}:
        if control.checked is None or control.checked is element.checked:
            return element
        return replace(element, checked=control.checked)
    if control.kind in {"select", "listbox"}:
        shown = ", ".join(label for label in control.selected if label)
        if shown == (element.value or ""):
            return element
        return replace(element, value=shown if shown else None)
    if control.value is None or control.value == element.value:
        return element
    return replace(element, value=control.value)


def _option_selected(element, elements, parents, mapping) -> bool | None:
    if element.role not in _OPTION_ROLES:
        return None
    control = _owning_choice(element, elements, parents, mapping)
    if control is None or not control.options:
        return None
    if element.title not in control.options:
        return None
    return element.title in control.selected


def _owning_choice(element, elements, parents, mapping) -> FormControl | None:
    ref = element.parent
    seen: set[str] = set()
    for _ in range(8):
        if not isinstance(ref, str) or ref in seen:
            return None
        seen.add(ref)
        parent = parents.get(ref)
        if parent is None:
            return None
        matched = _control_on(parent, elements, mapping)
        if matched is not None and matched.kind in {"select", "listbox"}:
            return matched
        ref = parent.parent
    return None


def _control_on(element: Element, elements: tuple[Element, ...], mapping: dict[int, FormControl]) -> FormControl | None:
    for index, control in mapping.items():
        if elements[index] is element:
            return control
    return None


def _window_title(snapshot: Snapshot) -> str:
    for element in snapshot.elements:
        if element.role == "AXWindow" and element.title:
            return element.title
    return ""


def _string_tuple(raw: object) -> tuple[str, ...] | None:
    if raw is None:
        return ()
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        return None
    return tuple(raw)


def _bound(raw: object) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, str):
        return None
    try:
        number = float(raw)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    return number


def _port_file(folder: str) -> int | None:
    try:
        with open(os.path.join(folder, "DevToolsActivePort"), encoding="utf-8") as handle:
            line = handle.readline(32).strip()
    except OSError:
        return None
    if not line.isdigit():
        return None
    port = int(line)
    if 1 <= port <= 65535:
        return port
    return None


def _same_number(requested: object, readback: object) -> bool:
    if not isinstance(requested, str) or not isinstance(readback, str):
        return False
    try:
        return math.isfinite(float(requested)) and float(requested) == float(readback)
    except ValueError:
        return False


def _mismatch(element: Element, message: str) -> ComputerUseError:
    return ComputerUseError(
        ErrorCode.UNSUPPORTED,
        message,
        detail={"ref": element.ref, "role": element.role, "reason": "text_mismatch"},
    )
