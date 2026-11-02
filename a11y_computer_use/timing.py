"""DEBUG-only per-call timing spans (instrumentation only, no behaviour change).

One line per tool call on the ``a11y_computer_use.timing`` logger::

    timing tool=click ref=e14 total=1.214s resolve=0.821s capture=0.050s gate=0.260s conclude=0.070s unaccounted=0.013s nested=gate/driver:0.201s,conclude/judge:0.060s marks=first_click:0.905s nodes=resolve:412,conclude:388

* ``total`` is the public entry (``Runtime.click`` / ``type_text`` / ...) to exit.
* The top-level spans are disjoint, so ``total`` = their sum + ``unaccounted``.
* ``nested`` lists sub-spans as ``parent/child:seconds``. They are already
  inside their parent and are not added again.
* ``marks`` gives the time since the call started at which an event first
  happened. ``nodes`` gives the snapshot elements built, keyed by the top-level
  span they were built in.

When DEBUG is off, ``call`` does one ``isEnabledFor`` check and ``span`` /
``mark`` / ``count_nodes`` do one global read. Nothing is recorded. Only
``time.perf_counter`` is used.
"""

from __future__ import annotations

import logging
import time
from functools import wraps

LOG = logging.getLogger("a11y_computer_use.timing")

# The one recorder for the call in progress. Runtime calls are serialized and
# the driver's a11y worker thread runs only while the caller is blocked, so a
# global (not a ContextVar) also sees spans that run on that thread.
_active: "_Recorder | None" = None


class _Recorder:
    __slots__ = ("start", "stack", "spans", "marks", "nodes")

    def __init__(self) -> None:
        self.start = time.perf_counter()
        self.stack: list[str] = []
        self.spans: dict[str, float] = {}
        self.marks: dict[str, float] = {}
        self.nodes: dict[str, int] = {}

    def line(self, tool: str, ref, total: float) -> str:
        top = {k: v for k, v in self.spans.items() if "/" not in k}
        nested = {k: v for k, v in self.spans.items() if "/" in k}
        parts = [f"timing tool={tool} ref={ref if ref is not None else '-'} total={total:.3f}s"]
        parts += [f"{k}={v:.3f}s" for k, v in top.items()]
        parts.append(f"unaccounted={total - sum(top.values()):.3f}s")
        parts.append("nested=" + (",".join(f"{k}:{v:.3f}s" for k, v in nested.items()) or "-"))
        parts.append("marks=" + (",".join(f"{k}:{v:.3f}s" for k, v in self.marks.items()) or "-"))
        parts.append("nodes=" + (",".join(f"{k}:{v}" for k, v in self.nodes.items()) or "-"))
        return " ".join(parts)


def call(tool: str, ref_pos: int | None = None):
    """Decorate a public Runtime entry point. A call nested in another call
    becomes a span of the outer one, so each tool call logs one line."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            global _active
            if _active is not None:
                return _span_run(tool, fn, args, kwargs)
            if not LOG.isEnabledFor(logging.DEBUG):
                return fn(*args, **kwargs)
            rec = _active = _Recorder()
            try:
                return fn(*args, **kwargs)
            finally:
                total = time.perf_counter() - rec.start
                _active = None
                ref = kwargs.get("ref")
                if ref is None and ref_pos is not None and len(args) > ref_pos:
                    ref = args[ref_pos]
                LOG.debug(rec.line(tool, ref, total))
        return wrapper
    return deco


def _span_run(name, fn, args, kwargs):
    rec = _active
    if rec is None:
        return fn(*args, **kwargs)
    if name in rec.stack:  # re-entrant (e.g. _target -> _resolve): outermost counts
        return fn(*args, **kwargs)
    key = "/".join([*rec.stack, name])
    rec.stack.append(name)
    start = time.perf_counter()
    try:
        return fn(*args, **kwargs)
    finally:
        rec.spans[key] = rec.spans.get(key, 0.0) + time.perf_counter() - start
        rec.stack.pop()


def span(name: str):
    """Decorate a method whose duration is one named span of the active call."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if _active is None:
                return fn(*args, **kwargs)
            return _span_run(name, fn, args, kwargs)
        return wrapper
    return deco


def snapshot_span(fn):
    """Decorate a driver ``snapshot``: a ``snapshot`` span plus its element count."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if _active is None:
            return fn(*args, **kwargs)
        result = _span_run("snapshot", fn, args, kwargs)
        elements = getattr(result, "elements", None)
        if elements is not None:
            count_nodes(len(elements))
        return result
    return wrapper


def mark(name: str) -> None:
    """Record the first time ``name`` happens in the active call."""
    rec = _active
    if rec is not None and name not in rec.marks:
        rec.marks[name] = time.perf_counter() - rec.start


def count_nodes(n: int) -> None:
    """Add ``n`` snapshot elements to the current top-level span's count."""
    rec = _active
    if rec is not None:
        key = rec.stack[0] if rec.stack else "-"
        rec.nodes[key] = rec.nodes.get(key, 0) + n
