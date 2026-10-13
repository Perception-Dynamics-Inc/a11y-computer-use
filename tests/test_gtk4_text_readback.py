"""GTK 4 Text rejects -1 as the end offset; synthetic AT-SPI regression."""

import pytest

from a11y_computer_use.drivers import _atspi, _linux_input


class _Text:
    @staticmethod
    def get_character_count(acc):
        return len(acc.text)

    @staticmethod
    def get_text(acc, start, end):
        acc.reads.append((start, end))
        return "" if end == -1 else acc.text[start:end]


class _Api:
    Text = _Text


class _Entry:
    def __init__(self, role, text):
        self.role = role
        self.text = text
        self.reads = []
        self.writes = []

    def get_role_name(self):
        return self.role

    def get_editable_text_iface(self):
        return self

    def set_text_contents(self, text):
        self.writes.append(text)
        self.text = text
        return True


@pytest.mark.parametrize("role", ["entry", "text"])
@pytest.mark.parametrize("original", ["", "old value"])
def test_gtk4_replace_verifies_one_write_without_focus_or_keys(monkeypatch, role, original):
    monkeypatch.setattr(_atspi, "_atspi", lambda: _Api)
    field = _Entry(role, original)

    def unexpected(*args, **kwargs):
        pytest.fail("a verified EditableText replace must not focus or type")

    monkeypatch.setattr(_atspi, "grab_focus", unexpected)
    monkeypatch.setattr(_linux_input, "type_string", unexpected)
    assert _atspi.set_text(field, "SRCH1") is True
    assert field.text == "SRCH1"
    assert field.writes == ["SRCH1"]
    assert _atspi._value_text(field, "AXTextField", role) == "SRCH1"
    assert _atspi._full_text(field) == "SRCH1"
    assert (0, 5) in field.reads


def test_gtk4_readback_tracks_changed_text_and_empty_field(monkeypatch):
    monkeypatch.setattr(_atspi, "_atspi", lambda: _Api)
    field = _Entry("entry", "TYPE1")
    assert _atspi._full_text(field) == "TYPE1"
    field.text = "TYPE2"
    assert _atspi._full_text(field) == "TYPE2"
    assert not _atspi._confirm_text(field, "TYPE1")
    field.text = ""
    field.reads.clear()
    assert _atspi._full_text(field) == ""
    assert field.reads == [(0, -1)]


def test_nonempty_sentinel_read_wins_over_bounded_write_echo(monkeypatch):
    class EchoText(_Text):
        @staticmethod
        def get_text(acc, start, end):
            acc.reads.append((start, end))
            return "actual old text" if end == -1 else "requested"

    class Api:
        Text = EchoText

    monkeypatch.setattr(_atspi, "_atspi", lambda: Api)
    field = _Entry("entry", "requested")
    assert _atspi._full_text(field) == "actual old text"
    assert _atspi._value_text(field, "AXTextField", "entry") == "actual old text"
    assert field.reads == [(0, -1), (0, -1)]
