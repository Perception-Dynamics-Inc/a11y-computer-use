"""Live Firefox on local pages, under the Linux AT-SPI job.

These tests launch Firefox ESR (or firefox on PATH) against file:// pages.
They are not synthetic. They skip only when there is no display or no AT-SPI
bus, the same gate as the GTK live tests. A missing Firefox binary is a
failure: the Linux CI job installs it.

#127: set_value and type land in a text input, a number input, and a textarea,
and set_value changes a select. EditableText on these fields returns success
without changing the DOM; the path that passes is focus plus key events, and
the read-back has to match. The page's own log records input and change events.

#128: a background tab and the preloaded New Tab page are not in the snapshot
or in find. A click on a link in the hidden tab is an error, not "clicked".

#151: type into a Firefox contenteditable reports success when the text landed.
The key fallback's read-back treats NBSP as a space and U+FFFC as the child
text, and set_value replaces Editor A instead of leaving it empty.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux backend")

from a11y_computer_use import observe  # noqa: E402
from a11y_computer_use.schema import Bounds, ComputerUseError, Element, ErrorCode, Scope  # noqa: E402

_FORM = textwrap.dedent(
    """\
    <!doctype html>
    <html>
    <head><meta charset="utf-8"><title>Form Probe</title></head>
    <body>
    <div><label>Name <input id="t"></label></div>
    <div><label>Count <input id="n" type="number"></label></div>
    <div><label>Notes <textarea id="ta"></textarea></label></div>
    <div><label>Color <select id="s"><option>Red</option><option>Green</option><option>Blue</option></select></label></div>
    <button type="button">Div button</button>
    <div id="ed" contenteditable="true" role="textbox" aria-label="Editor A">Hello world</div>
    <div id="edb" contenteditable="true" aria-label="Editor B"><p>First para</p><p>Second <b>bold</b> para</p></div>
    <p id="log"></p>
    <script>
    function hook(el) {
      ["input", "change"].forEach(function (ev) {
        el.addEventListener(ev, function () {
          var log = document.getElementById("log");
          log.textContent = (log.textContent + " " + el.id + ":" + ev + "=" + el.value).trim();
        });
      });
    }
    ["t", "n", "ta", "s"].forEach(function (id) { hook(document.getElementById(id)); });
    </script>
    </body>
    </html>
    """
)

_OTHER = textwrap.dedent(
    """\
    <!doctype html>
    <html>
    <head><meta charset="utf-8"><title>Firefox Privacy Notice</title></head>
    <body>
    <h1>Firefox Privacy Notice</h1>
    <p>This is a background tab.</p>
    <a href="https://www.example.com/products">Products</a>
    <a href="https://www.example.com/wiki">Wikipedia-bg</a>
    </body>
    </html>
    """
)

_PROFILE_JS = textwrap.dedent(
    """\
    user_pref("accessibility.force_disabled", 0);
    user_pref("browser.aboutwelcome.enabled", false);
    user_pref("browser.startup.homepage_override.mstone", "ignore");
    user_pref("datareporting.policy.dataSubmissionEnabled", false);
    user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
    user_pref("browser.shell.checkDefaultBrowser", false);
    user_pref("browser.tabs.warnOnClose", false);
    user_pref("browser.startup.firstrunSkipsHomepage", true);
    user_pref("browser.newtab.preload", true);
    user_pref("browser.sessionstore.resume_from_crash", false);
    user_pref("trailhead.firstrun.didSeeAboutWelcome", true);
    """
)


def _firefox_binary() -> str | None:
    for name in ("firefox", "firefox-esr"):
        found = shutil.which(name)
        if found:
            return found
    candidate = "/opt/firefox/firefox"
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return None


def _require_bus(driver) -> None:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        pytest.skip("no DISPLAY/WAYLAND_DISPLAY: Firefox cannot open a window")
    try:
        driver.ensure_trusted()
    except ComputerUseError as exc:
        pytest.skip(f"AT-SPI bus not reachable: {exc.message}")
    except ImportError as exc:  # pragma: no cover - env guard
        pytest.skip(f"PyGObject/Atspi missing: {exc}")


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)


@pytest.fixture(scope="module")
def firefox_form(tmp_path_factory):
    """One Firefox window: the form is the active tab, a second page is behind it."""
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    binary = _firefox_binary()
    if binary is None:
        pytest.fail("Firefox is not installed; the Linux live job installs it when it is missing")
    root = tmp_path_factory.mktemp("firefox-form")
    page = root / "page.html"
    other = root / "other.html"
    page.write_text(_FORM)
    other.write_text(_OTHER)
    env = os.environ.copy()
    env["MOZ_ENABLE_ACCESSIBILITY"] = "1"
    env["GTK_MODULES"] = "gail:atk-bridge"
    env["NO_AT_BRIDGE"] = "0"
    proc = None
    log_f = None
    try:
        snap = None
        # The first launch on a busy runner can exit before AT-SPI sees the
        # app. One fresh profile is enough when that happens; a second miss
        # still fails.
        for attempt in (1, 2):
            if proc is not None:
                _stop(proc)
            if log_f is not None:
                log_f.close()
            profile = root / f"profile-{attempt}"
            profile.mkdir()
            (profile / "user.js").write_text(_PROFILE_JS)
            log_f = (root / f"firefox-{attempt}.log").open("w", encoding="utf-8")
            proc = subprocess.Popen(
                [binary, "--profile", str(profile), "--no-remote", "--new-instance", page.as_uri(), other.as_uri()],
                env=env,
                start_new_session=True,
                stdout=log_f,
                stderr=subprocess.STDOUT,
            )
            snap = _wait_for_form(driver)
            if snap is not None:
                break
        if snap is None:
            if log_f is not None:
                log_f.flush()
            tail = (root / "firefox-2.log").read_text(encoding="utf-8", errors="replace")[-1500:]
            pytest.fail(
                "Firefox did not publish the form in an AT-SPI snapshot "
                f"(exit={proc.poll() if proc is not None else None}). log:\n{tail}"
            )
        yield driver
    finally:
        if proc is not None:
            _stop(proc)
        if log_f is not None:
            log_f.close()


def _wait_for_form(driver, timeout_s: float = 25.0):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            last = driver.snapshot(Scope.WINDOW, "firefox")
        except ComputerUseError as exc:
            if exc.code is not ErrorCode.APP_NOT_FOUND:
                raise
            last = None
        else:
            titles = {el.title for el in last.elements}
            if "Name" in titles and "Count" in titles and "Notes" in titles and "Color" in titles:
                return last
        time.sleep(0.4)
    return last


def _field(snap, title: str):
    return next((el for el in snap.elements if el.title == title and (el.editable or el.role == "AXComboBox")), None)


def _runtime(tmp_path, driver):
    from a11y_computer_use import safety, server

    store = safety.PermissionStore(tmp_path / "permissions.json")
    store.set_tier("firefox", safety.Tier.FULL)
    store.set_tier("firefox-bin", safety.Tier.FULL)
    front = driver.frontmost_app()[0]
    if front:
        store.set_tier(front, safety.Tier.FULL)
    return server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=driver)


def _serve_pages(host: str, pages: dict[str, str]):
    class _Quiet(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            body = self.server.pages.get(path)
            if body is None:
                self.send_error(404)
                return
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            return

    httpd = ThreadingHTTPServer((host, 0), _Quiet)
    httpd.pages = pages
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


_CHILD_PAGE = (
    "<!doctype html><meta charset=utf-8><title>Titled Child</title>"
    "<label><input id=agree type=checkbox aria-label=Agree> Agree</label>"
    "<button id=go type=button>InnerGo</button>"
)


def _parent_page(src: str) -> str:
    return (
        "<!doctype html><meta charset=utf-8><title>Titled Parent</title>"
        "<button id=outer type=button>OuterBtn</button>"
        f"<iframe title=guest src=\"{src}\" width=480 height=260></iframe>"
    )


def _launch_firefox(tmp_path, url: str) -> subprocess.Popen:
    binary = _firefox_binary()
    if binary is None:
        pytest.fail("Firefox is not installed; the Linux live job installs it when it is missing")
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "user.js").write_text(_PROFILE_JS)
    env = os.environ.copy()
    env["MOZ_ENABLE_ACCESSIBILITY"] = "1"
    env["GTK_MODULES"] = "gail:atk-bridge"
    env["NO_AT_BRIDGE"] = "0"
    log_f = (tmp_path / "firefox.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [binary, "--profile", str(profile), "--no-remote", "--new-instance", url],
        env=env,
        start_new_session=True,
        stdout=log_f,
        stderr=subprocess.STDOUT,
    )
    proc._log = log_f  # type: ignore[attr-defined]
    return proc


def _wait_titled_frame(driver, runtime, timeout_s: float = 60.0):
    """Snapshot through ``find``, so the grant is the process comm Firefox uses."""
    deadline = time.monotonic() + timeout_s
    shot = None
    while time.monotonic() < deadline:
        if getattr(driver, "_proc", None) is not None and driver._proc.poll() is not None:
            raise AssertionError(
                f"Firefox exited with status {driver._proc.returncode} before the titled frame was exposed"
            )
        try:
            runtime.find("firefox", text="Agree")
        except ComputerUseError as exc:
            if exc.code is not ErrorCode.APP_NOT_FOUND:
                raise
            shot = None
        else:
            shot = runtime._current
            titles = {el.title for el in shot.elements}
            boxes = [el for el in shot.elements if el.title == "Agree" and el.role == "AXCheckBox"]
            if boxes and "InnerGo" in titles and "OuterBtn" in titles:
                return shot, boxes[0]
        time.sleep(0.5)
    return shot, None


def _titled_child_frame_exposes_its_checkbox(tmp_path, *, cross_origin: bool) -> None:
    """Live Firefox. A child page with its own title is in the snapshot and in find.

    Same-origin and cross-origin both. The parent is Titled Parent. The child
    is Titled Child, with a checkbox named Agree and a button named InnerGo.
    No third-party captcha host.
    """
    from a11y_computer_use.drivers.linux import LinuxDriver

    driver = LinuxDriver()
    _require_bus(driver)
    if cross_origin:
        child = _serve_pages("127.0.0.1", {"/": _CHILD_PAGE})
        child_port = child.server_address[1]
        parent = _serve_pages("127.0.0.1", {"/": _parent_page(f"http://localhost:{child_port}/")})
        parent_port = parent.server_address[1]
        url = f"http://127.0.0.1:{parent_port}/"
        servers = (parent, child)
    else:
        parent = _serve_pages("127.0.0.1", {"/": _parent_page("/child"), "/child": _CHILD_PAGE})
        url = f"http://127.0.0.1:{parent.server_address[1]}/"
        servers = (parent,)
    proc = None
    try:
        proc = _launch_firefox(tmp_path, url)
        driver._proc = proc
        runtime = _runtime(tmp_path, driver)
        shot, box = _wait_titled_frame(driver, runtime)
        assert box is not None, [
            (el.role, el.title, el.checked) for el in (shot.elements if shot else [])
        ]
        assert box.checked is not True
        found = observe.find_elements(shot, text="Agree")
        assert any(el.role == "AXCheckBox" for el in found), [(el.role, el.title) for el in found]
        clicked = runtime.click(box.ref)
        assert str(clicked).startswith("clicked"), clicked
        deadline = time.monotonic() + 8
        checked = None
        while time.monotonic() < deadline:
            shot = driver.snapshot(Scope.WINDOW, "firefox")
            again = next(
                (el for el in shot.elements if el.title == "Agree" and el.role == "AXCheckBox"),
                None,
            )
            checked = None if again is None else again.checked
            if checked is True:
                break
            time.sleep(0.3)
        assert checked is True, [
            (el.role, el.title, el.checked) for el in shot.elements
        ]
        assert observe.find_elements(shot, text="InnerGo")
    finally:
        if proc is not None:
            _stop(proc)
            proc._log.close()
        for server in servers:
            server.shutdown()


def test_linux_firefox_same_origin_titled_iframe_checkbox(tmp_path) -> None:
    """Live Firefox. A same-origin child frame with its own title stays in the tree."""
    _titled_child_frame_exposes_its_checkbox(tmp_path, cross_origin=False)


def test_linux_firefox_cross_origin_titled_iframe_checkbox(tmp_path) -> None:
    """Live Firefox. A cross-origin child frame with its own title stays in the tree.

    The parent is ``http://127.0.0.1`` and the child is ``http://localhost``.
    """
    _titled_child_frame_exposes_its_checkbox(tmp_path, cross_origin=True)


def test_firefox_set_value_and_type_land_in_web_fields(firefox_form, tmp_path) -> None:
    """Live Firefox. set_value fills the four controls. type inserts after a ref click."""
    driver = firefox_form
    runtime = _runtime(tmp_path, driver)
    runtime.desktop_snapshot("firefox")
    snap = runtime._current
    assert snap is not None
    name = _field(snap, "Name")
    count = _field(snap, "Count")
    notes = _field(snap, "Notes")
    color = _field(snap, "Color")
    assert name and count and notes and color, [(el.role, el.title, el.value) for el in snap.elements]

    assert runtime.set_value(name.ref, "Ann Lee").startswith("set ")
    assert runtime.set_value(count.ref, "12").startswith("set ")
    assert runtime.set_value(notes.ref, "line1").startswith("set ")
    assert runtime.set_value(color.ref, "Blue").startswith("set ")

    after = _wait_values(driver, {"Name": "Ann Lee", "Count": "12", "Notes": "line1", "Color": "Blue"})
    assert after, _dump(driver)
    blob = " ".join(f"{el.title or ''} {el.value or ''}" for el in after.elements)
    assert "t:input" in blob or "t:change" in blob, blob

    runtime.desktop_snapshot("firefox")
    cleared = runtime._current
    name = _field(cleared, "Name")
    count = _field(cleared, "Count")
    notes = _field(cleared, "Notes")
    assert name and count and notes
    assert runtime.set_value(name.ref, "").startswith("set ")
    assert runtime.set_value(count.ref, "").startswith("set ")
    assert runtime.set_value(notes.ref, "").startswith("set ")
    runtime.desktop_snapshot("firefox")
    empty = runtime._current
    name = _field(empty, "Name")
    count = _field(empty, "Count")
    notes = _field(empty, "Notes")
    assert name and name.value in (None, "")
    assert runtime.click(name.ref).startswith("clicked ")
    assert runtime.type_text("Bo") == "typed 2 characters"
    assert runtime.click(count.ref).startswith("clicked ")
    assert runtime.type_text("9") == "typed 1 characters"
    assert runtime.click(notes.ref).startswith("clicked ")
    assert runtime.type_text("Hi") == "typed 2 characters"
    typed = _wait_values(driver, {"Name": "Bo", "Count": "9", "Notes": "Hi"})
    assert typed, _dump(driver)


def test_firefox_hidden_tabs_are_absent_and_not_clickable(firefox_form, tmp_path) -> None:
    """Live Firefox. Background tab and the preloaded New Tab are not targets."""
    driver = firefox_form
    runtime = _runtime(tmp_path, driver)
    text = runtime.desktop_snapshot("firefox", mode="full")
    snap = runtime._current
    titles = {el.title for el in snap.elements}
    assert "Name" in titles
    assert "Products" not in titles
    assert "Wikipedia" not in titles
    assert "Wikipedia-bg" not in titles
    assert "This is a background tab." not in titles
    # The toolbar can have a New Tab button. The preloaded page is an AXGroup
    # document, and that document is what has to be gone.
    assert not any(el.title == "New Tab" and el.role == "AXGroup" for el in snap.elements)
    assert "Firefox Privacy Notice" in titles  # the tab itself is on screen
    assert "Products" not in text
    assert "Wikipedia" not in text
    found = runtime.find("firefox", text="Wikipedia")
    assert "no elements match" in found
    found_products = runtime.find("firefox", text="Products")
    assert "no elements match" in found_products

    link = _hidden_link(driver)
    assert link is not None, "the background Products link was not in the raw AT-SPI tree"
    element = Element(
        "e49", "AXLink", "Products", "Products",
        Bounds(0, 8, 234, 67, 23), "snap-hidden", clickable=True, enabled=True,
    )
    observe._register_epoch("snap-hidden", {}, {"e49": link})
    with pytest.raises(ComputerUseError) as exc:
        driver.press_element(element)
    assert exc.value.code is ErrorCode.UNSUPPORTED
    assert exc.value.detail["reason"] == "not_showing"
    with pytest.raises(ComputerUseError) as exc:
        driver.click(element)
    assert exc.value.detail["reason"] == "not_showing"
    again = driver.snapshot(Scope.WINDOW, "firefox")
    assert "Name" in {el.title for el in again.elements}
    assert not any(el.title == "Products" for el in again.elements)


def test_firefox_background_ref_is_not_showing_not_stale(firefox_form, tmp_path) -> None:
    """Live Firefox. A ref issued while the form was showing is not_showing
    after that tab is in the background. The node is still alive. The error
    is refused, and it does not say re-observe."""
    driver = firefox_form
    runtime = _runtime(tmp_path, driver)
    runtime.desktop_snapshot("firefox")
    snap = runtime._current
    assert snap is not None
    name = _field(snap, "Name")
    assert name is not None, _dump(driver)
    ref = name.ref
    tab = next(
        (el for el in snap.elements if el.title == "Firefox Privacy Notice" and "tab" in el.role.lower()),
        None,
    )
    if tab is None:
        tab = next((el for el in snap.elements if el.title == "Firefox Privacy Notice"), None)
    assert tab is not None, _dump(driver)
    runtime.click(tab.ref)
    deadline = time.monotonic() + 6
    hidden = False
    while time.monotonic() < deadline:
        seen = driver.snapshot(Scope.WINDOW, "firefox")
        if not any(el.title == "Name" for el in seen.elements):
            hidden = True
            break
        time.sleep(0.25)
    assert hidden, _dump(driver)
    try:
        with pytest.raises(ComputerUseError) as exc:
            runtime.click(ref)
        assert exc.value.code is ErrorCode.UNSUPPORTED
        assert exc.value.detail["reason"] == "not_showing"
        assert exc.value.detail["outcome"] == "refused"
        assert "re-observe" not in exc.value.message
    finally:
        # Firefox exposes the tab as a button, so a role that contains "tab"
        # is not there. The ref has to come from the runtime snapshot: a
        # driver snapshot ref is not in the epoch click resolves.
        runtime.desktop_snapshot("firefox")
        back = runtime._current
        form_tab = None
        if back is not None:
            form_tab = next(
                (
                    el for el in back.elements
                    if el.title == "Form Probe" and "tab" in el.role.lower()
                ),
                None,
            )
            if form_tab is None:
                form_tab = next(
                    (el for el in back.elements if el.title == "Form Probe" and el.clickable),
                    None,
                )
        if form_tab is not None:
            try:
                runtime.click(form_tab.ref)
            except ComputerUseError:
                pass
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            seen = driver.snapshot(Scope.WINDOW, "firefox")
            titles = {el.title for el in seen.elements}
            if "Name" in titles and "Editor A" in titles:
                break
            time.sleep(0.25)


def test_firefox_type_and_key_with_app_carry_an_outcome(firefox_form, tmp_path) -> None:
    """Live Firefox. ``type`` and ``key`` with ``app=`` carry outcome and evidence.

    The sentence stays. The outcome is the same confirmed read-back the call
    without ``app=`` already returns.
    """
    driver = firefox_form
    runtime = _runtime(tmp_path, driver)
    runtime.desktop_snapshot("firefox")
    snap = runtime._current
    assert snap is not None
    name = _field(snap, "Name")
    assert name is not None, _dump(driver)
    runtime.click(name.ref)
    typed = runtime.type_text("xy", app="firefox")
    assert str(typed).startswith("typed ")
    assert typed.outcome == "confirmed", (typed.outcome, getattr(typed, "evidence", None))
    assert typed.evidence
    pressed = runtime.key("BackSpace", app="firefox")
    assert str(pressed).startswith("pressed BackSpace")
    assert pressed.outcome == "confirmed", (pressed.outcome, pressed.evidence)
    assert pressed.evidence


def test_firefox_second_inert_click_without_a_snapshot_is_suspected_noop(firefox_form, tmp_path) -> None:
    """Live Firefox. A second click on Div button, with no snapshot between, is a no-op."""
    driver = firefox_form
    runtime = _runtime(tmp_path, driver)
    runtime.desktop_snapshot("firefox")
    snap = runtime._current
    assert snap is not None
    button = next((el for el in snap.elements if el.title == "Div button" and el.clickable), None)
    assert button is not None, _dump(driver)
    first = runtime.click(button.ref)
    second = runtime.click(button.ref)
    assert str(second).startswith("clicked ")
    assert second.outcome == "suspected_noop", (
        first.outcome, second.outcome, getattr(second, "evidence", None),
    )
    assert "did not change" in second.evidence


def _wait_values(driver, expected: dict[str, str], timeout_s: float = 4.0):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = driver.snapshot(Scope.WINDOW, "firefox")
        if all(_value_is(last, title, value) for title, value in expected.items()):
            return last
        time.sleep(0.2)
    return None


def _value_is(snap, title: str, value: str) -> bool:
    for el in snap.elements:
        if el.title == title and (el.value or "") == value:
            return True
    return False


def _dump(driver) -> str:
    try:
        snap = driver.snapshot(Scope.WINDOW, "firefox")
    except ComputerUseError as exc:
        return exc.message
    return "\n".join(f"{el.ref} {el.role} {el.title!r}={el.value!r}" for el in snap.elements)


def _editor_text(snap, title: str) -> str:
    el = next((item for item in snap.elements if item.title == title), None)
    if el is None:
        return ""
    return str(el.value or "").replace("\u00a0", " ").replace("\ufffc", "")


def test_firefox_contenteditable_type_and_set_value(firefox_form, tmp_path) -> None:
    """Live Firefox ESR. type and set_value on a contenteditable.

    The page is the same window as the form tests. Editor A starts as
    "Hello world". Editor B is two paragraphs. This is not a synthetic tree.
    """
    from a11y_computer_use import observe

    driver = firefox_form
    runtime = _runtime(tmp_path, driver)
    driver.activate_app("firefox")

    def current():
        runtime.desktop_snapshot("firefox")
        return runtime._current

    shot = current()
    editor = next(
        (el for el in shot.elements if el.title == "Editor A" and (el.editable or el.role == "AXTextField")),
        None,
    )
    assert editor is not None, observe.render_text(shot)
    runtime._current = shot
    assert runtime.click(editor.ref).startswith("clicked ")
    runtime.key("ctrl+end")
    typed = runtime.type_text("  two spaces end ")
    assert typed.startswith("typed "), typed
    deadline = time.monotonic() + 4
    shown = ""
    last = shot
    while time.monotonic() < deadline:
        last = current()
        shown = _editor_text(last, "Editor A")
        if "two spaces end" in shown:
            break
        time.sleep(0.25)
    assert shown.count("two spaces end") == 1, observe.render_text(last)

    other = next(el for el in last.elements if el.title == "Editor B")
    runtime._current = last
    assert runtime.click(other.ref).startswith("clicked ")
    typed = runtime.type_text("ZZ")
    assert typed.startswith("typed "), typed
    deadline = time.monotonic() + 4
    blob = ""
    while time.monotonic() < deadline:
        last = current()
        blob = " ".join(
            f"{el.title or ''} {el.value or ''}" for el in last.elements
            if el.title in {"Editor B", "Editor A"} or "ZZ" in f"{el.title or ''} {el.value or ''}"
        ).replace("\ufffc", "")
        if blob.count("ZZ") == 1:
            break
        time.sleep(0.25)
    assert blob.count("ZZ") == 1, observe.render_text(last)

    editor = next(el for el in last.elements if el.title == "Editor A")
    runtime._current = last
    assert runtime.click(editor.ref).startswith("clicked ")
    runtime.key("ctrl+a")
    runtime.key("backspace")
    typed = runtime.type_text(" more")
    assert typed.startswith("typed "), typed
    deadline = time.monotonic() + 4
    shown = ""
    while time.monotonic() < deadline:
        last = current()
        shown = _editor_text(last, "Editor A")
        if "more" in shown:
            break
        time.sleep(0.25)
    assert shown.count("more") == 1, observe.render_text(last)

    editor = next(el for el in last.elements if el.title == "Editor A" and el.editable)
    runtime._current = last
    assert runtime.set_value(editor.ref, "Set 0").startswith("set ")
    deadline = time.monotonic() + 4
    shown = ""
    while time.monotonic() < deadline:
        last = current()
        shown = _editor_text(last, "Editor A").strip()
        if shown == "Set 0":
            break
        time.sleep(0.25)
    assert shown == "Set 0", observe.render_text(last)


def _hidden_link(driver):
    """The Products link in the background document, which the snapshot must not list."""
    from a11y_computer_use.drivers import _atspi

    root = driver._run(lambda: _atspi.find_root("firefox", Scope.WINDOW))
    if root is None:
        return None

    found = []

    def walk(node, depth: int) -> None:
        if node is None or depth > 18 or found:
            return
        role = _atspi._role_name(node)
        if role == "link" and _atspi._node_name(node) == "Products":
            found.append(node)
            return
        count = min(_atspi._child_count(node), 40)
        for index in range(count):
            walk(_atspi._child_at(node, index), depth + 1)

    driver._run(lambda: walk(root, 0))
    return found[0] if found else None


_CROP_SCROLL_PAGE = (
    "<!doctype html><meta charset=utf-8><title>cuacropscroll</title>"
    "<style>body{margin:0;font:16px/24px sans-serif}"
    "#swatch{display:block;width:200px;height:60px;margin:8px;background:#c00;"
    "color:#fff;border:0}.pad{height:240px}</style>"
    "<p>Lead paragraph</p><button id=swatch type=button>Red swatch</button>"
    + "".join("<div class=pad>pad</div>" for _ in range(40))
    + "<p id=tail>Tail marker</p>"
)


def test_linux_firefox_crop_of_a_scrolled_off_button_is_off_screen(tmp_path) -> None:
    """crop of a button End scrolled off the page is not_visible, not stale_ref.

    Live Firefox on a local page. The ref stays the one from before the scroll.
    The error reason is off_screen and the hint is scroll(ref, into_view=true).
    A missing Firefox binary fails. Padding does not turn the miss into a crop.
    """
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import ScrollUnit

    driver = LinuxDriver()
    _require_bus(driver)
    if _firefox_binary() is None:
        pytest.fail("Firefox is not installed; the Linux live job installs it when it is missing")
    page = tmp_path / "cuacropscroll.html"
    page.write_text(_CROP_SCROLL_PAGE)
    proc = _launch_firefox(tmp_path, page.as_uri())
    try:
        deadline = time.monotonic() + 45
        snap = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                log = (tmp_path / "firefox.log").read_text(encoding="utf-8", errors="replace")[-1500:]
                raise AssertionError(f"Firefox exited {proc.returncode}\n{log}")
            try:
                shot = driver.snapshot(Scope.WINDOW, "firefox")
            except ComputerUseError as exc:
                if exc.code is not ErrorCode.APP_NOT_FOUND:
                    raise
                shot = None
            else:
                if any(el.title == "Red swatch" and el.bounds.height >= 40 for el in shot.elements):
                    snap = shot
                    break
            time.sleep(0.4)
        assert snap is not None, "Firefox did not expose Red swatch"
        runtime = _runtime(tmp_path, driver)
        runtime._current = snap
        lead = next((el for el in snap.elements if el.title == "Lead paragraph"), None)
        if lead is not None:
            runtime.click(lead.ref)
            snap = runtime._current or snap
        swatch = next(el for el in snap.elements if el.title == "Red swatch" and el.bounds.height >= 40)
        runtime._current = snap
        ref = swatch.ref
        fresh = snap
        for _ in range(12):
            fresh = driver.snapshot(Scope.WINDOW, "firefox")
            if not any(el.title == "Red swatch" for el in fresh.elements):
                break
            runtime.key("end", app="firefox")
            runtime.key("ctrl+end", app="firefox")
            runtime.key("pagedown", app="firefox")
            try:
                driver.scroll(swatch, dy=1200, unit=ScrollUnit.PIXELS)
            except ComputerUseError:
                pass
            time.sleep(0.25)
        else:
            titles = sorted({el.title for el in fresh.elements if el.title})
            raise AssertionError(f"Red swatch stayed in the Firefox tree: {titles[:40]}")
        assert runtime._current.element(ref).title == "Red swatch"
        try:
            runtime.crop(ref, padding=512)
        except ComputerUseError as exc:
            assert exc.code is ErrorCode.NOT_VISIBLE, exc
            assert exc.detail["reason"] == "off_screen"
            assert "still valid" in exc.message
            assert exc.detail["hint"] == f"scroll(ref={ref!r}, into_view=true)"
            assert "Reload" not in exc.message
            assert "stale" not in exc.message
        else:
            raise AssertionError("crop of a scrolled-off button must be not_visible")
    finally:
        _stop(proc)
