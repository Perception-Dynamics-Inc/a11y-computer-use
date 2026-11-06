"""#1427: name Chrome's Download multiple files prompt. No live Chrome required."""
from types import SimpleNamespace

import pytest

from a11y_computer_use import server
from a11y_computer_use.drivers import _atspi, _linux_input
from a11y_computer_use.schema import Scope
from tests.test_linux_synthetic import (
    _Acc, _adopt, _bare_runtime, _wire_gtk_tree, fake_atspi,
)


@pytest.mark.parametrize("case", [
    "permission", "duplicate", "in_page_permission",
    "history", "page_words", "page_dialog",
])
def test_download_multiple_files_prompt_is_named_and_not_success(
    fake_atspi, monkeypatch, case,
):
    """A doAction press that leaves the permission dialog up is not a saved download.

    The permission dialog is the bubble Chrome showed for the second blob
    download: ``<origin> wants to: Download multiple files`` with Block and
    Allow, outside the page. A download-history bubble and in-page text that
    uses the same words are not that prompt.
    """
    states = {"SHOWING", "VISIBLE", "ENABLED", "SENSITIVE"}
    button = _Acc("push button", name="Make blob file", width=96, height=20, states=states)
    heading = _Acc("heading", name="Export", width=80, height=20, states=states)
    document = _adopt(
        _Acc("document web", name="DL|ready", width=800, height=600, states=states),
        heading, button,
    )
    window = _adopt(
        _Acc("frame", name="DL|ready - Google Chrome", width=800, height=600),
        document,
    )
    app = _adopt(_Acc("application", name="chrome"), window)
    app.get_toolkit_name = lambda: "Chrome"

    def bind(node):
        node.get_application = lambda: app
        for child in node.children:
            bind(child)

    bind(window)
    monkeypatch.setattr(_atspi, "_atspi_wrapper_key", lambda node: ("test-bus", str(id(node))))
    driver = _wire_gtk_tree(monkeypatch, window)
    monkeypatch.setattr(driver, "_run", lambda fn: fn())
    monkeypatch.setattr(driver, "frontmost_app", lambda: ("chrome", None))
    monkeypatch.setattr(driver, "same_app_window_cover", lambda *args, **kwargs: None)
    monkeypatch.setattr(driver, "_raise_handle_window", lambda *args, **kwargs: None)
    monkeypatch.setattr(driver, "_visible_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_running_app", lambda name: (None, name))
    monkeypatch.setattr(server, "PREFER_AX_ACTIONS", True)
    monkeypatch.setattr(_linux_input, "click", lambda *a, **kw: pytest.fail("pointer substituted"))
    presses = []
    prompt_title = "http://127.0.0.1:9 wants to: Download multiple files"

    def add_prompt(title, parent):
        prompt = _Acc("dialog", name=title, width=280, height=120, states=states)
        block = _Acc("push button", name="Block", width=60, height=24, states=states)
        allow = _Acc("push button", name="Allow", width=60, height=24, states=states)
        _adopt(prompt, block, allow)
        prompt.get_application = lambda: app
        parent.children.append(prompt)
        prompt.parent = parent
        bind(prompt)

    def press(index):
        presses.append(index)
        document.name = "DL|blob-done-2"
        window.name = "DL|blob-done-2 - Google Chrome"
        if case == "permission":
            add_prompt(prompt_title, window)
        elif case == "duplicate":
            add_prompt(prompt_title, window)
            add_prompt(prompt_title, window)
        elif case == "in_page_permission":
            add_prompt(prompt_title, document)
        elif case == "history":
            bubble = _Acc("dialog", name="Recent download history", width=280, height=80, states=states)
            bubble.get_application = lambda: app
            window.children.append(bubble)
            bubble.parent = window
            bind(bubble)
        elif case == "page_words":
            heading.name = "Download multiple files"
        elif case == "page_dialog":
            add_prompt("Download multiple files", document)
        return True

    button.get_action_iface = lambda: SimpleNamespace(
        get_n_actions=lambda: 1, get_action_name=lambda index: "press", do_action=press,
    )
    runtime = _bare_runtime(driver)
    runtime._current = driver.snapshot(Scope.WINDOW, "chrome")
    target = next(el for el in runtime._current.elements if el.title == "Make blob file")
    result = runtime.click(ref=target.ref)
    assert presses == [0]
    assert "AT-SPI doAction(press)" in result
    assert result.next == ()
    assert not str(result).startswith("clicked")
    if case in {"permission", "duplicate", "in_page_permission"}:
        assert not str(result).startswith("activated")
        assert result.outcome == "unverifiable"
        assert "download_multiple_files_prompt" in result.evidence
        assert prompt_title in result.evidence
        assert prompt_title in str(result)
        assert "not confirmed saved" in str(result)
        assert "Do not click Allow" in str(result)
        assert "no_pointer_gesture" in result.evidence
        assert result.evidence.count(prompt_title) == 1
        assert "clicked" not in str(result)
    elif case == "page_words":
        assert result.outcome == "confirmed"
        assert result.startswith("activated ")
        assert "page activation only" in result.evidence
        assert "download_multiple_files_prompt" not in result.evidence
        assert "permission prompt (" not in str(result)
    else:
        assert not str(result).startswith("activated")
        assert result.outcome == "unverifiable"
        assert "download_multiple_files_prompt" not in result.evidence
        assert "permission prompt (" not in str(result)
        assert "no_pointer_gesture" in result.evidence
