"""Hermetic GTK 4 buffer visibility regressions for #588."""
from types import SimpleNamespace as NS

import pytest

from a11y_computer_use.drivers import _atspi


@pytest.mark.parametrize('failure', [None, 'range', 'widget'])
@pytest.mark.parametrize('multiline', [True, False])
def test_gtk4_value_only_contains_verified_window_lines(monkeypatch, failure, multiline):
    text = 'SECOND_LINE\nDEEP_MARKER\nTAIL_MARKER'
    calls = []
    positions = {0: 96, 12: 5076, 24: 8076}

    class Text:
        @staticmethod
        def get_character_count(acc):
            return len(text)

        @staticmethod
        def get_range_extents(acc, start, end, coord):
            calls.append((start, end, coord))
            assert coord == 1  # SCREEN is unsupported; no point query exists.
            if failure == 'range':
                raise RuntimeError('Unsupported coordinate space')
            return NS(x=7, y=positions[start] - acc.scroll, width=100, height=16)

    acc = NS(scroll=0)
    monkeypatch.setattr(_atspi, '_atspi', lambda: NS(Text=Text, CoordType=NS(SCREEN=0, WINDOW=1)))
    monkeypatch.setattr(_atspi, 'is_secure', lambda acc: False)
    monkeypatch.setattr(_atspi, '_gtk4_app', lambda acc: True)
    monkeypatch.setattr(_atspi, '_get_attributes', lambda acc: {'multiline': 'true'} if multiline else {})
    monkeypatch.setattr(_atspi, '_state_has', lambda *args: False)
    monkeypatch.setattr(_atspi, '_read_full_text', lambda *args, **kwargs: text)
    monkeypatch.setattr(_atspi, '_raw_rect', lambda acc, coord: None if failure == 'widget' else (-5, 82, 690, 423))
    read = lambda: _atspi._value_text(acc, 'AXTextArea', 'text')
    if failure:
        assert read() is None
        return
    assert read() == 'SECOND_LINE'
    acc.scroll = 4980
    assert read() == 'DEEP_MARKER'
    acc.scroll = 7980
    assert read() == 'TAIL_MARKER'
    assert calls


def test_gtk4_partial_or_horizontal_offscreen_line_is_not_visible(monkeypatch):
    rectangles = iter([NS(x=7, y=75, width=100, height=16), NS(x=680, y=96, width=100, height=16)])
    monkeypatch.setattr(_atspi, '_atspi', lambda: NS(CoordType=NS(WINDOW=1), Text=NS(get_range_extents=lambda *args: next(rectangles))))
    monkeypatch.setattr(_atspi, '_raw_rect', lambda *args: (-5, 82, 690, 423))
    assert _atspi._gtk4_visible_buffer(object(), 'partial\nhorizontal') == ''


@pytest.mark.parametrize("hidden", [None, 1])
def test_gtk4_missing_range_uses_measured_character_rectangles(monkeypatch, hidden):
    calls = []

    def character(acc, offset, coord):
        calls.append((offset, coord))
        return NS(x=7, y=600 if offset == hidden else 96, width=9, height=16)

    monkeypatch.setattr(_atspi, '_atspi', lambda: NS(
        CoordType=NS(WINDOW=1), Text=NS(get_character_extents=character),
    ))
    monkeypatch.setattr(_atspi, '_raw_rect', lambda *args: (-5, 82, 690, 423))
    assert _atspi._gtk4_visible_buffer(object(), 'abc') == ('abc' if hidden is None else '')
    assert calls == [(0, 1), (1, 1), (2, 1)]


@pytest.mark.parametrize('text', ['hello', '7', 'hello\nDEEP_MARKER'])
def test_gtk4_unsupported_geometry_keeps_buffer_readback_out_of_search(monkeypatch, text):
    from a11y_computer_use import observe
    from a11y_computer_use.schema import Scope
    from tests.test_gtk4_bounds import _App, _Node
    from tests.fixtures.trees import GEOMETRY

    node = _Node('text', '', (0, 0, 500, 400), (0, 80, 500, 400),
                 app=_App('GTK', '4.14.5'))
    monkeypatch.setattr(_atspi, '_atspi', lambda: NS(Text=NS(
        get_character_count=lambda acc: len(text),
    ), CoordType=NS(WINDOW=1, SCREEN=0)))
    monkeypatch.setattr(_atspi, '_state_has', lambda acc, flag: flag in {'MULTI_LINE', 'EDITABLE', 'SHOWING', 'VISIBLE'})
    monkeypatch.setattr(_atspi, '_read_full_text', lambda *args, **kwargs: text)
    monkeypatch.setattr(_atspi, '_gtk4_client_origin', lambda *args: (10, 10))
    accessor = _atspi.ATSPIAccessor()
    raw = accessor.read(node)
    assert raw.value == text
    assert raw.search_value == ''
    assert 'visibility unavailable' in raw.content_note
    snap = observe.build_snapshot(node, accessor, scope=Scope.WINDOW,
                                  app='editor', pid=77, geometry=GEOMETRY)
    assert any(el.value == text for el in snap.elements)
    assert observe.find_elements(snap, text=text.splitlines()[-1]) == ()

    monkeypatch.setattr(_atspi, '_gtk4_visible_buffer', lambda *args: text.splitlines()[0])
    visible = observe.build_snapshot(node, accessor, scope=Scope.WINDOW,
                                     app='editor', pid=77, geometry=GEOMETRY)
    assert observe.find_elements(visible, text=text.splitlines()[0])
    if '\n' in text:
        assert observe.find_elements(visible, text='DEEP_MARKER') == ()
