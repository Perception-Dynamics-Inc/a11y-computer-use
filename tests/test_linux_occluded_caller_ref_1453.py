"""An occlusion refusal names the ref the caller holds, not a renumbered one (#1453)."""
import pytest

from a11y_computer_use.drivers import _atspi
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Bounds, ComputerUseError, Element, Snapshot


def _el(ref, role="AXButton", title="Reset all"):
    return Element(ref=ref, role=role, title=title, value=None, snapshot_id="snap-6", bounds=Bounds(1, 24, 151, 220, 170))


def _driver(monkeypatch, live_el):
    driver = LinuxDriver.__new__(LinuxDriver)
    driver._run = lambda fn: fn()
    monkeypatch.setattr(LinuxDriver, "_resolve_ref_live", lambda self, snap, ref, live=None: live_el)
    return driver


@pytest.mark.parametrize("occluder", [("menu", "Actions"), _atspi.UNKNOWN_MENU_STATE])
def test_occlusion_refusal_names_callers_ref(monkeypatch, occluder):
    live_el = _el("e32")
    driver = _driver(monkeypatch, live_el)
    monkeypatch.setattr(_atspi, "chromium_menu_occluder", lambda handle, x, y: occluder)
    snap = Snapshot.__new__(Snapshot)
    resolved = driver.resolve_ref(snap, "e26")
    with pytest.raises(ComputerUseError) as caught:
        driver._refuse_chromium_occluded(resolved, object())
    assert caught.value.detail["ref"] == "e26"
    assert "e26" in caught.value.message and "e32" not in caught.value.message
    assert "e32" not in str(caught.value.detail)


def test_unrelated_element_keeps_its_own_ref(monkeypatch):
    driver = _driver(monkeypatch, _el("e32"))
    monkeypatch.setattr(_atspi, "chromium_menu_occluder", lambda handle, x, y: ("menu", "Actions"))
    driver.resolve_ref(Snapshot.__new__(Snapshot), "e26")
    other = _el("e32", role="AXGroup", title="")
    with pytest.raises(ComputerUseError) as caught:
        driver._refuse_chromium_occluded(other, object())
    assert caught.value.detail["ref"] == "e32"


def test_same_numbered_ref_is_unchanged(monkeypatch):
    driver = _driver(monkeypatch, _el("e26"))
    monkeypatch.setattr(_atspi, "chromium_menu_occluder", lambda handle, x, y: ("menu", "Actions"))
    resolved = driver.resolve_ref(Snapshot.__new__(Snapshot), "e26")
    with pytest.raises(ComputerUseError) as caught:
        driver._refuse_chromium_occluded(resolved, object())
    assert caught.value.detail["ref"] == "e26"
