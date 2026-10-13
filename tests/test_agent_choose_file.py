"""choose_file types a path into an open chooser and does not report a false success.

Hermetic: a fake desktop, not a live GTK dialog. The live GTK run is
``test_agent_choose_file_opens_a_gtk_dialog``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from a11y_computer_use.agent import Agent, tool_schemas
from a11y_computer_use.agent.actions import Action, validate_action
from a11y_computer_use.agent.choose_file import (
    ChooseIO,
    entered_path,
    file_shown,
    perform_choose_file,
)
from a11y_computer_use.agent.models.base import ModelTurn, ToolCall
from a11y_computer_use.agent.models.scripted import ScriptedModel
from a11y_computer_use.observe import render_text
from a11y_computer_use.outcome import ActionResult
from a11y_computer_use.schema import Bounds, Element, Scope, Snapshot


def _node(ref, role, title, **kwargs):
    return SimpleNamespace(
        ref=ref,
        role=role,
        title=title,
        value=kwargs.get("value"),
        editable=kwargs.get("editable", False),
        focused=kwargs.get("focused", False),
        parent=kwargs.get("parent"),
    )


def _snap(*elements):
    return SimpleNamespace(elements=elements)


def _confirmed(path: str) -> ActionResult:
    return ActionResult("typed", outcome="confirmed", evidence=f"read back {path!r}")


class _Desk:
    """One chooser window and a host label. ``show`` is what the host says after Return."""

    def __init__(self, path: str, *, close: bool = True, show: bool = True, type_ok: bool = True,
                 titles: list[str] | None = None, entry: bool = False):
        self.path = path
        self.close = close
        self.show = show
        self.type_ok = type_ok
        self.titles = list(titles or ["Open File"])
        self.entry = entry
        self.open = True
        self.label = "chosen="
        self.calls: list[tuple[str, object]] = []
        self.set_values: list[tuple[str, str]] = []

    def windows(self):
        rows = []
        if self.open:
            for index, title in enumerate(self.titles):
                rows.append({"window_id": 7 + index, "app": "python3", "title": title})
        rows.append({"window_id": 3, "app": "python3", "title": "FileHost"})
        return rows

    def focus(self, window_id: int) -> None:
        self.calls.append(("focus", window_id))

    def key(self, chord: str):
        self.calls.append(("key", chord))
        if chord == "Return" and self.close:
            self.open = False
            self.label = f"chosen={self.path}" if self.show else "chosen="
        return ActionResult(f"pressed {chord}", outcome="confirmed", evidence="pressed")

    def type_text(self, text: str):
        self.calls.append(("type", text))
        if not self.type_ok:
            return ActionResult("typed", outcome="suspected_noop", evidence="the value did not change")
        return _confirmed(text if text == self.path else text + "-extra")

    def set_value(self, ref: str, value: str):
        self.calls.append(("set_value", (ref, value)))
        self.set_values.append((ref, value))
        return _confirmed(value)

    def snapshot(self, app: str):
        del app
        host = _node("e1", "AXWindow", "FileHost")
        label = _node("e4", "AXLabel", self.label, parent="e1")
        elements = [host, label]
        if self.entry:
            elements.append(_node(
                "e9", "AXTextField", "Location", value="/tmp", editable=True, focused=True, parent="e2",
            ))
        if self.open:
            dialog = _node("e2", "AXDialog", "Open File", parent="e1")
            row = _node("e3", "AXStaticText", "picked.txt", parent="e2")
            elements.extend([dialog, row])
        return _snap(*elements)

    def io(self) -> ChooseIO:
        return ChooseIO(
            windows=self.windows,
            focus=self.focus,
            key=self.key,
            type_text=self.type_text,
            set_value=self.set_value,
            snapshot=self.snapshot,
        )


def test_choose_file_arguments_require_an_absolute_existing_file(tmp_path) -> None:
    target = tmp_path / "picked.txt"
    missing = tmp_path / "missing.txt"
    target.write_text("picked")
    assert validate_action(Action("choose_file", {"path": str(target)})) is None
    assert validate_action(Action("choose_file", {"path": str(missing), "action": "save"})) is None
    assert validate_action(Action("choose_file", {"path": str(missing)})) == (
        "invalid_arguments: choose_file: open requires an existing file"
    )
    assert validate_action(Action("choose_file", {"path": "picked.txt"})) == (
        "invalid_arguments: choose_file: path must be an absolute path"
    )
    assert validate_action(Action("choose_file", {"path": str(tmp_path)})) == (
        "invalid_arguments: choose_file: open requires an existing file"
    )
    assert validate_action(Action("choose_file", {})) == (
        "invalid_arguments: choose_file: path is required"
    )
    assert validate_action(Action("choose_file", {"path": str(target), "action": "delete"})) == (
        "invalid_arguments: choose_file: action must be open or save"
    )
    assert validate_action(Action("choose_file", {"path": str(target) + "\n"})) == (
        "invalid_arguments: choose_file: path must be an absolute path"
    )
    assert validate_action(Action("choose_file", {"path": str(target), "zoom": 1})) == (
        "invalid_arguments: choose_file: unknown field 'zoom'; expected action, path"
    )
    schema = next(item for item in tool_schemas() if item["name"] == "choose_file")
    assert schema["parameters"]["required"] == ["path"]
    assert schema["parameters"]["additionalProperties"] is False
    assert "never navigate folders" in schema["description"]


def test_entered_path_rejects_a_longer_read_back(tmp_path) -> None:
    path = str(tmp_path / "picked.txt")
    assert entered_path(_confirmed(path), path) is True
    # Path.as_uri() is file:///C:/... on Windows and file:///tmp/... on Linux.
    # Pasting the native path onto "file://" is not a URI on Windows.
    assert entered_path(_confirmed(tmp_path.joinpath("picked.txt").as_uri()), path) is True
    doubled = str(tmp_path) + path
    assert entered_path(_confirmed(doubled), path) is False
    assert entered_path(
        ActionResult("typed", outcome="confirmed", evidence="the accessibility state changed"),
        path,
    ) is False
    assert entered_path(
        ActionResult("typed", outcome="suspected_noop", evidence=f"read back {path!r}"),
        path,
    ) is False


def test_file_list_inside_the_chooser_is_not_the_chosen_file(tmp_path) -> None:
    path = str(tmp_path / "picked.txt")
    host = _node("e1", "AXWindow", "FileHost")
    dialog = _node("e2", "AXDialog", "Open File", parent="e1")
    row = _node("e3", "AXStaticText", "picked.txt", parent="e2")
    label = _node("e4", "AXLabel", "chosen=", parent="e1")
    snap = _snap(host, dialog, row, label)
    assert file_shown(snap, [], path) is False
    label.title = "chosen=" + path
    # The name on the host counts. The same name inside the dialog does not.
    assert file_shown(snap, [], path) is True
    assert file_shown(snap, [], str(tmp_path / "notpicked.txt")) is False
    page = _snap(
        host,
        _node("e5", "AXButton", "Upload: No file chosen", parent="e1"),
        _node("e6", "AXStaticText", "see picked.txt in the title", parent="e1"),
    )
    assert file_shown(page, [], path) is False


def test_choose_file_types_the_path_and_requires_the_dialog_to_close(tmp_path) -> None:
    path = str(tmp_path / "picked.txt")
    desk = _Desk(path)
    result = perform_choose_file(desk.io(), path, apps=["python3"], timeout_s=0)
    assert result.ok is True
    assert result.message == f"chose {path}"
    assert ("key", "ctrl+l") in desk.calls
    assert ("type", path) in desk.calls
    assert ("key", "Return") in desk.calls
    assert desk.set_values == []
    assert all(name != "file_dialog" for name, _payload in desk.calls)


def test_choose_file_does_not_press_return_when_the_path_did_not_land(tmp_path) -> None:
    path = str(tmp_path / "picked.txt")
    desk = _Desk(path, type_ok=False)
    result = perform_choose_file(desk.io(), path, apps=["python3"], timeout_s=0)
    assert result.ok is False
    assert result.reason == "path_not_entered"
    assert ("key", "Return") not in desk.calls


def test_choose_file_uses_one_set_value_when_typing_misses(tmp_path) -> None:
    path = str(tmp_path / "picked.txt")
    desk = _Desk(path, type_ok=False, entry=True)
    result = perform_choose_file(desk.io(), path, apps=["python3"], timeout_s=0)
    assert result.ok is True
    assert desk.set_values == [("e9", path)]
    assert ("key", "Return") in desk.calls


def test_choose_file_fails_when_the_dialog_stays_open(tmp_path) -> None:
    path = str(tmp_path / "picked.txt")
    desk = _Desk(path, close=False, show=True)
    result = perform_choose_file(desk.io(), path, apps=["python3"], timeout_s=0)
    assert result.ok is False
    assert result.reason == "file_chooser_unchanged"


def test_choose_file_fails_when_the_dialog_closes_without_the_file(tmp_path) -> None:
    path = str(tmp_path / "picked.txt")
    desk = _Desk(path, close=True, show=False)
    result = perform_choose_file(desk.io(), path, apps=["python3"], timeout_s=0)
    assert result.ok is False
    assert result.reason == "file_not_chosen"


def test_choose_file_does_not_type_when_no_chooser_is_open(tmp_path) -> None:
    path = str(tmp_path / "picked.txt")
    desk = _Desk(path, titles=[])
    desk.open = False
    result = perform_choose_file(desk.io(), path, apps=["python3"], timeout_s=0)
    assert result.ok is False
    assert result.reason == "no_file_chooser"
    assert desk.calls == []


def test_choose_file_does_not_type_when_two_choosers_are_open(tmp_path) -> None:
    path = str(tmp_path / "picked.txt")
    desk = _Desk(path, titles=["Open File", "Save File"])
    result = perform_choose_file(desk.io(), path, apps=["python3"], timeout_s=0)
    assert result.ok is False
    assert result.reason == "no_file_chooser"
    assert "more than one" in result.message
    assert desk.calls == []


def test_qt_open_title_counts_only_with_chooser_controls(tmp_path) -> None:
    path = str(tmp_path / "picked.txt")
    desk = _Desk(path, titles=["Open"])

    def snapshot(app: str):
        del app
        host = _node("e1", "AXWindow", "Open")
        if not desk.entry:
            return _snap(host, _node("e4", "AXLabel", "a document", parent="e1"))
        return _snap(
            host,
            _node("e5", "AXButton", "Cancel", parent="e1"),
            _node("e6", "AXButton", "Open", parent="e1"),
            _node("e9", "AXTextField", "Location", value="/tmp", editable=True, focused=True, parent="e1"),
            _node("e4", "AXLabel", desk.label, parent="e1"),
        )

    desk.snapshot = snapshot  # type: ignore[method-assign]
    missed = perform_choose_file(desk.io(), path, apps=["python3"], timeout_s=0)
    assert missed.reason == "no_file_chooser"
    assert desk.calls == []
    desk.entry = True
    desk.type_ok = True
    landed = perform_choose_file(desk.io(), path, apps=["python3"], timeout_s=0)
    assert landed.ok is True
    assert ("type", path) in desk.calls


def _element(ref, role, title, **kwargs) -> Element:
    return Element(
        ref=ref,
        role=role,
        title=title,
        value=kwargs.get("value"),
        bounds=Bounds(0, 10, 10, 40, 20),
        snapshot_id="snap",
        parent=kwargs.get("parent"),
        editable=kwargs.get("editable", False),
        focused=kwargs.get("focused", False),
    )


class _Runtime:
    """Fake runtime whose screen changes on Return without the file being chosen."""

    def __init__(self, path: str, *, accept: bool):
        self.path = path
        self.accept = accept
        self.calls: list[tuple[str, dict]] = []
        self._current: Snapshot | None = None
        self.driver = SimpleNamespace(name="fake")
        self.open = True
        self.label = "idle"

    def _frontmost(self) -> str:
        return "python3"

    def desktop_snapshot(self, app, **kwargs) -> str:
        del app, kwargs
        elements = [
            _element("e1", "AXWindow", "FileHost"),
            _element("e4", "AXLabel", self.label, parent="e1"),
        ]
        if self.open:
            elements.append(_element("e2", "AXDialog", "Open File", parent="e1"))
            elements.append(_element("e3", "AXStaticText", "picked.txt", parent="e2"))
        self._current = Snapshot("snap", Scope.APP, "python3", 1, 0.0, (), tuple(elements))
        return render_text(self._current)

    def call_tool(self, name, params, confirm=None):
        del confirm
        self.calls.append((name, dict(params)))
        if name == "file_dialog":
            raise AssertionError("file_dialog is not the choose_file macro")
        if name == "window" and params.get("action") == "list":
            rows = [{"window_id": 3, "app": "python3", "title": "FileHost"}]
            if self.open:
                rows.insert(0, {"window_id": 7, "app": "python3", "title": "Open File"})
            return ActionResult(json.dumps(rows), outcome="confirmed", evidence="listed")
        if name == "window" and params.get("action") == "focus":
            return ActionResult("focused", outcome="confirmed", evidence="focused")
        if name == "key" and params.get("chord") == "ctrl+l":
            return ActionResult("pressed ctrl+l", outcome="confirmed", evidence="pressed")
        if name == "type":
            return _confirmed(str(params.get("text")))
        if name == "key" and params.get("chord") == "Return":
            # The dialog closes either way, and the screen changes either way.
            # Only accept=True shows the chosen file. A digest change is not success.
            self.open = False
            self.label = f"chosen={self.path}" if self.accept else "chosen="
            return ActionResult("pressed Return", outcome="confirmed", evidence="the accessibility state changed")
        return f"{name} ok"

    def screenshot(self, **kwargs):
        del kwargs
        return ("shot", SimpleNamespace(png=b"\x89PNG\r\n\x1a\nfake"))


def _run(runtime, path: str, tmp_path):
    def model(_messages):
        return ModelTurn(calls=[ToolCall("choose_file", {"path": path})])

    agent = Agent(
        ScriptedModel(model),
        runtime=runtime,
        max_steps=1,
        max_time_s=30,
        trace_dir=tmp_path / "trace",
        vision=False,
        look_again=False,
    )
    return agent.run("choose the file"), agent


def test_agent_choose_file_is_not_verified_when_the_screen_changes_without_the_file(tmp_path) -> None:
    path = str(tmp_path / "picked.txt")
    (tmp_path / "picked.txt").write_text("picked")
    runtime = _Runtime(path, accept=False)
    result, agent = _run(runtime, path, tmp_path)
    step = result.step_log[0]
    assert step.action == "choose_file"
    assert step.verified is False
    assert step.error and "file_not_chosen" in step.error
    assert result.status != "success"
    assert "never navigate folders" in agent._messages[0].content
    assert not any(name == "file_dialog" for name, _params in runtime.calls)


def test_agent_choose_file_is_verified_only_when_the_file_is_shown(tmp_path) -> None:
    path = str(tmp_path / "picked.txt")
    (tmp_path / "picked.txt").write_text("picked")
    runtime = _Runtime(path, accept=True)
    result, _agent = _run(runtime, path, tmp_path)
    step = result.step_log[0]
    assert step.verified is True
    assert step.error is None
    assert path in step.result
    chords = [params.get("chord") for name, params in runtime.calls if name == "key"]
    assert chords == ["ctrl+l", "Return"]
    assert not any(name == "file_dialog" for name, _params in runtime.calls)


def test_choose_file_rejects_a_stale_chooser_without_input(tmp_path) -> None:
    desk = _Desk(str(tmp_path / "picked.txt"))
    desk.windows = lambda: [{"window_id": 3, "app": "python3", "title": "FileHost"}]
    io = desk.io()
    io.sleep = lambda _seconds: (_ for _ in ()).throw(AssertionError("waited"))
    result = perform_choose_file(io, desk.path, apps=["python3"])
    assert result.reason == "no_file_chooser"
    assert "open the file dialog first" in result.message
    assert "Ctrl+O" in result.message
    assert desk.calls == []


def test_choose_file_rejects_unrelated_open_cancel_buttons(tmp_path) -> None:
    desk = _Desk(str(tmp_path / "picked.txt"), titles=["Open"])
    desk.snapshot = lambda _app: _snap(
        _node("e1", "AXWindow", "Open"),
        _node("e2", "AXWindow", "Preferences"),
        _node("e3", "AXButton", "Open", parent="e2"),
        _node("e4", "AXButton", "Cancel", parent="e2"),
        _node("e5", "AXTextField", "Name", editable=True, parent="e2"),
    )
    result = perform_choose_file(desk.io(), desk.path, apps=["python3"])
    assert result.reason == "no_file_chooser"
    assert desk.calls == []


def test_agent_choose_file_without_dialog_sends_no_input(tmp_path) -> None:
    target = tmp_path / "picked.txt"
    target.write_text("picked")
    runtime = _Runtime(str(target), accept=False)
    runtime.open = False
    result, _agent = _run(runtime, str(target), tmp_path)
    step = result.step_log[0]
    assert step.verified is False
    assert step.error and "no_file_chooser" in step.error
    assert "open the file dialog first" in step.error
    assert "file_not_chosen" not in step.error
    assert not any(name in {"key", "type", "set_value"} for name, _ in runtime.calls)


def test_ordinary_open_window_does_not_match_a_stale_other_chooser(tmp_path) -> None:
    desk = _Desk(str(tmp_path / "picked.txt"), titles=["Open"])
    # The tree still contains Open File, but the only mapped candidate is Open.
    result = perform_choose_file(desk.io(), desk.path, apps=["python3"])
    assert result.reason == "no_file_chooser"
    assert desk.calls == []


class _OfficeDesk(_Desk):
    """VCL's Open dialog: Ctrl+L does not expose a location entry."""

    def __init__(self, path, *, focused=True, read_back=True, **kwargs):
        super().__init__(path, titles=["Open"], **kwargs)
        self.filename_focused = focused
        self.read_back = read_back

    def windows(self):
        return [{**row, "app": "soffice"} for row in super().windows()]

    def snapshot(self, app):
        host = _node("e1", "AXWindow", "LibreOffice Calc")
        label = _node("e4", "AXLabel", self.label, parent="e1")
        if not self.open:
            return _snap(host, label)
        return _snap(
            host, label,
            _node("e2", "AXDialog", "Open", parent="e1"),
            _node("e5", "AXButton", "Open", parent="e2"),
            _node("e6", "AXButton", "Cancel", parent="e2"),
            _node("e9", "AXTextField", "File name", editable=True,
                  focused=self.filename_focused and ("key", "alt+n") in self.calls,
                  parent="e2"),
        )

    def set_value(self, ref, value):
        result = super().set_value(ref, value)
        return result if self.read_back else _confirmed(value + "-extra")


def test_choose_file_drives_libreoffice_filename_field(tmp_path) -> None:
    desk = _OfficeDesk(str(tmp_path / "data (20).csv"))
    result = perform_choose_file(desk.io(), desk.path, apps=["soffice"], timeout_s=0)
    assert result.ok
    assert desk.calls == [
        ("focus", 7), ("key", "alt+n"),
        ("set_value", ("e9", desk.path)), ("key", "Return"),
    ]
    assert "dialog closed" in result.evidence


def test_libreoffice_requires_filename_focus_and_exact_readback(tmp_path) -> None:
    for kwargs in ({"focused": False}, {"read_back": False}):
        desk = _OfficeDesk(str(tmp_path / "data.csv"), **kwargs)
        result = perform_choose_file(desk.io(), desk.path, apps=["soffice"], timeout_s=0)
        assert not result.ok
        assert result.reason == "path_not_entered"
        assert "File name field" in result.message
        assert ("key", "Return") not in desk.calls
        if not desk.filename_focused:
            assert desk.set_values == []


def test_libreoffice_requires_closed_dialog_and_file_evidence(tmp_path) -> None:
    for kwargs, reason in (({"close": False}, "file_chooser_unchanged"),
                           ({"show": False}, "file_not_chosen")):
        desk = _OfficeDesk(str(tmp_path / "data.csv"), **kwargs)
        result = perform_choose_file(desk.io(), desk.path, apps=["soffice"], timeout_s=0)
        assert not result.ok
        assert result.reason == reason


def test_libreoffice_does_not_use_filename_field_outside_chooser(tmp_path) -> None:
    desk = _OfficeDesk(str(tmp_path / "data.csv"))
    original = desk.snapshot

    def snapshot(app):
        snap = original(app)
        for element in snap.elements:
            if element.ref == "e9":
                element.title = "Search"
        return _snap(*snap.elements,
                     _node("e10", "AXTextField", "File name", editable=True,
                           focused=True, parent="e1"))

    desk.snapshot = snapshot
    result = perform_choose_file(desk.io(), desk.path, apps=["soffice"], timeout_s=0)
    assert result.reason == "path_not_entered"
    assert desk.set_values == []
    assert ("key", "Return") not in desk.calls
