"""GTK editor searches require viewport text, not whole-buffer membership."""
from types import SimpleNamespace as NS

import pytest

from a11y_computer_use import observe, server
from a11y_computer_use.drivers import _atspi
from tests.test_evince_page_text import _Node, _Text, _snap


@pytest.fixture
def editor(monkeypatch):
    body = 'SHARED-001\n' + ('other line\n' * 149) + 'LEFT-MARKER-150'
    node = _Node('text', body)
    node.visible = [(0, 10)]

    class Text(_Text):
        @staticmethod
        def get_bounded_ranges(*args):
            pytest.fail('native bounded-range arrays must not be queried')

        @staticmethod
        def get_range_extents(acc, start, end, coord):
            pytest.fail('native range extents must not be queried')

        @staticmethod
        def get_character_extents(acc, index, coord):
            if acc.visible is None:
                raise RuntimeError('unsupported')
            valid = [(lo, hi) for lo, hi in acc.visible if 0 <= lo < hi <= len(acc.text)]
            visible = any(lo <= index < hi for lo, hi in valid)
            y = 40 if visible else (-100 if not valid or index < valid[0][0] else 800)
            return NS(x=40, y=y, width=10, height=20)

    api = NS(Text=Text, CoordType=NS(SCREEN=0), TextClipType=NS(BOTH=3))
    monkeypatch.setattr(_atspi, '_atspi', lambda: api)
    monkeypatch.setattr(_atspi, '_state_has', lambda acc, name: False)
    monkeypatch.setattr(_atspi, '_toolkit_name', lambda acc: 'gtk')
    monkeypatch.setattr(_atspi, '_state_flags', lambda acc: (True, True, None, False, None, True, True))
    monkeypatch.setattr(_atspi, '_extents', lambda acc, **kw: acc.box)
    monkeypatch.setattr(_atspi, '_raw_rect', lambda acc, coord: (*acc.box[0], *acc.box[1]))
    return node


def snapshot(node):
    raw = _atspi.ATSPIAccessor().read(node)
    assert raw.value == node.text  # Editing retains the real buffer.
    return _snap(raw)


def test_find_gtk_editor_uses_only_visible_ranges(editor):
    snap = snapshot(editor)
    assert observe.find_elements(snap, text='SHARED-001')
    assert not observe.find_elements(snap, text='LEFT-MARKER-150')
    start = editor.text.index('LEFT-MARKER-150')
    editor.visible = [(start, len(editor.text))]
    snap = snapshot(editor)
    assert observe.find_elements(snap, text='LEFT-MARKER-150')
    assert not observe.find_elements(snap, text='SHARED-001')


@pytest.mark.parametrize('ranges', [None, [], [(-1, 20)], [(0, 100000)]])
def test_find_gtk_editor_does_not_guess_visible_text(editor, ranges):
    editor.visible = ranges
    assert not observe.find_elements(snapshot(editor), text='LEFT-MARKER-150')


def test_find_gtk_editor_searches_visible_text_past_render_cap(editor):
    editor.visible = [(0, len(editor.text))]
    snap = snapshot(editor)
    assert observe.find_elements(snap, text='LEFT-MARKER-150')
    assert 'LEFT-MARKER-150' not in (snap.elements[-1].value or '')


def test_scroll_to_find_waits_for_gtk_match_to_be_visible(editor, monkeypatch):
    scrolls = []

    class Driver:
        def ensure_trusted(self):
            pass

        def snapshot(self, scope, app):
            return snapshot(editor)

        def scroll(self, target, **kwargs):
            scrolls.append(kwargs)
            start = editor.text.index('LEFT-MARKER-150')
            editor.visible = [(start, len(editor.text))]

    rt = server.Runtime.__new__(server.Runtime)
    rt.driver = Driver()
    rt._run_gated = lambda action, app, execute, **kw: execute()
    rt._require_permission = lambda *args, **kw: None
    rt._recheck_target = lambda *args: None
    monkeypatch.setattr(server, '_running_app', lambda app: (None, 'meld'))
    result = rt.scroll_to_find('meld', text='LEFT-MARKER-150', max_scrolls=2)
    assert scrolls
    assert 'found after 0 scroll(s)' not in result
    assert '1 match' in result


def test_find_gtk_editor_checks_horizontal_clipping(editor, monkeypatch):
    api = _atspi._atspi()
    marker = editor.text.index('LEFT-MARKER-150')
    monkeypatch.setattr(api.Text, 'get_character_extents', staticmethod(
        lambda acc, index, coord: NS(
            x=-100 if index >= marker else 40, y=40, width=10, height=20,
        ),
    ))
    snap = snapshot(editor)
    assert observe.find_elements(snap, text='SHARED-001')
    assert not observe.find_elements(snap, text='LEFT-MARKER-150')


def test_defunct_editor_never_reads_text_or_geometry(editor, monkeypatch):
    monkeypatch.setattr(_atspi, '_state_has', lambda acc, name: name == 'DEFUNCT')
    monkeypatch.setattr(_atspi, '_atspi', lambda: pytest.fail('defunct node queried'))
    raw = _atspi.ATSPIAccessor().read(editor)
    assert raw.value is None
    assert raw.unallocated
    assert _atspi._gtk_editor_on_screen(editor, editor.text) == ''


def test_changed_editor_does_not_query_outdated_offsets(editor, monkeypatch):
    monkeypatch.setattr(_atspi._atspi().Text, 'get_character_count', staticmethod(lambda acc: 1))
    monkeypatch.setattr(_atspi._atspi().Text, 'get_character_extents',
                        staticmethod(lambda *args: pytest.fail('outdated offset queried')))
    assert _atspi._gtk_editor_on_screen(editor, editor.text) == ''


def test_huge_single_line_uses_only_viewport_character_queries(editor, monkeypatch):
    editor.text = 'x' * 1000000 + 'VISIBLE'
    calls = []

    def glyph(acc, index, coord):
        assert 0 <= index < len(acc.text)
        calls.append(index)
        return NS(x=40, y=-100 if index < 1000000 else 40, width=10, height=20)

    monkeypatch.setattr(_atspi._atspi().Text, 'get_character_extents', staticmethod(glyph))
    assert _atspi._gtk_editor_on_screen(editor, editor.text) == 'VISIBLE'
    assert len(calls) < 50


@pytest.mark.parametrize('rect', [None, NS(x=0, y=float('nan'), width=10, height=20),
                                  NS(x=40, y=40, width=10, height=0)])
def test_invalid_character_geometry_fails_closed(editor, monkeypatch, rect):
    monkeypatch.setattr(_atspi._atspi().Text, 'get_character_extents', staticmethod(lambda *args: rect))
    assert _atspi._gtk_editor_on_screen(editor, editor.text) == ''


def test_editor_becoming_defunct_aborts_geometry_reads(editor, monkeypatch):
    calls = []
    def glyph(acc, index, coord):
        calls.append(index)
        return NS(x=40, y=40, width=10, height=20)
    monkeypatch.setattr(_atspi._atspi().Text, 'get_character_extents', staticmethod(glyph))
    monkeypatch.setattr(_atspi, '_state_has', lambda acc, name: name == 'DEFUNCT' and bool(calls))
    assert _atspi._gtk_editor_on_screen(editor, editor.text) == ''
    assert len(calls) == 1


def test_unbounded_viewport_geometry_fails_closed(editor, monkeypatch):
    editor.text = 'x' * 1000000
    calls = []
    def glyph(acc, index, coord):
        calls.append(index)
        return NS(x=40, y=40, width=10, height=20)
    monkeypatch.setattr(_atspi._atspi().Text, 'get_character_extents', staticmethod(glyph))
    assert _atspi._gtk_editor_on_screen(editor, editor.text) == ''
    assert len(calls) < 50
