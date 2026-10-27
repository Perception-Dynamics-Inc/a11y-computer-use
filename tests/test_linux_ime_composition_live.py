"""#1219 live: an open IBus composition and a refused switch, in Chrome and Firefox.

A real pinyin composition ("nihao" typed on the X keyboard, preedit 你好) is open in
a browser field. ``set_value`` and ``type`` have to refuse it, deterministically and
without a pause that lets the panel catch up, and leave the preedit open and the
field untouched. A forced engine-switch failure must refuse before the field is
cleared (a Firefox entry is cleared with ctrl+a, BackSpace before it is typed).

Skipped unless the browser, ``ibus`` with ``libpinyin``, ``ibus-ui-gtk3`` and
``xdotool`` are present. The user's engine is put back afterwards.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux backend")

from a11y_computer_use.schema import ComputerUseError, ErrorCode, Scope  # noqa: E402
from tests.test_linux_firefox_live import _PROFILE_JS, _firefox_binary  # noqa: E402
from tests.test_linux_live import _chrome_binary, _runtime_for, _stop_group  # noqa: E402

_PAGE = (
    "<!doctype html><meta charset=utf-8><title>cuaime</title>"
    "<input id=f aria-label='Name field'>"
)
_ATTEMPTS = 6


def _ibus(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["ibus", *args], capture_output=True, text=True, timeout=5, check=False)


def _wait_for(check, seconds: float = 5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(0.05)
    return check()


def _launch(kind: str, tmp_path):
    page = tmp_path / "ime.html"
    page.write_text(_PAGE)
    profile = tmp_path / "profile"
    profile.mkdir()
    env = dict(os.environ, XMODIFIERS="@im=ibus", GTK_IM_MODULE="ibus", QT_IM_MODULE="ibus")
    if kind == "chrome":
        binary = _chrome_binary()
        if binary is None:
            pytest.skip("no Chrome")
        cmd = [
            binary, "--force-renderer-accessibility", "--no-sandbox", "--disable-gpu",
            "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
            f"--user-data-dir={profile}", "--window-size=1000,500", f"file://{page}",
        ]
        return cmd, env, "chrome", "google-chrome"
    binary = _firefox_binary()
    if binary is None:
        pytest.skip("no Firefox")
    (profile / "user.js").write_text(_PROFILE_JS)
    env.update(MOZ_ENABLE_ACCESSIBILITY="1", GTK_MODULES="gail:atk-bridge", NO_AT_BRIDGE="0")
    return [binary, "--profile", str(profile), "--no-remote", "--new-instance", page.as_uri()], env, "firefox", "firefox"


@pytest.fixture(scope="module", params=["chrome", "firefox"])
def browser(request, tmp_path_factory):
    from a11y_computer_use.drivers import _atspi
    from a11y_computer_use.drivers.linux import LinuxDriver

    for tool in ("ibus", "xdotool"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not installed")
    before = _ibus("engine")
    if before.returncode != 0 or not before.stdout.strip() or " " in before.stdout.strip():
        pytest.skip("no running IBus daemon with an engine set")
    original = before.stdout.strip()
    _ibus("engine", "libpinyin")
    if _ibus("engine").stdout.strip() != "libpinyin":
        _ibus("engine", original)
        pytest.skip("IBus cannot load libpinyin")
    if _atspi.ime_composition_open() is None:
        _ibus("engine", original)
        pytest.skip("the IBus panel cannot be read (is ibus-ui-gtk3 the panel?)")
    tmp_path = tmp_path_factory.mktemp(f"ime-{request.param}")
    cmd, env, app, comm = _launch(request.param, tmp_path)
    saved = os.environ.get("A11Y_IME_BYPASS")
    os.environ["A11Y_IME_BYPASS"] = "1"  # tests/conftest.py turns it off by default
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, env=env,
    )
    driver = LinuxDriver()
    try:
        field = shot = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and field is None:
            try:
                shot = driver.snapshot(Scope.WINDOW, app)
            except ComputerUseError as exc:
                if exc.code is not ErrorCode.APP_NOT_FOUND:
                    raise
            else:
                field = next((e for e in shot.elements if e.title == "Name field"), None)
            time.sleep(0.5)
        assert field is not None, f"{app} never exposed the field"
        runtime = _runtime_for(tmp_path, driver, app, comm, "ibus-ui-gtk3")
        runtime._current = shot
        yield driver, runtime, field, app
    finally:
        subprocess.run(["xdotool", "key", "Escape"], check=False, timeout=5)
        _stop_group(proc)
        if saved is None:
            os.environ.pop("A11Y_IME_BYPASS", None)
        else:
            os.environ["A11Y_IME_BYPASS"] = saved
        for _ in range(3):
            _ibus("engine", original)
            if _ibus("engine").stdout.strip() == original:
                break


def _value(driver, app) -> str | None:
    shot = driver.snapshot(Scope.WINDOW, app)
    return next(e.value for e in shot.elements if e.title == "Name field")


def _reset(driver, runtime, field, app) -> None:
    """No composition, an empty field, the pinyin engine, the field focused."""
    from a11y_computer_use.drivers import _atspi

    subprocess.run(["xdotool", "key", "Escape"], check=False, timeout=5)
    assert _wait_for(lambda: _atspi.ime_composition_open() is False), "composition did not close"
    if _ibus("engine").stdout.strip() != "libpinyin":
        _ibus("engine", "libpinyin")
    runtime.click(field.ref)
    if _value(driver, app):
        subprocess.run(["xdotool", "key", "ctrl+a", "BackSpace"], check=True, timeout=5)
        assert _wait_for(lambda: not _value(driver, app))


def _open_composition(attempt: int, wait: bool) -> None:
    from a11y_computer_use.drivers import _atspi

    delay = ("0", "10", "80")[attempt % 3]
    subprocess.run(["xdotool", "type", "--delay", delay, "nihao"], check=True, timeout=15)
    if wait:
        assert _wait_for(lambda: _atspi.ime_composition_open() is True), "no preedit appeared"


@pytest.mark.parametrize("attempt", range(_ATTEMPTS))
def test_set_value_refuses_an_open_composition_and_leaves_it_open(browser, attempt) -> None:
    """The call comes straight after the keys on even attempts: the panel may not know yet."""
    from a11y_computer_use.drivers import _atspi

    driver, runtime, field, app = browser
    _reset(driver, runtime, field, app)
    _open_composition(attempt, wait=bool(attempt % 2))
    with pytest.raises(ComputerUseError) as exc:
        runtime.set_value(field.ref, "hello world 42")
    assert exc.value.detail.get("reason") == "ime_composition_open"
    assert exc.value.detail.get("typed") is False
    assert _ibus("engine").stdout.strip() == "libpinyin"  # not switched
    assert _atspi.ime_composition_open() is True  # the preedit is still open
    assert "你好" in (_value(driver, app) or "")


@pytest.mark.parametrize("attempt", range(_ATTEMPTS))
def test_type_never_drops_an_open_composition(browser, attempt) -> None:
    from a11y_computer_use.drivers import _atspi

    driver, runtime, field, app = browser
    _reset(driver, runtime, field, app)
    _open_composition(attempt, wait=bool(attempt % 2))
    with pytest.raises(ComputerUseError) as exc:
        runtime.type_text(" abc", app=app)
    assert exc.value.detail.get("reason") == "ime_composition_open"
    assert _ibus("engine").stdout.strip() == "libpinyin"
    assert _atspi.ime_composition_open() is True
    assert "你好" in (_value(driver, app) or "")
    # By ref the field is focused first, which commits the preedit in Chrome: that or a
    # refusal is fine, but the 你好 is never dropped.
    try:
        runtime.type_text(" abc", ref=field.ref)
    except ComputerUseError as err:
        assert err.detail.get("reason") == "ime_composition_open"
    assert "你好" in (_value(driver, app) or "")


@pytest.mark.parametrize("attempt", range(_ATTEMPTS))
def test_a_failed_switch_refuses_before_the_field_is_cleared(browser, attempt, monkeypatch) -> None:
    """Firefox used to clear the field (ctrl+a, BackSpace) and only then find the switch had failed."""
    from a11y_computer_use.drivers import _atspi, _linux_ime, _linux_input

    driver, runtime, field, app = browser
    _reset(driver, runtime, field, app)
    _ibus("engine", "xkb:us::eng")
    runtime.set_value(field.ref, "keep me")
    assert _value(driver, app) == "keep me"
    _ibus("engine", "libpinyin")
    assert _wait_for(lambda: _atspi.ime_composition_open() is False)
    sent: list[str] = []
    monkeypatch.setattr(_linux_ime, "_set_engine", lambda name: False)
    for name in ("_chord_into_target", "_x11_select_all_and_delete"):
        real = getattr(_atspi, name)
        monkeypatch.setattr(_atspi, name, lambda *a, _n=name, _r=real, **k: (sent.append(_n), _r(*a, **k))[1])
    real_keys = _linux_input._type_string_keys
    monkeypatch.setattr(
        _linux_input, "_type_string_keys",
        lambda text, delay=None: (sent.append("type"), real_keys(text, delay))[1],
    )
    for call in (
        lambda: runtime.set_value(field.ref, "hello world 42"),
        lambda: runtime.type_text("hello world 42", ref=field.ref),
    ):
        with pytest.raises(ComputerUseError) as exc:
            call()
        assert exc.value.detail.get("reason") == "ime_active"
        assert exc.value.detail.get("typed") is False
        assert _value(driver, app) == "keep me"
    assert sent == []
    assert _ibus("engine").stdout.strip() == "libpinyin"


@pytest.mark.parametrize("attempt", range(_ATTEMPTS))
def test_once_the_composition_is_cleared_the_exact_text_lands(browser, attempt) -> None:
    driver, runtime, field, app = browser
    _reset(driver, runtime, field, app)
    _open_composition(attempt, wait=False)
    subprocess.run(["xdotool", "key", "Escape"], check=True, timeout=5)
    runtime.set_value(field.ref, "hello world 42")
    assert _value(driver, app) == "hello world 42"
    assert _ibus("engine").stdout.strip() == "libpinyin"
    runtime.type_text(" 你好", ref=field.ref)
    assert _value(driver, app) == "hello world 42 你好"
