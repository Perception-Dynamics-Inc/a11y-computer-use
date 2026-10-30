"""#1369: a ref inside a <details> that has since collapsed.

The refusal must say the target is inside a collapsed section and suggest
expanding it. It must not name a control from another section, suggest
find()/scroll_to_find, or offer any redirect candidate.
"""
import re

import pytest

from a11y_computer_use import observe
from a11y_computer_use.schema import ComputerUseError, ErrorCode
from tests.fixtures.trees import ax, button
from tests.test_observe import by_title, snap_of


def summary(title, at, expanded):
    return ax("AXButton", title=title, at=at, size=(200.0, 20.0),
              actions=("AXPress",), expanded=expanded)


def field(title, at):
    return ax("AXTextField", title=title, at=at, size=(150.0, 20.0), value="")


def details_lab(d1_open, d2_open):
    """The #1369 page: two <details>, each a group led by its summary."""
    y = 100.0
    d1 = [summary("Shipping options", (120.0, y), d1_open)]
    y += 30
    if d1_open:
        d1 += [button("Express Ship", (120.0, y)), field("Ship note", (220.0, y))]
        y += 30
    d2 = [summary("Billing info", (120.0, y), d2_open)]
    y += 30
    if d2_open:
        d2 += [button("Pay Now", (120.0, y)), field("Bill note", (220.0, y))]
    return ax("AXWindow", title="Details Lab", at=(100.0, 50.0), size=(1000.0, 700.0),
              children=[
                  ax("AXGroup", at=(110.0, 95.0), size=(600.0, 60.0), children=d1),
                  ax("AXGroup", at=(110.0, 155.0), size=(600.0, 60.0), children=d2),
              ])


@pytest.mark.parametrize("title,role", [("Pay Now", "AXButton"), ("Bill note", "AXTextField")])
def test_ref_in_collapsed_details_names_the_collapsed_section(title, role):
    # Snapshot: d1 collapsed, d2 open. Then d1 opens and d2 collapses, so
    # d1's controls take the old slots of d2's controls (the issue's case).
    old = snap_of(details_lab(False, True))
    live = snap_of(details_lab(True, False))
    target = by_title(old, title)
    assert target.role == role
    with pytest.raises(ComputerUseError) as caught:
        observe.rematch_ref(old, target.ref, live)
    err = caught.value
    message = str(err)
    assert err.code is ErrorCode.STALE_REF
    assert err.detail["reason"] == "collapsed_ancestor"
    assert err.detail["candidates"] == []
    assert err.detail["collapsed"]["title"] == "Billing info"
    assert err.detail["collapsed"]["ref"] == by_title(old, "Billing info").ref
    assert "Billing info" in message and "collapsed" in message and "Expand" in message
    for wrong in ("Express Ship", "Ship note", "Shipping options", "reordered"):
        assert wrong not in message
    assert "use find" not in message


def test_ref_in_open_details_still_resolves():
    old = snap_of(details_lab(True, True))
    live = snap_of(details_lab(True, True))
    assert observe.rematch_ref(old, by_title(old, "Pay Now").ref, live).title == "Pay Now"


def test_other_section_collapsing_does_not_claim_collapsed_ancestor():
    # d1 collapses; d2 (the ref's section) stays open and the ref resolves.
    old = snap_of(details_lab(True, True))
    live = snap_of(details_lab(False, True))
    assert observe.rematch_ref(old, by_title(old, "Pay Now").ref, live).title == "Pay Now"


def test_removed_control_in_still_open_section_keeps_existing_reason():
    old = snap_of(details_lab(False, True))
    tree = details_lab(False, True)
    tree["children"][1]["children"][1]["title"] = "Pay Later"
    live = snap_of(tree)
    with pytest.raises(ComputerUseError) as caught:
        observe.rematch_ref(old, by_title(old, "Pay Now").ref, live)
    assert caught.value.detail["reason"] == "title_changed"


def test_unrelated_window_level_disclosure_is_not_blamed():
    # A window-level expanded menu button that collapses is not the owner of
    # a control that was removed; the existing reason stays.
    def tree(menu_open, with_target):
        kids = [summary("Menu", (120.0, 60.0), menu_open)]
        kids.append(button("Pay Now" if with_target else "Other", (120.0, 200.0)))
        return ax("AXWindow", title="W", at=(100.0, 50.0), size=(1000.0, 700.0),
                  children=kids)
    old = snap_of(tree(True, True))
    live = snap_of(tree(False, False))
    with pytest.raises(ComputerUseError) as caught:
        observe.rematch_ref(old, by_title(old, "Pay Now").ref, live)
    assert caught.value.detail["reason"] != "collapsed_ancestor"


def test_hinted_ref_is_the_callers_summary_and_opens_the_section():
    """#1370 review: the hint ref is from the caller's snapshot, not the re-snapshot."""
    old = snap_of(details_lab(False, True))       # the caller's latest snapshot
    live = snap_of(details_lab(True, False))
    with pytest.raises(ComputerUseError) as caught:
        observe.rematch_ref(old, by_title(old, "Pay Now").ref, live)
    hinted = caught.value.detail["collapsed"]["ref"]
    assert f"ref {hinted}" in str(caught.value)
    # In the caller's snapshot the hinted ref is the summary, not its group.
    in_caller = old.element(hinted)
    assert (in_caller.role, in_caller.title) == ("AXButton", "Billing info")
    # The internal re-snapshot numbers differently; its ref must not leak.
    internal = by_title(live, "Billing info").ref
    assert internal != hinted
    assert f"ref {internal} " not in str(caught.value)
    # Clicking the hinted ref resolves to the live summary, which is collapsed.
    target = observe.rematch_ref(old, hinted, live)
    assert (target.role, target.title, target.expanded) == ("AXButton", "Billing info", False)
    # Activating that summary opens the <details>; its content is back.
    opened = snap_of(details_lab(True, True))
    assert by_title(opened, "Billing info").expanded is True
    assert by_title(opened, "Pay Now").role == "AXButton"


def test_no_reliable_caller_ref_falls_back_to_title_only():
    """Two summaries share the title: name it, ask to snapshot again, no ref."""
    def tree(open_second):
        page = details_lab(False, open_second)
        page["children"].append(ax("AXGroup", at=(110.0, 400.0), size=(600.0, 60.0),
                                   children=[summary("Billing info", (120.0, 400.0), False),
                                             button("Elsewhere", (120.0, 430.0)),
                                             button("Elsewhere 2", (220.0, 430.0))]))
        return page
    old = snap_of(tree(True))
    live = snap_of(tree(False))
    with pytest.raises(ComputerUseError) as caught:
        observe.rematch_ref(old, by_title(old, "Pay Now").ref, live)
    err = caught.value
    message = str(err)
    assert err.detail["reason"] == "collapsed_ancestor"
    assert err.detail["collapsed"]["ref"] is None
    assert "Billing info" in message and "snapshot again" in message
    assert re.search(r"\be\d+\b", message.replace(err.detail["ref"], "")) is None


def gift_page(first_open, *, extra_first=False):
    """Tester's #1370 page: two <details>, both summarised 'Gift options'."""
    y = 100.0
    groups = []
    for name, is_open in (("A", first_open), ("B", True)):
        kids = [summary("Gift options", (120.0, y), is_open)]
        y += 30
        if is_open:
            kids += [button(f"Wrap {name}", (120.0, y)), field(f"Note {name}", (220.0, y))]
            y += 30
        groups.append(ax("AXGroup", at=(110.0, y - 60), size=(600.0, 60.0), children=kids))
    if extra_first:
        groups.insert(0, ax("AXGroup", at=(110.0, 20.0), size=(600.0, 30.0), children=[
            summary("Gift options", (120.0, 20.0), False), button("Spare", (120.0, 40.0))]))
    return ax("AXWindow", title="Gift Lab", at=(100.0, 10.0), size=(1000.0, 700.0),
              children=groups)


def _assert_title_only_collapsed(err, title):
    message = str(err)
    assert err.code is ErrorCode.STALE_REF
    assert err.detail["reason"] == "collapsed_ancestor"
    assert err.detail["candidates"] == []
    assert err.detail["collapsed"]["ref"] is None
    assert title in message and "snapshot again" in message
    for wrong in ("Wrap B", "Note B", "reordered", "use find"):
        assert wrong not in message
    assert re.search(r"\be\d+\b", message.replace(err.detail["ref"], "")) is None


@pytest.mark.parametrize("title", ["Wrap A", "Note A"])
def test_duplicate_titled_collapsed_section_refuses_title_only(title):
    """#1370 re-gate: the first of two 'Gift options' collapses."""
    old = snap_of(gift_page(True))
    live = snap_of(gift_page(False))
    with pytest.raises(ComputerUseError) as caught:
        observe.rematch_ref(old, by_title(old, title).ref, live)
    _assert_title_only_collapsed(caught.value, "Gift options")


def test_duplicate_titled_open_section_still_resolves():
    old = snap_of(gift_page(True))
    live = snap_of(gift_page(True))
    assert observe.rematch_ref(old, by_title(old, "Wrap B").ref, live).title == "Wrap B"


def test_unidentifiable_same_titled_section_fails_safe():
    """A third 'Gift options' appears: which one held the ref is unknown."""
    old = snap_of(gift_page(True))
    live = snap_of(gift_page(False, extra_first=True))
    with pytest.raises(ComputerUseError) as caught:
        observe.rematch_ref(old, by_title(old, "Wrap A").ref, live)
    _assert_title_only_collapsed(caught.value, "Gift options")
