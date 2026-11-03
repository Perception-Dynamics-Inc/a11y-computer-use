"""#1481: a held ref to a toast's Undo button after the toast expired.

The refusal must say the status message is gone, name no unrelated control
(the issue's 'Resend code'), offer no candidates and no find() advice.
"""
import pytest

from a11y_computer_use import observe
from a11y_computer_use.schema import ComputerUseError, ErrorCode
from tests.fixtures.trees import ax, button
from tests.test_observe import by_title, snap_of


def page(toast, resend="Resend code in 6s"):
    kids = [
        ax("AXList", at=(10.0, 60.0), size=(400.0, 40.0), children=[
            ax("AXGroup", at=(10.0, 60.0), size=(400.0, 30.0), children=[
                button("Hide message", (200.0, 60.0))])]),
        ax("AXGroup", at=(10.0, 110.0), size=(400.0, 30.0), children=[
            button("Archive", (10.0, 110.0)), button(resend, (100.0, 110.0))]),
    ]
    if toast:
        kids.append(ax("AXStatusBar", at=(10.0, 400.0), size=(300.0, 30.0), children=[
            ax("AXStaticText", title="Message hidden", at=(10.0, 400.0), size=(100.0, 20.0)),
            button("Undo (1)", (120.0, 400.0))]))
    return ax("AXWindow", title="Toast Lab", at=(0.0, 0.0), size=(1000.0, 700.0),
              children=[ax("AXGroup", at=(0.0, 0.0), size=(1000.0, 700.0), children=kids)])


def test_ref_in_an_expired_toast_says_it_is_gone_and_names_nothing_else():
    old = snap_of(page(True))
    live = snap_of(page(False, resend="Resend code"))
    target = by_title(old, "Undo (1)")
    with pytest.raises(ComputerUseError) as caught:
        observe.rematch_ref(old, target.ref, live)
    err = caught.value
    message = str(err)
    assert err.code is ErrorCode.STALE_REF
    assert err.detail["reason"] == "hidden_ancestor"
    assert err.detail["candidates"] == []
    assert err.detail["next"] == ["snapshot"]
    assert err.detail["container"]["title"] == "Message hidden"
    assert err.detail["container"]["state"] == "dismissed"
    assert target.ref in message and "Undo (1)" in message and "no longer shown" in message
    for wrong in ("Resend", "Archive", "reordered", "find(", "scroll_to_find"):
        assert wrong not in message


def test_ref_in_a_shown_toast_still_resolves():
    old = snap_of(page(True))
    live = snap_of(page(True))
    target = by_title(old, "Undo (1)")
    assert observe.rematch_ref(old, target.ref, live).title == "Undo (1)"


def test_a_ref_outside_any_status_region_keeps_the_generic_refusal():
    old = snap_of(page(True))
    live = snap_of(page(True, resend="Resend code"))
    with pytest.raises(ComputerUseError) as caught:
        observe.rematch_ref(old, by_title(old, "Resend code in 6s").ref, live)
    assert caught.value.detail["reason"] != "hidden_ancestor"
