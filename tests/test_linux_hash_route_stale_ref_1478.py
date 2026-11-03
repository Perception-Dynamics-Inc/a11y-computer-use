"""#1478: a ref held across a hash-only route change is refused.

A hash router changes pages by fragment alone. A button ref issued on
``#/inbox`` must not press the same-named button on ``#/settings``. An
in-page ``#section`` anchor on the same document is not navigation.
"""
from dataclasses import replace

import pytest

from a11y_computer_use import observe
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import ComputerUseError, Element, ErrorCode, Scope, Snapshot

BASE = "https://app.example.test/"


def _pair(old_url, new_url, *, new_id=None, recreated=True):
    window = Element("e1", "AXWindow", "App", None, None, "h-old",
                     context_identity=("frame", ":1.7", "/window"))
    document = Element("e2", "AXGroup", "App", None, None, "h-old", parent="e1",
                       context_identity=("document web", ":1.7", "/doc"),
                       document_route=old_url)
    button = Element("e3", "AXButton", "Delete", None, None, "h-old", parent="e2",
                     node_identity=(":1.7", "/button"), clickable=True)
    heading = Element("e4", "AXHeading", "Billing", None, None, "h-old", parent="e2",
                      stable_id=new_id)
    old = Snapshot("h-old", Scope.WINDOW, "chrome", 1, 0., (), (window, document, button, heading))
    live = replace(old, snapshot_id="h-live", elements=(
        replace(window, snapshot_id="h-live"),
        replace(document, snapshot_id="h-live", document_route=new_url),
        replace(button, snapshot_id="h-live",
                node_identity=(":1.7", "/button2" if recreated else "/button")),
        replace(heading, snapshot_id="h-live"),
    ))
    return old, live, button


@pytest.fixture(autouse=True)
def _no_handles(monkeypatch):
    monkeypatch.setattr(observe, "ax_handle_for", lambda *args: None)


@pytest.mark.parametrize("run", range(12))
def test_hash_route_change_refuses_same_named_button(run):
    """Tester's 12/12 repro: Delete on #/inbox, then the route is #/settings."""
    old, live, button = _pair(BASE + "#/inbox", BASE + "#/settings")
    with pytest.raises(ComputerUseError) as caught:
        LinuxDriver().resolve_ref(old, button.ref, live=live)
    assert caught.value.code is ErrorCode.STALE_REF
    assert caught.value.detail == {
        "ref": button.ref, "reason": "route_changed", "outcome": "refused",
        "old_url": BASE + "#/inbox", "new_url": BASE + "#/settings",
    }


@pytest.mark.parametrize("old_url, new_url", [
    (BASE, BASE + "#/settings"),
    (BASE + "#/settings", BASE),
    (BASE + "#tab1", BASE + "#tab2"),
])
def test_any_other_fragment_change_is_a_route_change(old_url, new_url):
    old, live, button = _pair(old_url, new_url, recreated=False)
    with pytest.raises(ComputerUseError) as caught:
        LinuxDriver().resolve_ref(old, button.ref, live=live)
    assert caught.value.detail["reason"] == "route_changed"


@pytest.mark.parametrize("old_url", [BASE, BASE + "#intro"])
def test_same_page_anchor_keeps_the_ref(old_url):
    """A #section jump whose fragment names an element id in the same document."""
    old, live, button = _pair(old_url, BASE + "#billing", new_id="billing", recreated=False)
    assert LinuxDriver().resolve_ref(old, button.ref, live=live) is live.elements[2]


def test_anchor_id_must_be_in_the_same_document():
    old, live, button = _pair(BASE, BASE + "#billing", new_id="other", recreated=False)
    with pytest.raises(ComputerUseError):
        LinuxDriver().resolve_ref(old, button.ref, live=live)


def test_anchor_on_another_page_is_still_navigation():
    old, live, button = _pair(BASE + "a", BASE + "b#billing", new_id="billing", recreated=False)
    with pytest.raises(ComputerUseError) as caught:
        LinuxDriver().resolve_ref(old, button.ref, live=live)
    assert caught.value.detail["reason"] == "route_changed"


def test_unchanged_hash_route_keeps_the_ref():
    old, live, button = _pair(BASE + "#/inbox", BASE + "#/inbox", recreated=False)
    assert LinuxDriver().resolve_ref(old, button.ref, live=live) is live.elements[2]
