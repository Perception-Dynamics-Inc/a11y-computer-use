"""#1449: a sort must not make a column-header ref look like a recycled row."""
import pytest

from a11y_computer_use.drivers import _atspi
from a11y_computer_use.drivers.linux import LinuxDriver

from a11y_computer_use.schema import ComputerUseError, Element, ErrorCode
from tests.test_linux_synthetic import _GeckoNode, fake_atspi  # noqa: F401


def _table(monkeypatch, button_role="push button"):
    button = _GeckoNode(button_role, "Company")
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


def test_chromium_button_rename_is_refused_before_input(fake_atspi, monkeypatch):
    """Chromium's AT-SPI role is "button": its name must be part of identity."""
    driver, button, *_ = _table(monkeypatch, "button")
    element = _el(button, "Company")
    button.name = "Employer"
    with pytest.raises(ComputerUseError) as exc:
        driver._verify_chromium_row_target(element, button)
    assert exc.value.detail["outcome"] == "refused"
    assert not button.pressed


def test_chromium_button_header_survives_sort(fake_atspi, monkeypatch):
    driver, button, th, head, *_ = _table(monkeypatch, "button")
    element = _el(button, "Company")
    head.name = "resorted"
    head.parent = _GeckoNode("table")
    driver._verify_chromium_row_target(element, button)


def test_flat_identity_does_not_crash():
    from a11y_computer_use.drivers.linux import _header_own_identity
    assert _header_own_identity(("row", "jane@example.com", 2)) == ()


def test_issued_header_identity_is_stable_across_a_reorder(fake_atspi, monkeypatch):
    """#1449 step order: refs issued before a sort are compared, by every
    later snapshot and by the act-time check, with the identity read after
    it. The header's issued identity must not include the header row."""
    driver, button, th, head, link, body = _table(monkeypatch, "button")
    with _atspi.read_cache():
        cache = {}
        before_link = _atspi.chromium_row_identity(link, cache=cache)
        before = _atspi.chromium_row_identity(button, cache=cache)
    assert all(part[0] != "table row" for part in before)
    # Sort by another column: rows re-appended, header row re-indexed and
    # re-parented in the tree, the sort indicator moves.
    head.name = "First Name Last Name Company Favorite Number \u25b2"
    head.attributes["rowindex"] = "1"
    head.parent = _GeckoNode("table")
    body.attributes["rowindex"] = "2"
    after = _atspi.chromium_row_identity(button, fresh=True)
    assert after == before
    assert _atspi.chromium_row_identity(link, fresh=True) != before_link
    element = Element("e37", "AXButton", "Company", None, None, "hdr", row_identity=before)
    driver._verify_chromium_row_target(element, button)


def test_header_ref_survives_reissued_header_objects_1449(fake_atspi, monkeypatch):
    """#1449 order: another column's sort removes aria-sort and the CSS
    indicator from this header, and Chromium re-issues the header cell's
    accessible object (new object path) while it is still the "Company"
    header. The held ref must still click; a rename must still refuse."""
    driver, button, th, head, *_ = _table(monkeypatch, "button")
    element = _el(button, "Company")
    keyed = {th: ("bus", "/new/th"), button: ("bus", "/new/button")}
    real = _atspi._ancestor_key
    monkeypatch.setattr(_atspi, "_ancestor_key", lambda node: keyed.get(node) or real(node))
    head.name = "First Name Last Name Company Favorite Number \u25b2"
    head.attributes["rowindex"] = "1"
    driver._verify_chromium_row_target(element, button)
    button.name = "Employer"
    with pytest.raises(ComputerUseError) as exc:
        driver._verify_chromium_row_target(element, button)
    assert exc.value.detail["reason"] == "target_changed"
    assert exc.value.detail["identity_diff"]
    assert not button.pressed
