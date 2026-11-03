"""#1470 ?sib: a menu rendered right after its button. Chromium's first hit
test at the freshly drawn item answers from its hover cache; the real hit
follows. Hermetic: the native hit test is stubbed."""
import json

import pytest

from a11y_computer_use.drivers import _atspi
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Bounds, ComputerUseError, Element, ErrorCode


def _item():
    return Element(ref="e42", role="AXMenuItem", title="Mono", value=None, snapshot_id="s1",
                   bounds=Bounds(display_id=1, x=100, y=200, width=80, height=20))


def _driver(monkeypatch, answers, occluded=None):
    driver = LinuxDriver()
    driver._run = lambda fn: fn()  # type: ignore[method-assign]
    driver._COVER_READ_GAP_S = 0
    calls = []

    def occluder(*a, **k):
        calls.append("occluder")
        if occluded:
            raise occluded

    monkeypatch.setattr(driver, "_refuse_chromium_occluded", occluder)
    seq = iter(answers)

    def hit(h, x, y, trace=None):
        calls.append("hit")
        answer = next(seq)
        if trace is not None:
            trace.append({"hit": {"role": "menu item" if answer else "push button"}})
        return answer

    monkeypatch.setattr(_atspi, "chromium_pointer_target", hit)
    return driver, calls


def test_a_cached_first_miss_then_a_settled_hit_on_the_item_proceeds(monkeypatch):
    driver, calls = _driver(monkeypatch, [False, True, True])
    driver._verify_chromium_pointer_target(_item(), object(), 140, 210)
    assert calls[0] == "occluder" and calls.count("hit") == 3


def test_a_hit_that_never_settles_on_the_item_still_refuses(monkeypatch):
    driver, _ = _driver(monkeypatch, [False, True, False, True, False, True, False])
    with pytest.raises(ComputerUseError) as raised:
        driver._verify_chromium_pointer_target(_item(), object(), 140, 210)
    assert raised.value.detail["reason"] == "hit_test_unverified"


def test_a_hit_on_another_element_refuses(monkeypatch):
    driver, _ = _driver(monkeypatch, [False] * 7)
    with pytest.raises(ComputerUseError) as raised:
        driver._verify_chromium_pointer_target(_item(), object(), 140, 210)
    assert raised.value.detail["reason"] == "hit_test_unverified"


def test_the_open_menu_rule_still_decides_before_any_hit_test(monkeypatch):
    blocked = ComputerUseError(ErrorCode.STALE_REF, "covered", detail={"reason": "occluded_target"})
    driver, calls = _driver(monkeypatch, [True, True], occluded=blocked)
    with pytest.raises(ComputerUseError) as raised:
        driver._verify_chromium_pointer_target(_item(), object(), 140, 210)
    assert raised.value.detail["reason"] == "occluded_target" and "hit" not in calls


def test_debug_flag_dumps_every_read(monkeypatch, capsys):
    monkeypatch.setenv("A11Y_DEBUG_HIT_TEST", "1")
    driver, _ = _driver(monkeypatch, [False, True, True])
    driver._verify_chromium_pointer_target(_item(), object(), 140, 210)
    line = next(l for l in capsys.readouterr().err.splitlines() if l.startswith("hit_test_debug: "))
    data = json.loads(line.split(": ", 1)[1])
    assert data["verified"] is True and [r["answer"] for r in data["reads"]] == [False, True, True]
    assert data["reads"][0]["walk"][0]["hit"]["role"] == "push button"


def test_no_debug_output_without_the_flag(monkeypatch, capsys):
    monkeypatch.delenv("A11Y_DEBUG_HIT_TEST", raising=False)
    driver, _ = _driver(monkeypatch, [True])
    driver._verify_chromium_pointer_target(_item(), object(), 140, 210)
    assert "hit_test_debug" not in capsys.readouterr().err


def test_a_non_menu_target_is_not_re_asked(monkeypatch):
    driver, calls = _driver(monkeypatch, [False, True, True])
    button = Element(ref="e7", role="AXButton", title="Font", value=None, snapshot_id="s1",
                     bounds=Bounds(display_id=1, x=100, y=170, width=80, height=20))
    with pytest.raises(ComputerUseError):
        driver._verify_chromium_pointer_target(button, object(), 140, 180)
    assert calls.count("hit") == 1
