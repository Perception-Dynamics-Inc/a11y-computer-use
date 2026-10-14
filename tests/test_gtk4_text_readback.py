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
    assert _atspi._value_text(field, "AXTextField", role) is None
    assert _atspi._full_text(field) == ""
    assert (0, 5) in field.reads


def test_gtk4_readback_tracks_changed_text_and_empty_field(monkeypatch):
    monkeypatch.setattr(_atspi, "_atspi", lambda: _Api)
    field = _Entry("entry", "TYPE1")
    assert _atspi._full_text(field) == ""
    assert _atspi._verification_matches(field, "TYPE1")
    field.text = "TYPE2"
    assert _atspi._full_text(field) == ""
    assert _atspi._verification_matches(field, "TYPE2")
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


@pytest.mark.parametrize("signal", [
    "visibility-method", "visibility-attribute", "password-role",
    "password-state", "protected-state", "password-attribute", "protected-attribute",
])
@pytest.mark.parametrize("sentinel_empty", [True, False])
def test_masking_signals_block_every_readback(monkeypatch, signal, sentinel_empty):
    from types import SimpleNamespace
    from a11y_computer_use import observe
    from a11y_computer_use.schema import Scope
    from tests.test_linux_synthetic import _geometry

    class Text(_Text):
        @staticmethod
        def get_text(acc, start, end):
            acc.reads.append((start, end))
            return "" if sentinel_empty and end == -1 else acc.text[start:None if end == -1 else end]

    Api = type("Api", (), {"Text": Text})

    monkeypatch.setattr(_atspi, "_atspi", lambda: Api)
    field = _Entry("text", "hunter2-578")
    field.get_name = lambda: "PIN"
    field.get_attributes = lambda: {}
    field.get_state_set = lambda: SimpleNamespace(names={"EDITABLE", "VISIBLE", "SHOWING"})
    if signal == "visibility-method":
        field.get_visibility = lambda: False
    elif signal == "visibility-attribute":
        field.get_attributes = lambda: {"visibility": "false"}
    elif signal == "password-role":
        field.role = "password text"
    elif signal.endswith("-state"):
        field.get_state_set = lambda: SimpleNamespace(names={signal.split("-")[0].upper(), "EDITABLE"})
    else:
        field.get_attributes = lambda: {signal.split("-")[0]: "true"}

    assert _atspi.is_secure(field)
    assert _atspi._value_text(field, "AXTextArea", field.role) is None
    assert _atspi._full_text(field) is None
    assert _atspi._readable_text(field) is None
    assert _atspi._direct_text(field) == ""
    assert not _atspi._verification_matches(field, field.text)
    assert not _atspi._confirm_text(field, field.text)
    assert not _atspi.set_text(field, "replacement")
    assert not field.writes
    raw = _atspi.ATSPIAccessor().read(field)
    assert raw.role == "AXSecureTextField"
    assert raw.value is None
    snap = observe.build_snapshot(field, _atspi.ATSPIAccessor(), scope=Scope.WINDOW,
                                  app="test", pid=1, geometry=_geometry())
    assert not observe.find_elements(snap, text="hunter2")
    assert all(el.value is None for el in snap.elements)
    assert field.reads == []


def test_unmarked_gtk4_masked_entry_never_exposes_bounded_text(monkeypatch):
    from a11y_computer_use import observe
    from a11y_computer_use.schema import Scope
    from tests.test_linux_synthetic import _geometry

    monkeypatch.setattr(_atspi, "_atspi", lambda: _Api)
    field = _Entry("text", "hunter2-578")
    field.get_name = lambda: "PIN"
    assert not _atspi.is_secure(field)
    assert _atspi._full_text(field) == ""
    assert _atspi._readable_text(field) == ""
    assert _atspi._value_text(field, "AXTextArea", "text") is None
    snap = observe.build_snapshot(field, _atspi.ATSPIAccessor(), scope=Scope.WINDOW,
                                  app="test", pid=1, geometry=_geometry())
    assert not observe.find_elements(snap, text="hunter2")
    assert all(el.value is None for el in snap.elements)
    assert all(end == -1 for _, end in field.reads)
    assert _atspi._verification_matches(field, field.text)
    assert _atspi._full_text(field) == ""
