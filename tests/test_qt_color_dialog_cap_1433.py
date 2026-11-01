"""#1433: Qt 6 QColorDialog basic swatches past the 24th child.

Display-free. The tree models what Qt 6 publishes over AT-SPI for the
dialog: a ``list "Basic colors"`` of 48 ``list item`` children (AXRow here)
named ``RGB r, g, b`` and laid out by QWellArray column-major (colour ``i``
at row ``i % 6``, column ``i // 6``), all SHOWING, and a ``list "Custom
colors"`` of 16 white cells. Each node carries its bus name and object path
as ``node_identity``.
"""

from __future__ import annotations

import dataclasses

import pytest

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi
from a11y_computer_use.drivers.linux import LinuxDriver

from a11y_computer_use.observe import build_snapshot, find_elements
from a11y_computer_use.schema import ComputerUseError, ErrorCode, Scope, Snapshot
from tests.fixtures.trees import GEOMETRY, DictAccessor, ax

BUS = ":1.42"


def _qt_basic_rgb() -> list[tuple[int, int, int]]:
    # qcolordialog.cpp initRgb(): g outer, r, then b.
    return [(r * 255 // 3, g * 255 // 3, b * 255 // 2)
            for g in range(4) for r in range(4) for b in range(3)]


def _swatch(i: int, rgb, x0: float, y0: float, rows: int, path: str) -> dict:
    r, g, b = rgb
    return ax("AXRow", title=f"RGB {r}, {g}, {b}",
              at=(x0 + (i // rows) * 28.0, y0 + (i % rows) * 24.0), size=(28.0, 24.0),
              actions=("AXPress",), node_identity=(BUS, f"{path}/{i}"))


def qt_color_dialog() -> dict:
    basic = [_swatch(i, rgb, 31.0, 63.0, 6, "/org/a11y/atspi/accessible/basic")
             for i, rgb in enumerate(_qt_basic_rgb())]
    custom = [_swatch(i, (255, 255, 255), 31.0, 260.0, 2, "/org/a11y/atspi/accessible/custom")
              for i in range(16)]
    return ax("AXWindow", title="Select Color", at=(0.0, 0.0), size=(560.0, 420.0),
              node_identity=(BUS, "/org/a11y/atspi/accessible/win"), children=[
        ax("AXList", title="Basic colors", at=(31.0, 63.0), size=(224.0, 144.0),
           node_identity=(BUS, "/org/a11y/atspi/accessible/basic"), children=basic),
        ax("AXList", title="Custom colors", at=(31.0, 260.0), size=(224.0, 48.0),
           node_identity=(BUS, "/org/a11y/atspi/accessible/custom"), children=custom),
    ])


class QtAccessor(DictAccessor):
    def __init__(self, seek: str | None = None) -> None:
        self.child_seek = seek
        self.capped_titles: list[str] = []

    def read(self, node):
        return dataclasses.replace(super().read(node),
                                   node_identity=tuple(node.get("node_identity", ())))

    note_capped = _atspi.ATSPIAccessor.note_capped


def _snap(seek: str | None = None, accessor: QtAccessor | None = None) -> Snapshot:
    return build_snapshot(qt_color_dialog(), accessor or QtAccessor(seek), scope=Scope.WINDOW,
                          app="python3", pid=4242, geometry=GEOMETRY)


GREEN = "RGB 0, 255, 0"  # colour 36: top row, 7th column (x=199, y=63)


def _basic(snap: Snapshot):
    lst = next(el for el in snap.elements if el.title == "Basic colors")
    return [el for el in snap.elements if el.parent == lst.ref]


def test_plain_snapshot_folds_basic_swatches_past_the_24th() -> None:
    accessor = QtAccessor()
    snap = _snap(accessor=accessor)
    kept = [el.title for el in _basic(snap)]
    rgb = _qt_basic_rgb()
    assert kept == [f"RGB {r}, {g}, {b}" for r, g, b in rgb[:24]]
    assert not find_elements(snap, text=GREEN)
    # Every colour with green 170 or 255 is the folded half.
    assert all(t.split(", ")[1] in {"0", "85"} for t in kept)
    assert GREEN in accessor.capped_titles
    assert len([el for el in snap.elements if el.title == "RGB 255, 255, 255"]) == 16


def test_armed_snapshot_keeps_the_swatch() -> None:
    snap = _snap(seek=GREEN)
    hits = find_elements(snap, text=GREEN)
    assert [el.title for el in hits] == [GREEN]
    assert hits[0].node_identity == (BUS, "/org/a11y/atspi/accessible/basic/36")


def test_label_rematch_against_plain_tree_is_title_changed_nearest_occupant() -> None:
    """The reported child-18 occupant is the nearest kept swatch, not an index slip."""
    armed = _snap(seek=GREEN)
    ref = find_elements(armed, text=GREEN)[0].ref
    with pytest.raises(ComputerUseError) as caught:
        observe.rematch_ref(armed, ref, _snap())
    assert caught.value.detail["reason"] == "title_changed"
    assert "RGB 170, 85, 0" in repr(caught.value.detail)  # colour 18: column 4, top row


def test_hidden_child_named_sees_cap_folded_names() -> None:
    accessor = QtAccessor()
    _snap(accessor=accessor)
    driver = LinuxDriver.__new__(LinuxDriver)
    driver._last_accessor = accessor
    assert driver.hidden_child_named("0, 255, 0")
    assert not driver.hidden_child_named("RGB 1, 2, 3")


# --- resolve_ref: the exact object, by bus name and path ----------------------

class _Node:
    def __init__(self, key, name, parent=None, showing=True, gone=False):
        self.key, self.name, self.parent, self.showing, self.gone = key, name, parent, showing, gone


@pytest.fixture
def fake_atspi(monkeypatch):
    monkeypatch.setattr(_atspi, "_atspi_wrapper_key", lambda n: getattr(n, "key", None))
    monkeypatch.setattr(_atspi, "accessible_gone", lambda n: n is None or n.gone)
    monkeypatch.setattr(_atspi, "_state_has", lambda n, s: s == "SHOWING" and n.showing)
    monkeypatch.setattr(_atspi, "_node_name", lambda n: n.name)
    monkeypatch.setattr(_atspi, "_parent_of", lambda n: n.parent)


def _resolver(monkeypatch, handle):
    armed = _snap(seek=GREEN)
    ref = find_elements(armed, text=GREEN)[0].ref
    plain = _snap()
    monkeypatch.setattr(observe, "ax_handle_for",
                        lambda sid, r: handle if (sid, r) == (armed.snapshot_id, ref) else None)

    class Driver:
        _run = staticmethod(lambda fn: fn())
        snapshot = staticmethod(lambda scope, app: plain)
        _raise_if_hidden_alive = staticmethod(lambda *a, **k: None)
        _remember_popover_cover = staticmethod(lambda *a, **k: None)
    return Driver(), armed, ref


def _green_handle(**kw):
    basic = _Node((BUS, "/org/a11y/atspi/accessible/basic"), "Basic colors")
    fields = {"key": (BUS, "/org/a11y/atspi/accessible/basic/36"), "name": GREEN, "parent": basic}
    fields.update(kw)
    return _Node(**fields)


def test_resolve_ref_binds_the_exact_cap_folded_object(monkeypatch, fake_atspi) -> None:
    driver, armed, ref = _resolver(monkeypatch, _green_handle())
    resolved = LinuxDriver.resolve_ref(driver, armed, ref)
    assert resolved.title == GREEN
    assert resolved.node_identity == (BUS, "/org/a11y/atspi/accessible/basic/36")


@pytest.mark.parametrize("change", [
    {"name": "RGB 1, 2, 3"},                                   # really renamed
    {"gone": True},                                            # destroyed
    {"showing": False},
    {"key": (BUS, "/org/a11y/atspi/accessible/basic/18")},    # another object
    {"parent": _Node((BUS, "/elsewhere"), "Other")},          # reparented
])
def test_resolve_ref_still_refuses_real_changes(monkeypatch, fake_atspi, change) -> None:
    driver, armed, ref = _resolver(monkeypatch, _green_handle(**change))
    with pytest.raises(ComputerUseError) as caught:
        LinuxDriver.resolve_ref(driver, armed, ref)
    assert caught.value.code is ErrorCode.STALE_REF
    assert caught.value.detail["reason"] == "title_changed"
