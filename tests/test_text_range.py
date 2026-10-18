"""Hermetic GTK text range targeting; no bus or desktop required."""
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from a11y_computer_use import observe
from a11y_computer_use.drivers import _atspi, _linux_input
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Bounds, ComputerUseError, Display, Element, Point, Scope, Snapshot


@pytest.fixture
def native(monkeypatch):
    state = NS(raw="alpha DRAGWORDK5 omega\nsecond line here", showing=True,
               rect=NS(x=70, y=20, width=100, height=18), missing=False)
    def char(_acc, offset, coord):
        assert coord == 0
        return NS(x=70 + (offset - 6) * 10, y=20, width=10, height=18)
    def extent(_acc, start, end, coord):
        assert (start, end, coord) == (6, 16, 0)
        return None if state.missing else state.rect
    monkeypatch.setattr(_atspi, '_atspi', lambda: NS(
        CoordType=NS(SCREEN=0), Text=NS(get_range_extents=extent, get_character_extents=char)))
    monkeypatch.setattr(_atspi, 'is_secure', lambda h: False)
    monkeypatch.setattr(_atspi, '_state_has', lambda h, s: state.showing)
    monkeypatch.setattr(_atspi, '_full_text', lambda h: state.raw)
    monkeypatch.setattr(_atspi, '_raw_rect', lambda h, c: (10, 10, 638, 347))
    monkeypatch.setattr(_atspi, '_toolkit_name', lambda h: 'gtk')
    monkeypatch.setattr(observe, 'ax_handle_for', lambda sid, ref: object())
    return state


def element():
    return Element('e1', 'AXTextArea', '', 'alpha DRAGWORDK5 omega',
                   Bounds(0, 10, 10, 638, 347), 'range-test', editable=True)


def driver(monkeypatch):
    d = LinuxDriver()
    monkeypatch.setattr(d, '_run', lambda fn: fn())
    monkeypatch.setattr(d, 'displays', lambda: (Display(0, 1280, 800, 1.0, True),))
    return d


def test_range_uses_native_offsets_and_screen_box(native, monkeypatch):
    d = driver(monkeypatch)
    target = d.text_match_target(element(), 'dragwordk5')
    assert target.bounds == Bounds(0, 70, 20, 100, 18)
    assert target.text_range == (6, 16, 'DRAGWORDK5')
    assert target.ref == 'e1'
    snap = Snapshot('range-test', Scope.WINDOW, 'editor', 1, 0, (), (target,))
    live = replace(snap, snapshot_id='range-live', elements=(replace(element(), snapshot_id='range-live'),))
    assert d.resolve_ref(snap, 'e1', live=live).bounds == target.bounds
    native.raw = 'alpha DIFFERENT! omega'
    with pytest.raises(ComputerUseError, match='verified visible screen range'):
        d.resolve_ref(snap, 'e1', live=live)


@pytest.mark.parametrize('bad', ['missing', 'outside', 'hidden', 'zero', 'nan'])
def test_unavailable_or_invisible_range_refused(native, monkeypatch, bad):
    if bad == 'missing':
        native.missing = True
    elif bad == 'outside':
        native.rect.x = 900
    elif bad == 'hidden':
        native.showing = False
    elif bad == 'zero':
        native.rect.width = 0
    else:
        native.rect.x = float('nan')
    with pytest.raises(ComputerUseError) as exc:
        driver(monkeypatch).text_match_target(element(), 'DRAGWORDK5')
    assert exc.value.detail['reason'] == 'text_range_unavailable'
    assert 'SCREEN' in exc.value.detail['hint']


def test_range_drag_uses_word_centre(native, monkeypatch):
    d = driver(monkeypatch)
    target = d.text_match_target(element(), 'DRAGWORDK5')
    calls = []
    monkeypatch.setattr(d, '_pointer', lambda x, y: (x, y))
    monkeypatch.setattr(d, '_note_pointer', lambda *a: None)
    monkeypatch.setattr('a11y_computer_use.drivers.linux._on_wayland', lambda: False)
    monkeypatch.setattr(_linux_input, 'drag', lambda *a, **kw: calls.append(a))
    d.drag(target, Point(0, 200, 100))
    assert calls == [(120, 29, 200, 100)]


def test_missing_native_text_does_not_return_widget_target(native, monkeypatch):
    native.raw = None
    with pytest.raises(ComputerUseError, match='native offsets'):
        driver(monkeypatch).text_match_target(element(), 'DRAGWORDK5')


def test_wrapped_or_unreadable_character_geometry_is_refused(native, monkeypatch):
    api = _atspi._atspi()
    api.Text.get_character_extents = lambda h, i, c: NS(
        x=70, y=20 if i < 11 else 40, width=10, height=18)
    monkeypatch.setattr(_atspi, '_atspi', lambda: api)
    with pytest.raises(ComputerUseError):
        driver(monkeypatch).text_match_target(element(), 'DRAGWORDK5')
    api.Text.get_character_extents = lambda *args: None
    with pytest.raises(ComputerUseError):
        driver(monkeypatch).text_match_target(element(), 'DRAGWORDK5')


@pytest.mark.parametrize('count,span,expected', [
    (0, None, ()), (1, (6, 16), ((6, 16),)),
    (None, None, None), (1, None, None), (1, (6, 6), ()),
])
def test_selection_reads_distinguish_empty_from_unavailable(native, monkeypatch, count, span, expected):
    api = _atspi._atspi()
    api.Text.get_n_selections = lambda h: count
    api.Text.get_selection = lambda h, i: span
    monkeypatch.setattr(_atspi, '_atspi', lambda: api)
    assert driver(monkeypatch).text_selection(element()) == expected


def test_find_publishes_range_in_current_and_issued_epoch(native, monkeypatch, tmp_path):
    from a11y_computer_use import server, safety
    d = driver(monkeypatch)
    snap = Snapshot('range-test', Scope.WINDOW, 'editor', 1, 0, (), (element(),))
    monkeypatch.setattr(d, 'ensure_trusted', lambda: None)
    monkeypatch.setattr(d, 'snapshot', lambda *a, **kw: snap)
    with server.Runtime(driver=d, store=safety.PermissionStore(tmp_path / 'p.json'),
                        audit=safety.AuditLog(tmp_path / 'audit')) as rt:
        monkeypatch.setattr(rt, '_resolve_app', lambda app: (None, 'editor'))
        monkeypatch.setattr(rt, '_run_gated', lambda action, app, execute: execute())
        found = rt.find('editor', text='DRAGWORDK5')
        assert '[100x18 @0:70,20]' in found
        assert 'text range [6, 16)' in found
        assert rt._current.element('e1').text_range == (6, 16, 'DRAGWORDK5')
        assert rt._current.element('e1').bounds == element().bounds
        resolved = d.resolve_ref(rt._current, 'e1', live=snap)
        assert resolved.bounds == Bounds(0, 70, 20, 100, 18)
        assert resolved.text_range == (6, 16, 'DRAGWORDK5')
        assert rt._last_issued is rt._current


def test_range_inside_widget_but_outside_display_is_refused(native, monkeypatch):
    d = driver(monkeypatch)
    monkeypatch.setattr(d, 'displays', lambda: (Display(0, 100, 800, 1.0, True),))
    with pytest.raises(ComputerUseError, match='outside the visible display'):
        d.text_match_target(element(), 'DRAGWORDK5')
