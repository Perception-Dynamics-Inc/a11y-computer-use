"""#793: X keysym spellings such as Page_Down are accepted by the key tool."""

import pytest

from a11y_computer_use import safety, server
from a11y_computer_use.schema import canonical_chord


@pytest.mark.parametrize("chord,canon", [
    ("page_down", "pagedown"), ("Page_Down", "pagedown"), ("PAGE_DOWN", "pagedown"),
    ("Next", "pagedown"), ("pgdn", "pagedown"),
    ("page_up", "pageup"), ("Page_Up", "pageup"), ("Prior", "pageup"), ("PgUp", "pageup"),
    ("ArrowUp", "up"), ("arrowdown", "down"), ("ArrowLeft", "left"), ("ArrowRight", "right"),
    ("Caps_Lock", "capslock"), ("Num_Lock", "numlock"), ("Scroll_Lock", "scrolllock"),
    ("Control_L+a", "ctrl+a"), ("Control_R+Next", "ctrl+pagedown"),
    ("Shift_L+Page_Up", "shift+pageup"), ("Alt_R+Tab", "alt+Tab"), ("Super_L+e", "cmd+e"),
    ("Meta_L+e", "cmd+e"),
])
def test_alias_spellings_map_to_canonical_names(chord, canon):
    assert canonical_chord(chord) == canon


@pytest.mark.parametrize("chord", [
    "pagedown", "pageup", "Return", "BackSpace", "Escape", "KP_Enter", "ctrl+a",
    "ctrl+shift+t", "ctrl+plus", "F5", "kp_home", "", "ctrl+", "+", "not-a-key",
])
def test_names_without_an_alias_are_unchanged(chord):
    assert canonical_chord(chord) == chord


def _linux_input():
    pytest.importorskip("Xlib")
    from a11y_computer_use.drivers import _linux_input
    return _linux_input


@pytest.mark.parametrize("alias,canon", [
    ("Page_Down", "pagedown"), ("page_down", "pagedown"), ("Next", "pagedown"),
    ("Page_Up", "pageup"), ("page_up", "pageup"), ("Prior", "pageup"),
    ("Return", "return"), ("BackSpace", "backspace"), ("Escape", "escape"),
    ("KP_Enter", "kp_enter"), ("Control_L+a", "ctrl+a"), ("Shift_R+Tab", "shift+tab"),
])
def test_linux_aliases_send_the_same_keysyms(alias, canon):
    linux = _linux_input()
    assert linux._parse_chord(canonical_chord(alias)) == linux._parse_chord(canon)


@pytest.mark.parametrize("alias,canon", [
    ("Page_Down", "pagedown"), ("Prior", "pageup"), ("Return", "return"),
    ("BackSpace", "backspace"), ("Escape", "escape"), ("Control_L+a", "ctrl+a"),
])
def test_browser_and_windows_aliases_send_the_same_keys(alias, canon):
    from a11y_computer_use.drivers import _win_input, browser

    assert browser._key_events(canonical_chord(alias)) == browser._key_events(canon)
    assert _win_input._parse_chord(canonical_chord(alias)) == _win_input._parse_chord(canon)


def test_kp_enter_is_known_on_every_backend():
    from a11y_computer_use import act
    from a11y_computer_use.drivers import _win_input, browser

    events = browser._key_events(canonical_chord("KP_Enter"))
    assert events[0]["key"] == "Enter" and events[0]["code"] == "NumpadEnter"
    assert _win_input._parse_chord("KP_Enter") == ([], 0x0D)
    assert act.parse_chord("KP_Enter")[1] == 76  # kVK_ANSI_KeypadEnter
    assert _linux_input()._parse_chord("KP_Enter") == ([], 0xFF8D)


class _LinuxKeys:
    name = "linux"
    resolves_apps = False

    def __init__(self):
        self.pressed: list[str] = []

    def key_chord(self, chord, **kw):
        self.pressed.append(chord)


def _runtime(tmp_path, monkeypatch):
    """A Linux runtime whose frontmost/menu/outcome probes are stubbed: only key() parsing runs."""
    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier("editor", safety.Tier.FULL)
    driver = _LinuxKeys()
    rt = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)
    monkeypatch.setattr(rt, "_frontmost", lambda *a, **k: "editor")
    monkeypatch.setattr(rt, "_open_menu_app", lambda *a, **k: None)
    monkeypatch.setattr(rt, "_key_focus_probe", lambda *a, **k: None)
    monkeypatch.setattr(rt, "_capture", lambda *a, **k: None)
    monkeypatch.setattr(rt, "_guard_user", lambda *a, **k: None)
    monkeypatch.setattr(rt, "_open_mnemonic_menu", lambda *a, **k: False)
    monkeypatch.setattr(rt, "_run_gated", lambda action, target, execute, **k: execute())
    monkeypatch.setattr(rt, "_conclude", lambda text, **k: text)
    return rt, driver


@pytest.mark.parametrize("chord,sent", [
    ("page_down", "pagedown"), ("Page_Down", "pagedown"),
    ("page_up", "pageup"), ("Page_Up", "pageup"), ("ctrl+Next", "ctrl+pagedown"),
])
def test_runtime_key_presses_the_canonical_chord(tmp_path, monkeypatch, chord, sent):
    pytest.importorskip("Xlib")
    rt, driver = _runtime(tmp_path, monkeypatch)
    assert rt.key(chord) == f"pressed {sent}"
    assert driver.pressed == [sent]


@pytest.mark.parametrize("chord", ["pagedown", "Return", "escape", "KP_Enter"])
def test_runtime_key_keeps_existing_names(tmp_path, monkeypatch, chord):
    pytest.importorskip("Xlib")
    rt, driver = _runtime(tmp_path, monkeypatch)
    rt.key(chord)
    assert driver.pressed == [chord]


def test_runtime_key_still_rejects_unknown_keys_before_input(tmp_path, monkeypatch):
    pytest.importorskip("Xlib")
    rt, driver = _runtime(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="unknown key 'page_dwn'"):
        rt.key("page_dwn")
    assert driver.pressed == []


def test_act_key_step_validation_accepts_aliases(tmp_path, monkeypatch):
    pytest.importorskip("Xlib")
    rt, _ = _runtime(tmp_path, monkeypatch)
    assert rt._chord_argument_message("Page_Down") is None
    assert rt._chord_argument_message("Control_L+page_up") is None
    assert "unknown key 'page_dwn'" in rt._chord_argument_message("page_dwn")
