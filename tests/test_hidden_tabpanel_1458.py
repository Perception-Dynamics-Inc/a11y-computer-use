"""#1458: a ref inside an ARIA tab panel that is now hidden or re-rendering.

The refusal must name the held control and the owning tab (with its ref in
the caller's snapshot) and say to select that tab first. It must not name a
control from another panel or browser chrome, offer candidates, or suggest
find()/scroll_to_find.
"""
import pytest

from a11y_computer_use import observe, outcome
from a11y_computer_use.schema import ComputerUseError, ErrorCode
from tests.fixtures.trees import ax, button
from tests.test_observe import by_title, snap_of


def tab(title, at, selected):
    return ax("AXTab", title=title, at=at, size=(90.0, 24.0), actions=("AXPress",),
              selected=selected)


def field(title, at):
    return ax("AXTextField", title=title, at=at, size=(150.0, 20.0), value="")


def tabs_page(shown, panel_body=None):
    """The issue's minimal APG tabs page; ``shown`` names the visible panel."""
    chrome = [button("Back", (10.0, 10.0)), button("Forward", (100.0, 10.0))]
    tablist = ax("AXTabGroup", at=(110.0, 60.0), size=(400.0, 30.0), children=[
        tab("Profile", (110.0, 60.0), shown == "Profile"),
        tab("Billing", (210.0, 60.0), shown == "Billing"),
    ])
    if shown == "Profile":
        body = panel_body if panel_body is not None else [
            field("Display name", (120.0, 100.0)), button("Save profile", (120.0, 130.0))]
    else:
        body = panel_body if panel_body is not None else [
            field("Card number", (120.0, 100.0)), button("Pay", (120.0, 130.0))]
    panel = ax("AXScrollArea", subrole="tabpanel", title=shown, at=(110.0, 95.0),
               size=(600.0, 200.0), children=body)
    return ax("AXWindow", title="Tabs Lab", at=(0.0, 0.0), size=(1000.0, 700.0),
              children=chrome + [tablist, panel])


@pytest.mark.parametrize("title", ["Display name", "Save profile"])
def test_ref_in_hidden_tabpanel_names_the_owning_tab(title):
    old = snap_of(tabs_page("Profile"))
    live = snap_of(tabs_page("Billing"))
    target = by_title(old, title)
    with pytest.raises(ComputerUseError) as caught:
        observe.rematch_ref(old, target.ref, live)
    err = caught.value
    message = str(err)
    assert err.code is ErrorCode.STALE_REF
    assert err.detail["reason"] == "hidden_ancestor"
    assert err.detail["candidates"] == []
    assert err.detail["next"] == ["snapshot"]
    assert err.detail["tabpanel"] == {"title": "Profile", "state": "hidden"}
    assert err.detail["tab"]["ref"] == by_title(old, "Profile").ref
    assert target.ref in message and title in message
    assert "Profile" in message and "Select the owning tab first" in message
    for wrong in ("Card number", "Pay'", "Forward", "Back", "reordered", "use find"):
        assert wrong not in message
    result = outcome.refused_result(text=message, code=err.code.value,
                                    message=err.message, detail=err.detail)
    assert result.outcome == "refused"
    assert tuple(result.next) == ("snapshot",)


def test_ref_in_rerendering_tabpanel_says_reobserve():
    old = snap_of(tabs_page("Profile"))
    loading = ax("AXStaticText", subrole="status", title="Loading…", at=(120.0, 100.0),
                 size=(100.0, 20.0))
    live = snap_of(tabs_page("Profile", panel_body=[loading]))
    with pytest.raises(ComputerUseError) as caught:
        observe.rematch_ref(old, by_title(old, "Save profile").ref, live)
    err = caught.value
    assert err.detail["reason"] == "hidden_ancestor"
    assert err.detail["tabpanel"]["state"] == "rerendering"
    assert err.detail["candidates"] == []
    assert err.detail["next"] == ["snapshot"]
    assert "re-rendering" in str(err) and "Loading" in str(err)
    assert "new snapshot" in str(err)
    result = outcome.refused_result(text=str(err), code=err.code.value,
                                    message=err.message, detail=err.detail)
    assert result.outcome == "refused"
    assert tuple(result.next) == ("snapshot",)


def test_ref_in_shown_tabpanel_still_resolves():
    old = snap_of(tabs_page("Profile"))
    live = snap_of(tabs_page("Profile"))
    assert observe.rematch_ref(old, by_title(old, "Save profile").ref, live).title == "Save profile"
