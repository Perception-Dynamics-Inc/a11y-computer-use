"""#1449: a sort must not make a column-header ref look like a recycled row."""
import pytest

from a11y_computer_use.drivers import _atspi
from a11y_computer_use.drivers.linux import LinuxDriver

from a11y_computer_use.schema import ComputerUseError, Element, ErrorCode
from tests.test_linux_synthetic import _GeckoNode, fake_atspi  # noqa: F401


def _table(monkeypatch):
    button = _GeckoNode("push button", "Company")
    th = _GeckoNode("column header", children=[button])
    head = _GeckoNode("table row", "First Name Last Name \u25b2 Company", children=[th])
    head.attributes = {"rowindex": "0"}
    link = _GeckoNode("link", "513")
    cell = _GeckoNode("table cell", children=[link])
    body = _GeckoNode("table row", "Ralph Jefferson 513", children=[cell])
    body.attributes = {"rowindex": "3"}
    table = _GeckoNode("table", children=[head, body])
    _GeckoNode("document web", children=[table])
    monkeypatch.setattr(_atspi, "_chromium_app", lambda node: True)
    monkeypatch.setattr(_atspi, "_drop_cached_properties", lambda node: None)
    driver = LinuxDriver()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    return driver, button, th, head, link, body


def _el(node, title):
    identity = _atspi.chromium_row_identity(node)
    assert identity
    return Element("e37", "AXButton", title, None, None, "hdr", row_identity=identity)


@pytest.mark.parametrize("change", ["row_name", "row_index", "row_moved"])
def test_header_ref_survives_sort(fake_atspi, monkeypatch, change):
    driver, button, th, head, *_ = _table(monkeypatch)
    element = _el(button, "Company")
    if change == "row_name":
        head.name = "First Name Last Name Company Favorite Number \u25b2"
    elif change == "row_index":
        head.attributes["rowindex"] = "1"
    else:
        head.parent = _GeckoNode("table")
    driver._verify_chromium_row_target(element, button)


@pytest.mark.parametrize("change", ["button_name", "header_role", "not_header"])
def test_changed_header_is_still_refused(fake_atspi, monkeypatch, change):
    driver, button, th, head, *_ = _table(monkeypatch)
    element = _el(button, "Company")
    if change == "button_name":
        button.name = "Price"
    elif change == "header_role":
        th.role = "table cell"
    else:
        head.name = "changed"
        th.role = "table cell"
    with pytest.raises(ComputerUseError) as exc:
        driver._verify_chromium_row_target(element, button)
    assert exc.value.code is ErrorCode.STALE_REF
    assert exc.value.detail["outcome"] == "refused"
    assert not button.pressed


def test_body_row_recycle_is_still_refused(fake_atspi, monkeypatch):
    driver, *_, link, body = _table(monkeypatch)
    element = _el(link, "513")
    body.name = "Fred Jackson 56"
    with pytest.raises(ComputerUseError) as exc:
        driver._verify_chromium_row_target(element, link)
    assert exc.value.detail["reason"] == "target_changed"
