"""Caret insertion, UTF-8 insert length, and disabled-ref refusals.

Hermetic. The AT-SPI objects are fakes. A byte-length fake cuts the UTF-8
bytes the way GTK does; a character-length fake slices the Python string.
No display, no bus, no browser.
"""

from __future__ import annotations

import json
from contextlib import nullcontext

import pytest

from a11y_computer_use import safety, server
from a11y_computer_use.drivers import _atspi, _linux_input, _linux_system
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Bounds, ComputerUseError, Element, ErrorCode, Point, Scope, Snapshot, Display


class _Text:
    @staticmethod
    def get_character_count(acc):
        return len(acc.text)

    @staticmethod
    def get_text(acc, start, end):
        if getattr(acc, "text_error", False):
            raise RuntimeError("unreadable")
        if int(end) < 0:
            return acc.text
        return acc.text[int(start):int(end)]

    @staticmethod
    def get_caret_offset(acc):
        return int(acc.caret)

    @staticmethod
    def get_n_selections(acc):
        sel = getattr(acc, "selection", None)
        if not isinstance(sel, tuple) or len(sel) != 2:
            return 0
        return 1 if int(sel[1]) > int(sel[0]) else 0

    @staticmethod
    def get_selection(acc, _n):
        return getattr(acc, "selection", None)


class _Atspi:
    Text = _Text


class _Field:
    def __init__(self, text="", caret=None, selection=None):
        self.text = text
        self.caret = len(text) if caret is None else caret
        self.selection = selection
        self.lengths: list[int] = []

    def get_editable_text_iface(self):
        return self

    def clear_cache(self):
        return None

    def delete_text(self, start, end):
        self.text = self.text[:int(start)] + self.text[int(end):]
        self.caret = int(start)
        self.selection = None
        return True


class _ByteField(_Field):
    """GTK: length is a UTF-8 byte count. A cut code point inserts nothing."""

    def insert_text(self, pos, text, length):
        self.lengths.append(int(length))
        piece = text.encode("utf-8")[:int(length)]
        try:
            chunk = piece.decode("utf-8")
        except UnicodeDecodeError:
            return True
        pos = int(pos)
        self.text = self.text[:pos] + chunk + self.text[pos:]
        self.caret = pos + len(chunk)
        self.selection = None
        return True


_ByteField.insert_text._length_unit = "bytes"


class _CharField(_Field):
    """A binding whose length argument is a character count."""

    def insert_text(self, pos, text, length):
        self.lengths.append(int(length))
        chunk = text[:int(length)]
        pos = int(pos)
        self.text = self.text[:pos] + chunk + self.text[pos:]
        self.caret = pos + len(chunk)
        self.selection = None
        return True


class _IgnoreField(_Field):
    """insert_text returns true and leaves the buffer alone."""

    def insert_text(self, pos, text, length):
        self.lengths.append(int(length))
        return True


@pytest.fixture
def atspi(monkeypatch):
    monkeypatch.setattr(_atspi, "_atspi", lambda: _Atspi)
    monkeypatch.setattr(_atspi.time, "sleep", lambda _seconds: None)


def test_byte_length_inserts_the_whole_unicode_string(atspi) -> None:
    samples = ["Hello world", "naïve café", "Привет", "中文字", "ok 😀", "ü✓Привет 中文"]
    for sample in samples:
        field = _ByteField()
        assert _atspi.insert_text(field, sample) == len(sample)
        assert field.text == sample
        assert field.lengths == [len(sample.encode("utf-8"))]


def test_character_length_binding_inserts_the_whole_string(atspi) -> None:
    field = _CharField()
    assert _atspi.insert_text(field, "Привет") == 6
    assert field.text == "Привет"
    assert field.lengths == [6]


def test_a_short_byte_length_would_truncate_or_drop_the_text(atspi) -> None:
    """The old character count, applied as a byte count, is the reported bug."""
    field = _ByteField()
    field.insert_text(0, "Привет", len("Привет"))
    assert field.text == "При"
    field = _ByteField()
    field.insert_text(0, "中文字", len("中文字"))
    assert field.text == "中"
    field = _ByteField()
    field.insert_text(0, "ok 😀", len("ok 😀"))
    assert field.text == ""


def test_insert_at_the_caret_and_over_a_selection(atspi) -> None:
    field = _ByteField("hello ", caret=0)
    assert _atspi.insert_text(field, "world") == 5
    assert field.text == "worldhello "

    field = _ByteField("world", caret=0)
    assert _atspi.insert_text(field, "hello ") == 6
    assert field.text == "hello world"

    field = _ByteField("keep DROP keep", caret=9, selection=(5, 9))
    assert _atspi.insert_text(field, "NEW") == 3
    assert field.text == "keep NEW keep"


def test_crlf_inserts_one_newline(atspi) -> None:
    field = _ByteField()
    assert _atspi.insert_text(field, "a\r\nb") == 3
    assert field.text == "a\nb"


def test_read_back_mismatch_is_an_error(atspi) -> None:
    field = _IgnoreField("old", caret=3)
    with pytest.raises(ComputerUseError) as exc:
        _atspi.insert_text(field, "Привет")
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "text_mismatch"
    assert field.text == "old"


def test_selection_that_cannot_be_deleted_is_not_then_appended(atspi) -> None:
    field = _ByteField("keep DROP keep", caret=9, selection=(5, 9))

    def delete_text(start, end):
        return True

    field.delete_text = delete_text  # type: ignore[method-assign]
    with pytest.raises(ComputerUseError) as exc:
        _atspi.insert_text(field, "NEW")
    assert exc.value.detail["reason"] == "selection_not_replaced"
    assert field.text == "keep DROP keep"
    assert field.lengths == []


def test_focused_editable_without_pygobject_is_not_a_crash(monkeypatch) -> None:
    def missing(*_args, **_kwargs):
        raise ImportError("PyGObject is not installed")

    monkeypatch.setattr(_atspi, "_focused_node", missing)
    assert _atspi.focused_editable("editor") is None


def test_coordinate_click_types_through_the_focused_editable(atspi, monkeypatch) -> None:
    """No remembered ref: look up the focused field and use insert_text."""
    driver = LinuxDriver()
    driver._focused_editable = None
    field = _ByteField()
    typed: list[str] = []
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr("a11y_computer_use.drivers.linux._on_wayland", lambda: False)
    monkeypatch.setattr(driver, "frontmost_app", lambda: ("editor", 1))
    monkeypatch.setattr(_atspi, "is_secure", lambda acc: False)
    monkeypatch.setattr(_atspi, "focused_secure", lambda app: False)
    monkeypatch.setattr(_atspi, "focused_editable", lambda app: field if app == "editor" else None)
    monkeypatch.setattr(_linux_input, "type_string", lambda text: typed.append(text))
    monkeypatch.setattr(_linux_input, "held", lambda modifiers: nullcontext())
    clicks: list[tuple] = []
    monkeypatch.setattr(_linux_input, "click", lambda *args, **kwargs: clicks.append((args, kwargs)))

    driver._focused_editable = object()
    driver.click(Point(0, 40, 12))
    assert driver._focused_editable is None and clicks

    for sample in ("Привет", "中文字", "ok 😀", "ab ✓ ok"):
        field.text = ""
        field.caret = 0
        field.lengths = []
        assert driver.type_text(sample) == len(sample)
        assert field.text == sample
        assert field.lengths == [len(sample.encode("utf-8"))]
    assert typed == []

    field.text = "world"
    field.caret = 0
    assert driver.type_text("hello ") == 6
    assert field.text == "hello world"

    monkeypatch.setattr(_atspi, "focused_editable", lambda app: None)
    assert driver.type_text("plain") == 5
    assert typed == ["plain"]
    assert field.text == "hello world"


def test_focused_password_is_refused_before_insert_or_keys(atspi, monkeypatch) -> None:
    driver = LinuxDriver()
    field = _ByteField()
    typed: list[str] = []
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr("a11y_computer_use.drivers.linux._on_wayland", lambda: False)
    monkeypatch.setattr(driver, "frontmost_app", lambda: ("editor", 1))
    monkeypatch.setattr(_atspi, "is_secure", lambda acc: True)
    monkeypatch.setattr(_atspi, "focused_editable", lambda app: field)
    monkeypatch.setattr(_linux_input, "type_string", lambda text: typed.append(text))
    with pytest.raises(ComputerUseError) as exc:
        driver.type_text("secret")
    assert exc.value.code is ErrorCode.SECURE_FIELD
    assert field.text == "" and field.lengths == [] and typed == []


def test_linux_type_uses_the_inserted_count_and_keeps_the_key_path(monkeypatch) -> None:
    driver = LinuxDriver()
    driver._focused_editable = object()
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr("a11y_computer_use.drivers.linux._on_wayland", lambda: False)
    monkeypatch.setattr(driver, "frontmost_app", lambda: ("editor", 1))
    monkeypatch.setattr(_atspi, "is_secure", lambda acc: False)
    monkeypatch.setattr(_atspi, "focused_secure", lambda app: False)
    monkeypatch.setattr(_atspi, "pid_of", lambda acc: 7)
    monkeypatch.setattr(_linux_system, "_comm_for_pid", lambda pid: "editor")
    seen: list[str] = []
    typed: list[str] = []
    monkeypatch.setattr(_atspi, "insert_text", lambda acc, text: seen.append(text) or len(text))
    monkeypatch.setattr(_linux_input, "type_string", lambda text: typed.append(text))

    assert driver.type_text("a\r\nb") == 3
    assert seen == ["a\nb"] and typed == []

    monkeypatch.setattr(_atspi, "insert_text", lambda acc, text: None)
    assert driver.type_text("a\r\nb") == 3
    assert typed == ["a\nb"]

    def mismatch(acc, text):
        raise ComputerUseError(
            ErrorCode.UNSUPPORTED, "nope", detail={"reason": "text_mismatch"},
        )

    monkeypatch.setattr(_atspi, "insert_text", mismatch)
    with pytest.raises(ComputerUseError) as exc:
        driver.type_text("Привет")
    assert exc.value.detail["reason"] == "text_mismatch"
    assert typed == ["a\nb"]


def _element(ref: str, role: str, title: str, *, enabled: bool = True, editable: bool = False) -> Element:
    return Element(
        ref=ref,
        role=role,
        title=title,
        value=None,
        bounds=Bounds(0, 10, 20, 80, 24),
        snapshot_id="snap-1",
        enabled=enabled,
        editable=editable,
        clickable=True,
    )


class _Input:
    name = "linux"
    resolves_apps = False

    def __init__(self, elements: list[Element]) -> None:
        self.elements = elements
        self.calls: list[str] = []

    def resolve_ref(self, snap, ref, live=None):
        return snap.element(ref)

    def press_element(self, element):
        self.calls.append(f"press:{element.ref}")
        return True

    def click(self, target, **kwargs):
        self.calls.append("click")

    def hover(self, target, **kwargs):
        self.calls.append("hover")

    def scroll(self, target, **kwargs):
        self.calls.append("scroll")

    def scroll_into_view(self, element):
        self.calls.append("scroll_into_view")
        return False

    def drag(self, start, end, **kwargs):
        self.calls.append("drag")

    def set_value(self, element, value):
        self.calls.append("set_value")
        return True

    def type_text(self, text, **kwargs):
        self.calls.append("type")
        return len(text.replace("\r\n", "\n"))


def _runtime(tmp_path, driver, monkeypatch):
    monkeypatch.setattr(server, "_frontmost_bundle", lambda: "demo")
    store = safety.PermissionStore(tmp_path / "permissions.json")
    store.set_tier("demo", safety.Tier.FULL)
    rt = server.Runtime(driver=driver, store=store, audit=safety.AuditLog(tmp_path / "audit"))
    rt._current = Snapshot(
        snapshot_id="snap-1",
        scope=Scope.WINDOW,
        app="demo",
        pid=1,
        created_at=0.0,
        displays=(Display(0, 800, 600, 1.0, True),),
        elements=tuple(driver.elements),
    )
    return rt


def test_disabled_refs_refuse_input_verbs_before_any_call(tmp_path, monkeypatch) -> None:
    button = _element("e1", "AXButton", "Save", enabled=False)
    field = _element("e2", "AXTextField", "Name", enabled=False, editable=True)
    other = _element("e3", "AXButton", "Cancel", enabled=True)
    driver = _Input([button, field, other])
    rt = _runtime(tmp_path, driver, monkeypatch)

    with pytest.raises(ComputerUseError) as exc:
        rt.click("e1")
    assert exc.value.code is ErrorCode.ELEMENT_DISABLED
    assert exc.value.detail["reason"] == "disabled"
    assert "e1" in exc.value.message and "Save" in exc.value.message

    for call in (
        lambda: rt.hover(ref="e1"),
        lambda: rt.scroll(ref="e1", dy=1),
        lambda: rt.drag(start_ref="e3", end_ref="e1"),
        lambda: rt.set_value("e2", "notes.txt"),
    ):
        with pytest.raises(ComputerUseError) as refused:
            call()
        assert refused.value.code is ErrorCode.ELEMENT_DISABLED
    assert driver.calls == []

    batch = json.loads(rt.act_batch([{"do": "click", "ref": "e1"}]))
    assert batch[0]["ok"] is False
    assert batch[0]["error"].startswith("element_disabled:")
    assert driver.calls == []

    assert rt.click("e3").startswith("clicked")
    assert driver.calls == ["press:e3"]


def test_type_reports_the_count_the_driver_inserted(tmp_path, monkeypatch) -> None:
    driver = _Input([_element("e1", "AXTextField", "Name")])
    rt = _runtime(tmp_path, driver, monkeypatch)
    assert rt.type_text("a\r\nb") == "typed 3 characters"
    assert driver.calls == ["type"]
