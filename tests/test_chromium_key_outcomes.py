"""Scoped Linux Chromium key verification must not require a page snapshot."""
from types import SimpleNamespace

import pytest

from a11y_computer_use import server
from a11y_computer_use.drivers import _atspi


@pytest.mark.parametrize("change", ["scroll", "caret", "window", "count", "unchanged", "missing", "died"])
@pytest.mark.parametrize("chord", ["End", "Escape", "ctrl+c"])
def test_scoped_page_key_outcome(monkeypatch, change, chord):
    runtime = server.Runtime.__new__(server.Runtime)
    runtime.driver = SimpleNamespace(name="linux")
    runtime._current = object()
    page = ("doc", "window", "anchor", (0, 0, 100, 20), "Page", 1)
    before = ("abc", 0, 0, 0, None, page)
    after = list(before)
    changed_page = list(page)
    if change == "scroll":
        changed_page[3] = (0, -900, 100, 20)
    elif change == "window":
        changed_page[4] = "New page"
    elif change == "count":
        changed_page[5] = 2
    elif change == "caret":
        after[1] = 3
    after[5] = tuple(changed_page)
    after = tuple(after) if change != "missing" else before[:5]
    scans = []
    monkeypatch.setattr(runtime, "_key_focus_probe", lambda app: after)
    monkeypatch.setattr(runtime, "_process_died", lambda before: change == "died")
    monkeypatch.setattr(runtime, "_reread", lambda *args: scans.append(args))
    result, evidence = runtime._judge_mutation(
        "chrome", None, None, None, key_focus=True, key_chord=chord, focus_before=before,
    )
    if change != "missing":
        assert scans == []
        assert runtime._action_baseline["capture"] is None
        expected = "partial" if change == "died" else (
            "unverifiable" if change == "unchanged" or chord == "ctrl+c" else "confirmed"
        )
        assert result == expected
    else:
        assert len(scans) == 1


@pytest.mark.parametrize("failure", [None, "not_chromium", "no_document", "no_window", "no_anchor", "no_bounds", "empty_bounds", "no_count"])
def test_page_probe_is_bounded_and_requires_native_evidence(monkeypatch, failure):
    window = SimpleNamespace(role="frame", get_name=lambda: "Page", get_child_count=lambda: None if failure == "no_count" else 1)
    document = SimpleNamespace(role="document web")
    anchor = object()
    focused = SimpleNamespace(role="entry")
    chain = [focused, document, window]
    if failure == "no_document":
        chain.remove(document)
    if failure == "no_window":
        chain.remove(window)
    monkeypatch.setattr(_atspi, "_chromium_app", lambda acc: failure != "not_chromium")
    monkeypatch.setattr(_atspi, "_ancestor_chain", lambda acc: iter(chain))
    monkeypatch.setattr(_atspi, "_role_name", lambda acc: acc.role)
    monkeypatch.setattr(_atspi, "_ancestor_key", id)
    reads = []

    def child_at(node, index):
        assert node is document and index == 0
        reads.append(index)
        return None if failure == "no_anchor" else anchor

    monkeypatch.setattr(_atspi, "_child_at", child_at)
    monkeypatch.setattr(_atspi, "_raw_rect", lambda node, coord: None if failure == "no_bounds" else (0, -100, 0 if failure == "empty_bounds" else 100, 20))
    result = _atspi._chromium_page_key_state(focused)
    assert (result is None) == (failure is not None)
    assert len(reads) <= 1
    if result is not None:
        assert result[3] == (0, -100, 100, 20)


def test_focus_probe_attaches_page_only_when_requested(monkeypatch):
    focused = object()
    monkeypatch.setattr(_atspi, "_focused_node", lambda app: (focused, False))
    monkeypatch.setattr(_atspi, "_readable_text", lambda acc: "abc")
    monkeypatch.setattr(_atspi, "_atspi", lambda: SimpleNamespace(Text=SimpleNamespace(get_character_count=lambda acc: 3)))
    monkeypatch.setattr(_atspi, "_caret_and_selection", lambda *args: (1, 1, 1))
    monkeypatch.setattr(_atspi, "_chromium_page_key_state", lambda acc: ("page",))
    assert _atspi.focused_key_evidence("chrome") == ("abc", 1, 1, 1, None)
    assert _atspi.focused_key_evidence("chrome", include_page=True) == ("abc", 1, 1, 1, None, ("page",))


@pytest.mark.parametrize("chord", ["End", "Escape", "ctrl+c"])
def test_runtime_delivers_exact_key_and_uses_scoped_evidence(tmp_path, monkeypatch, chord):
    from tests.test_action_outcomes import APP, TreeDriver, _runtime

    driver = TreeDriver()
    runtime = _runtime(tmp_path, driver)
    driver.name = "linux"
    state = [0]
    monkeypatch.setattr(runtime, "_linux_keyboard_window", lambda app: (APP, {}, []))
    monkeypatch.setattr(runtime, "_ensure_linux_window", lambda *args: False)
    monkeypatch.setattr(runtime, "_guard_user", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "_open_menu_app", lambda app: None)
    monkeypatch.setattr(runtime, "_open_mnemonic_menu", lambda *args: False)
    monkeypatch.setattr(runtime, "_writer_key_probe", lambda *args: None)
    monkeypatch.setattr(runtime, "_sheet_key_probe", lambda *args: None)
    monkeypatch.setattr(runtime, "_key_focus_probe", lambda app: (None, None, None, None, None, (state[0],)))
    monkeypatch.setattr(runtime, "_reread", lambda *args: pytest.fail("full window read"))
    clipboard = iter([(1, ("text",)), (2, ("text",))])
    monkeypatch.setattr(runtime, "_clipboard_key_probe", lambda chord: next(clipboard))
    driver.key_effect = lambda key: state.__setitem__(0, 1 if key == "End" else 0)
    result = runtime.key(chord, app=APP)
    assert result.outcome == ("unverifiable" if chord == "Escape" else "confirmed")
    if chord == "ctrl+c":
        assert "clipboard" in result.evidence
    assert driver.calls == [("key", chord)]


@pytest.mark.parametrize("truncated", [True, False])
def test_page_evidence_failure_preserves_original_focus_record(monkeypatch, truncated):
    monkeypatch.setattr(_atspi, "_focused_node", lambda app: (object(), truncated))
    monkeypatch.setattr(_atspi, "_readable_text", lambda acc: None)
    monkeypatch.setattr(_atspi, "_atspi", lambda: None)
    monkeypatch.setattr(_atspi, "_caret_and_selection", lambda *args: (None, None, None))

    def unavailable(acc):
        if truncated:
            pytest.fail("incomplete focus discovery must not authorize scoped verification")
        raise RuntimeError("defunct document")

    monkeypatch.setattr(_atspi, "_chromium_page_key_state", unavailable)
    assert _atspi.focused_key_evidence("chrome", include_page=True) == (None,) * 5
