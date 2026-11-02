"""DEBUG per-call timing spans: one line per call, phases + unaccounted == total."""

from __future__ import annotations

import logging
import re

from a11y_computer_use import timing

LOGGER = "a11y_computer_use.timing"
LINE = re.compile(
    r"^timing tool=(?P<tool>\S+) ref=(?P<ref>\S+) total=(?P<total>\d+\.\d{3})s"
    r"(?P<top>(?: [a-z_]+=\d+\.\d{3}s)*) unaccounted=(?P<un>-?\d+\.\d{3})s"
    r" nested=(?P<nested>\S+) marks=(?P<marks>\S+) nodes=(?P<nodes>\S+)$"
)


class _Snap:
    def __init__(self, n):
        self.elements = tuple(range(n))


class _Fake:
    @timing.call("click", ref_pos=1)
    def click(self, ref=None):
        self.resolve()
        self.capture()
        self.gate()
        self.conclude()
        return "ok"

    @timing.call("set_value", ref_pos=1)
    def set_value(self, ref, value):
        return self.click(ref=ref)  # nested call: a span, not a second line

    @timing.span("resolve")
    def resolve(self):
        self.resolve_inner()
        self.snapshot(40)

    @timing.span("resolve")
    def resolve_inner(self):  # re-entrant: counted once, in the outer span
        _busy(0.002)

    @timing.snapshot_span
    def snapshot(self, n):
        _busy(0.001)
        return _Snap(n)

    @timing.span("capture")
    def capture(self):
        _busy(0.002)

    @timing.span("gate")
    def gate(self):
        self.driver()

    @timing.span("driver")
    def driver(self):
        _busy(0.001)
        timing.mark("first_key")
        timing.mark("first_key")
        _busy(0.001)

    @timing.span("conclude")
    def conclude(self):
        self.judge()
        self.snapshot(7)

    @timing.span("judge")
    def judge(self):
        _busy(0.001)


def _busy(seconds):
    import time
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        pass


def _lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


def test_one_line_per_call_and_spans_sum_to_total(caplog) -> None:
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert _Fake().click(ref="e14") == "ok"
    lines = _lines(caplog)
    assert len(lines) == 1, lines
    m = LINE.match(lines[0])
    assert m, lines[0]
    assert m["tool"] == "click" and m["ref"] == "e14"
    top = dict(re.findall(r" ([a-z_]+)=(\d+\.\d{3})s", m["top"]))
    assert list(top) == ["resolve", "capture", "gate", "conclude"]
    total = float(m["total"])
    assert abs(sum(map(float, top.values())) + float(m["un"]) - total) <= 0.003
    assert float(m["un"]) >= -0.003
    nested = dict(p.rsplit(":", 1) for p in m["nested"].split(","))
    assert set(nested) == {"resolve/snapshot", "gate/driver", "conclude/judge", "conclude/snapshot"}
    assert "resolve/resolve" not in m["nested"]
    assert float(nested["gate/driver"][:-1]) <= float(top["gate"])
    marks = dict(p.rsplit(":", 1) for p in m["marks"].split(","))
    assert list(marks) == ["first_key"] and float(marks["first_key"][:-1]) <= total
    assert m["nodes"] == "resolve:40,conclude:7"


def test_nested_public_call_is_a_span_and_positional_ref_is_logged(caplog) -> None:
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        _Fake().set_value("e3", "x")
    lines = _lines(caplog)
    assert len(lines) == 1, lines
    m = LINE.match(lines[0])
    assert m and m["tool"] == "set_value" and m["ref"] == "e3", lines[0]
    assert re.fullmatch(r" click=\d+\.\d{3}s", m["top"]), m["top"]


def test_nothing_recorded_or_logged_when_debug_is_off(caplog, monkeypatch) -> None:
    def no_recorder():
        raise AssertionError("a recorder was built with DEBUG off")

    monkeypatch.setattr(timing, "_Recorder", no_recorder)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert _Fake().click(ref="e1") == "ok"
    assert _lines(caplog) == []
    assert timing._active is None


def test_exception_still_logs_and_clears(caplog) -> None:
    class Boom(_Fake):
        @timing.span("capture")
        def capture(self):
            raise RuntimeError("x")

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        try:
            Boom().click(ref="e9")
        except RuntimeError:
            pass
    lines = _lines(caplog)
    assert len(lines) == 1 and LINE.match(lines[0]), lines
    assert timing._active is None
