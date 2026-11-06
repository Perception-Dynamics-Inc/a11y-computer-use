"""Hermetic light-DOM toolbar regressions for #1489; no Shadow DOM or live Chrome."""
from types import SimpleNamespace

import pytest

from a11y_computer_use import server
from a11y_computer_use.drivers import _atspi, _linux_input
from a11y_computer_use.schema import Scope
from tests.test_linux_synthetic import (
    _Acc, _adopt, _bare_runtime, _wire_gtk_tree, fake_atspi,
)


@pytest.mark.parametrize("label,token", [
    ("Bold", "bold"),
    ("Italic", "italic"),
])
@pytest.mark.parametrize("effect", ["title", "unchanged", "prompt", "foreign_title", "unreadable"])
def test_light_dom_toolbar_press_confirms_page_title_activation(
    fake_atspi, monkeypatch, label, token, effect,
):
    """Plain toolbar buttons (no shadow) confirm when the page title/status log changes."""
    states = {"SHOWING", "VISIBLE", "ENABLED", "SENSITIVE"}
    bold = _Acc("push button", name="Bold", width=60, height=24, states=states)
    italic = _Acc("push button", name="Italic", width=60, height=24, states=states)
    editor = _Acc("textfield", name="Editor", width=400, height=200, states=states)
    toolbar = _adopt(_Acc("tool bar", name="Formatting", width=400, height=30, states=states),
                     bold, italic)
    document = _adopt(
        _Acc("document web", name="V:ced ready", width=800, height=600, states=states),
        toolbar, editor,
    )
    foreign = _Acc("document web", name="Other page", width=100, height=100, states=states)
    window = _adopt(_Acc("frame", name="V:ced ready - Chrome", width=800, height=600),
                    document, foreign)
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
    button = bold if label == "Bold" else italic

    def press(index):
        presses.append(index)
        if effect in {"title", "prompt", "unreadable"}:
            document.name = f"V:ced S=[{token}]"
            window.name = f"V:ced S=[{token}] - Chrome"
        if effect == "foreign_title":
            foreign.name = "Other page changed"
        if effect == "prompt":
            prompt = _Acc("dialog", name="Download multiple files", width=200, height=100,
                          states=states)
            prompt.get_application = lambda: app
            _adopt(window, document, foreign, prompt)
        return True

    button.get_action_iface = lambda: SimpleNamespace(
        get_n_actions=lambda: 1, get_action_name=lambda index: "press", do_action=press,
    )
    runtime = _bare_runtime(driver)
    runtime._current = driver.snapshot(Scope.WINDOW, "chrome")
    target = next(el for el in runtime._current.elements if el.title == label)
    if effect == "unreadable":
        original_effect = driver.effect_snapshot
        monkeypatch.setattr(
            driver, "effect_snapshot",
            lambda *args: None if presses else original_effect(*args),
        )
    result = runtime.click(ref=target.ref)
    assert presses == [0]
    assert "AT-SPI doAction(press)" in result
    assert result.next == ()
    if effect == "title":
        assert result.outcome == "confirmed"
        assert result.startswith("activated ")
        assert "the clicked page's native title changed" in result.evidence
        assert "page activation only" in result.evidence
        assert "downloads and other gesture-gated browser effects are not verified" in result.evidence
    else:
        assert result.outcome == "unverifiable"
        if effect == "prompt":
            assert "no_pointer_gesture" in result.evidence
