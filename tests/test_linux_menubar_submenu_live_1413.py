"""#1413 live: a click on an ARIA menubar submenu item lands, no Escape first.

Live Chrome on Linux, APG-style menubar: File > Recent > Report.txt. With
File and Recent open, a click on Report.txt used to send Escape (the open
menu chain was dismissed), and the call then reported target_vanished.
"""
from __future__ import annotations

import re
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux backend")

from a11y_computer_use.schema import ComputerUseError, ErrorCode, Scope  # noqa: E402
from tests.test_linux_live import _launch_chrome_file, _runtime_for  # noqa: E402

_HTML = """<!doctype html><html><head><title>menubar</title><style>
[role=menu]{display:none;border:1px solid #888;padding:4px;background:#fff}
[role=menu].open{display:block}
li{list-style:none;padding:2px 8px}
</style></head><body>
<ul role=menubar aria-label="Main" style="display:flex;gap:16px">
 <li role=none><span role=menuitem id=file tabindex=0 aria-haspopup=menu aria-expanded=false>File</span>
  <ul role=menu id=filemenu aria-label=File>
   <li role=none><span role=menuitem tabindex=-1 id=newdoc>New</span></li>
   <li role=none><span role=menuitem id=recent tabindex=-1 aria-haspopup=menu aria-expanded=false>Recent</span>
    <ul role=menu id=recentmenu aria-label=Recent>
     <li role=none><span role=menuitem tabindex=-1 id=report>Report.txt</span></li>
    </ul></li>
  </ul></li>
 <li role=none><span role=menuitem tabindex=-1 id=edit>Edit</span></li>
</ul>
<button id=outside>Outside</button>
<script>
function openMenu(btn, menu){btn.setAttribute('aria-expanded','true');menu.classList.add('open');}
function closeAll(){for(const b of document.querySelectorAll('[aria-expanded]'))b.setAttribute('aria-expanded','false');
 for(const m of document.querySelectorAll('[role=menu]'))m.classList.remove('open');}
file.onclick=()=>openMenu(file,filemenu);
recent.onclick=(e)=>{e.stopPropagation();openMenu(recent,recentmenu);};
report.onclick=(e)=>{e.stopPropagation();document.title='picked report';closeAll();};
document.addEventListener('keydown',e=>{if(e.key==='Escape'){document.title='escaped';closeAll();}});
</script></body></html>"""


def test_linux_chrome_aria_submenu_item_click_sends_no_escape(tmp_path) -> None:
    driver, proc = _launch_chrome_file(tmp_path, "menubar1413", _HTML)
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError(f"Chrome exited with status {proc.returncode}")
            try:
                shot = driver.snapshot(Scope.WINDOW, "chrome")
            except ComputerUseError as exc:
                if exc.code is not ErrorCode.APP_NOT_FOUND:
                    raise
            else:
                # The page's own menubar item, not Chrome's "File" address-bar
                # chip that a file:// page also shows.
                if any(el.role == "AXMenuItem" and el.title == "File" for el in shot.elements):
                    break
            time.sleep(0.3)
        else:
            raise AssertionError("Chrome did not show the menubar")
        runtime = _runtime_for(tmp_path, driver, "chrome", "google-chrome")

        def window_title() -> str:
            return " ".join(el.title or "" for el in driver.snapshot(Scope.WINDOW, "chrome").elements
                            if el.role == "AXWindow")

        def click_named(name: str) -> str:
            # Scope to the page menubar's menuitem role. A bare text match
            # picked Chrome's "File" address-bar chip on file:// pages.
            found = runtime.find("chrome", text=name, role="menuitem")
            match = re.search(rf"\b(e\d+) menuitem \"{re.escape(name)}\"", found)
            assert match, found
            return runtime.click(match.group(1))

        click_named("File")
        click_named("Recent")
        result = click_named("Report.txt")
        assert "closed open menu" not in result, result
        title = ""
        for _ in range(20):
            title = window_title()
            if "picked report" in title or "escaped" in title:
                break
            time.sleep(0.2)
        assert "escaped" not in title, title
        assert "picked report" in title, title
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
