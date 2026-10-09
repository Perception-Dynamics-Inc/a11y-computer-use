"""Grant identity and launch resolution. No display and no AT-SPI."""

from __future__ import annotations

import pytest

from a11y_computer_use import app_identity, safety
from a11y_computer_use.app_identity import matching_stored_key, resolve_launch, same_app
from a11y_computer_use.safety import PermissionStore, Tier, check_action
from a11y_computer_use.schema import Click, ComputerUseError, ErrorCode, Point

_CLICK = Click(target=Point(display_id=1, x=1, y=1))


def test_alias_and_case_share_a_grant(tmp_path) -> None:
    store = PermissionStore(tmp_path / "p.json")
    store.set_tier("thunar", Tier.FULL)
    store.set_tier("xfce4-terminal", Tier.CLICK)
    store.set_tier("soffice", Tier.CLICK)
    store.set_tier("mousepad", Tier.FULL)
    store.set_tier("google-chrome", Tier.CLICK)
    assert store.get_tier("Files") is Tier.FULL
    assert store.get_tier("FILES") is Tier.FULL
    assert store.get_tier("nautilus") is Tier.FULL
    assert store.get_tier("xfce4-terminal") is Tier.CLICK
    assert store.get_tier("Terminal") is None
    assert store.get_tier("gnome-terminal") is None
    assert store.get_tier("xterm") is None
    assert store.get_tier("Text Editor") is Tier.FULL
    assert store.get_tier("gedit") is Tier.FULL
    assert store.get_tier("gnome-text-editor") is Tier.FULL
    assert store.get_tier("chrome") is Tier.CLICK
    assert store.get_tier("chromium") is Tier.CLICK
    assert store.get_tier("chromium-browser") is Tier.CLICK
    assert store.get_tier("google-chrome-stable") is Tier.CLICK
    store.set_tier("gnome-terminal", Tier.FULL)
    assert store.get_tier("gnome-terminal-server") is Tier.FULL
    assert store.get_tier("org.gnome.Terminal") is Tier.FULL
    assert store.get_tier("xterm") is None
    assert store.get_tier("Terminal") is None
    assert store.get_tier("libreoffice calc") is Tier.CLICK
    assert store.get_tier("LibreOffice Calc") is Tier.CLICK
    assert store.get_tier("soffice.bin") is Tier.CLICK
    assert same_app("chrome", "notchrome") is False
    assert same_app("chrome", "google-chrome") is True
    assert same_app("xterm", "gnome-terminal") is False
    assert same_app("gnome-terminal", "gnome-terminal-server") is True
    assert same_app("Files", "profiles") is False
    assert matching_stored_key("Mousepad", ["mousepad"]) == "mousepad"


def test_permission_text_does_not_tell_the_model_to_ask(tmp_path) -> None:
    from a11y_computer_use.server import refusal_text

    decision = check_action(_CLICK, "unknown", store=PermissionStore(tmp_path / "p.json"))
    assert decision.verdict is safety.Verdict.NEEDS_PERMISSION
    assert "ask the user" not in decision.reason
    text = refusal_text(decision)
    assert "ask the user" not in text
    assert "grant_app(app='unknown', tier='click')" in text


def test_resolve_launch_rewrites_labels_and_rejects_unknown_names() -> None:
    entries = [
        {"id": "org.gnome.Nautilus", "name": "Files", "exec": "nautilus", "wm_class": "org.gnome.Nautilus"},
        {"id": "org.gnome.Terminal", "name": "Terminal", "exec": "gnome-terminal", "wm_class": "Gnome-terminal"},
        {"id": "libreoffice-calc", "name": "LibreOffice Calc", "exec": "soffice", "wm_class": "libreoffice-calc"},
    ]
    files = resolve_launch(
        "Files", granted=["thunar"], entries=entries, path_lookup=lambda _name: None,
    )
    assert files.resolved is True
    assert files.launch_name == "thunar"
    assert files.gate_key == "thunar"

    terminal = resolve_launch(
        "Terminal", granted=["xterm"], entries=entries, path_lookup=lambda _name: None,
    )
    assert terminal.launch_name == "xterm"
    assert terminal.gate_key == "xterm"

    calc = resolve_launch(
        "libreoffice calc", granted=["soffice"], entries=entries, path_lookup=lambda _name: None,
    )
    assert calc.launch_name == "soffice"
    assert calc.gate_key == "soffice"

    ungranted = resolve_launch(
        "Files", granted=[], entries=entries, path_lookup=lambda _name: None,
    )
    assert ungranted.resolved is True
    assert ungranted.launch_name == "nautilus"
    assert ungranted.gate_key == "nautilus"

    missing = resolve_launch(
        "not-a-real-app-zz9", granted=["soffice"], entries=entries, path_lookup=lambda _name: None,
    )
    assert missing.resolved is False

    editor_entries = [
        {"id": "org.xfce.mousepad", "name": "Text Editor", "exec": "mousepad", "wm_class": "mousepad"},
    ]
    editor = resolve_launch(
        "Text Editor", granted=["gedit"], entries=editor_entries, path_lookup=lambda _name: None,
    )
    assert editor.resolved is True
    assert editor.launch_name == "gedit"
    assert editor.gate_key == "gedit"

    other_terminal = resolve_launch(
        "gnome-terminal", granted=["xterm"], entries=entries, path_lookup=lambda _name: "/usr/bin/gnome-terminal",
    )
    assert other_terminal.gate_key != "xterm"
    assert same_app(other_terminal.gate_key or "", "xterm") is False

    # CI installs xterm and no desktop file named Terminal. The grant alone resolves.
    bare = resolve_launch(
        "Terminal", granted=["pcmanfm", "xterm"], entries=[], path_lookup=lambda _name: None,
    )
    assert bare.resolved is True
    assert bare.launch_name == "xterm"
    assert bare.gate_key == "xterm"

    ambiguous = resolve_launch(
        "Terminal", granted=["xterm", "kitty"], entries=[], path_lookup=lambda _name: None,
    )
    assert ambiguous.resolved is False


def test_launch_uses_the_granted_alias_and_unknown_names_are_not_permission_errors(
    tmp_path, monkeypatch,
) -> None:
    from a11y_computer_use import server

    launched: list[str] = []

    class _D:
        resolves_apps = False
        name = "fake"

        def ensure_trusted(self):
            return None

        def frontmost_app(self):
            return ("shell", 1)

        def main_display_id(self):
            return 0

        def launch_app(self, ident):
            launched.append(ident)
            return {
                "pid": 9, "proc": None, "identifier": ident,
                "names": [ident], "is_launcher": False,
            }

        def windows(self):
            return [{"window_id": 1, "app": "thunar", "title": "Home", "pid": 9}]

    monkeypatch.setattr(app_identity, "load_desktop_entries", lambda: [])
    monkeypatch.setattr(server, "_installed_bundle_id", lambda _ident: None)
    monkeypatch.setattr(
        server, "_running_app",
        lambda _ident: (_ for _ in ()).throw(ComputerUseError(ErrorCode.APP_NOT_FOUND, "not running")),
    )
    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier("thunar", Tier.CLICK)
    store.set_tier("soffice", Tier.CLICK)
    store.set_tier("xterm", Tier.FULL)
    runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=_D())
    runtime.APP_LAUNCH_WAIT_S = 1
    text = runtime.app("launch", "Files")
    assert "needs_permission" not in text
    assert "ask the user" not in text
    assert launched == ["thunar"]
    terminal = runtime.app("launch", "Terminal")
    assert "needs_permission" not in terminal
    assert launched == ["thunar", "xterm"]

    with pytest.raises(ComputerUseError) as exc:
        runtime.app("launch", "not-a-real-app-zz9")
    assert exc.value.code is ErrorCode.APP_NOT_FOUND
    assert "soffice" in exc.value.message
    assert "thunar" in exc.value.message
    assert "needs_permission" not in exc.value.message
    assert "ask the user" not in exc.value.message
