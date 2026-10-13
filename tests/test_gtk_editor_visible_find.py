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
        def get_bounded_ranges(acc, x, y, width, height, coord, clip_x, clip_y):
            assert (x, y, width, height) == (40, 40, 500, 640)
            assert (coord, clip_x, clip_y) == (0, 3, 3)
            if acc.visible is None:
                raise RuntimeError('unsupported')
            return [NS(start_offset=start, end_offset=end) for start, end in acc.visible]

    api = NS(Text=Text, CoordType=NS(SCREEN=0), TextClipType=NS(BOTH=3))
    monkeypatch.setattr(_atspi, '_atspi', lambda: api)
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
