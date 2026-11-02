"""#1463: a popover over the target. No coordinate advice; Escape, then the ref.

Tester's repro: an open ``auto`` popover ('Actions popover') sits over the
"Underlay save" button. ``click(ref)`` is refused (correct), but suggested
``coordinates``/``foreground``; a click at the button's centre (639, 441)
landed on the popover's padding and was reported as a plain click.
"""

from __future__ import annotations

import pytest

from a11y_computer_use import outcome
from a11y_computer_use.schema import Bounds, ComputerUseError, ErrorCode
from tests.test_action_outcomes import TreeDriver, _element, _runtime

POPOVER = Bounds(0, 460, 285, 360, 230)
UNDER = Bounds(0, 559, 421, 161, 40)


def _popover_page(open_: bool = True):
    tree = [
        _element("e1", "AXWindow", "Popover page", path=("AXWindow",), bounds=Bounds(0, 0, 0, 1280, 800)),
        _element("doc", "AXWebArea", "Popover page", parent="e1", path=("AXWindow", "AXWebArea"),
                 bounds=Bounds(0, 0, 80, 1280, 720)),
        _element("opener", "AXButton", "Open actions", parent="doc", clickable=True,
                 path=("AXWindow", "AXWebArea", "AXButton"), bounds=Bounds(0, 8, 90, 110, 24)),
        _element("e32", "AXButton", "Underlay save", parent="doc", clickable=True,
                 path=("AXWindow", "AXWebArea", "AXButton"), bounds=UNDER),
    ]
    if open_:
        tree += [
            _element("pop", "AXPopover", "Actions popover", parent="doc",
                     path=("AXWindow", "AXWebArea", "AXPopover"), bounds=POPOVER),
            _element("rename", "AXButton", "Rename item", parent="pop", clickable=True,
                     path=("AXWindow", "AXWebArea", "AXPopover", "AXButton"),
                     bounds=Bounds(0, 480, 340, 110, 28)),
            _element("close", "AXButton", "Close actions", parent="pop", clickable=True,
                     path=("AXWindow", "AXWebArea", "AXPopover", "AXButton"),
                     bounds=Bounds(0, 600, 340, 110, 28)),
        ]
    return tree


def _driver(hit=None, mutate=False):
    driver = TreeDriver()
    driver.elements = _popover_page()
    sent = []

    def click(target, **kwargs):
        sent.append(target)
        if mutate:  # the popover takes focus: the tree changes, the target does not
            driver._replace("rename", focused=True)

    driver.click = click  # type: ignore[method-assign]
    driver.press_element = lambda element: False  # type: ignore[method-assign]
    if hit is not None:
        driver.hit_bounds_at_point = lambda point: hit  # type: ignore[attr-defined]
    return driver, sent


def test_ref_under_a_popover_suggests_escape_then_the_ref_not_coordinates(tmp_path):
    driver, sent = _driver()
    runtime = _runtime(tmp_path, driver)
    with pytest.raises(ComputerUseError) as caught:
        runtime.click("e32")
    err = caught.value
    assert err.code == ErrorCode.NOT_VISIBLE
    assert err.detail["reason"] == "covered"
    assert "covered by popover 'Actions popover'" in err.message
    assert "press Escape to close the popover, then click the ref again" in err.message
    assert err.detail["recovery"] == {"key": "Escape", "then": "click e32"}
    advice = outcome.next_for_error(err.code.value, err.detail)
    assert advice == ("keyboard", "ref")
    assert "coordinates" not in advice and "foreground" not in advice
    refused = outcome.refused_result(text=err.message, code=err.code.value,
                                     message=err.message, detail=err.detail)
    assert "coordinates" not in refused.next
    assert sent == []


@pytest.mark.parametrize("mutate", [False, True], ids=["tree_unchanged", "popover_changed"])
@pytest.mark.parametrize("hit", [POPOVER, None], ids=["hit_test_popover", "no_hit_test"])
def test_coordinate_click_on_the_popover_is_not_confirmed(tmp_path, hit, mutate):
    driver, sent = _driver(hit=hit, mutate=mutate)
    runtime = _runtime(tmp_path, driver)
    result = runtime.click(x=639, y=441)
    assert len(sent) == 1  # the caller's click is still sent
    assert result.outcome != "confirmed"
    assert result.outcome in {"suspected_noop", "unverifiable"}
    assert "landed on popover 'Actions popover', not on e32" in result.evidence
    assert "press Escape" in result
    assert result.next == ("keyboard", "ref")


def test_coordinate_click_the_hit_test_puts_on_the_target_is_not_flagged(tmp_path):
    driver, _sent = _driver(hit=Bounds(0, 570, 430, 100, 20))  # inside "Underlay save"
    runtime = _runtime(tmp_path, driver)
    result = runtime.click(x=639, y=441)
    assert "landed on popover" not in result.evidence
    assert "landed on popover" not in result


def test_coordinate_click_inside_a_popover_control_is_not_flagged(tmp_path):
    driver, _sent = _driver(hit=Bounds(0, 480, 340, 110, 28))
    runtime = _runtime(tmp_path, driver)
    result = runtime.click(x=500, y=350)  # "Rename item": nothing beneath it but the page
    assert "landed on popover" not in result


def test_escape_closes_the_popover_and_the_ref_click_is_sent(tmp_path):
    driver, sent = _driver()
    runtime = _runtime(tmp_path, driver)
    with pytest.raises(ComputerUseError):
        runtime.click("e32")
    driver.elements = _popover_page(open_=False)  # Escape closed it
    runtime.desktop_snapshot(runtime._current.app)
    runtime.click("e32")
    assert [getattr(t, "ref", None) for t in sent] == ["e32"]
