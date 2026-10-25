#!/usr/bin/env python3
"""Profile #1072's pooled 10k-row Chrome list in a private X11/D-Bus session.

Usage: python scripts/box/profile_scroll_find.py CHECKOUT OUTPUT_DIR TARGET MAX_SCROLLS
Use a fresh OUTPUT_DIR for each run. The click-only grant isolates scroll costs
from browser Find keystrokes. cProfile covers the first step; all snapshot and
step durations are printed. Chrome is launched without a CDP endpoint.
"""
from __future__ import annotations

import argparse
import cProfile
import os
from pathlib import Path
import pstats
import signal
import subprocess
import sys
import time


def main() -> None:
    """Measure a selected checkout without changing its source or test limits."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("target", type=int)
    parser.add_argument("max_scrolls", type=int)
    args = parser.parse_args()
    sys.path.insert(0, str(args.checkout.resolve()))
    from a11y_computer_use import server, safety
    from a11y_computer_use.drivers.linux import LinuxDriver
    from a11y_computer_use.schema import Scope
    out = args.output_dir.resolve()
    out.mkdir(exist_ok=True)
    page = out / "virtual.html"
    page.write_text('''<!doctype html><title>Virtual probe</title><div id="vp" role="list" aria-label="Virtual rows" style="height:420px;width:520px;overflow-y:auto;position:relative"><div id="inner" style="position:relative;height:300000px"></div></div><script>
    const N=10000,RH=30,OS=3,vp=document.getElementById('vp'),inner=document.getElementById('inner'),pool=[];
    function mk(){const d=document.createElement('div');d.setAttribute('role','listitem');d.style.cssText='position:absolute;left:0;right:0;height:30px;display:flex;gap:8px';const s=document.createElement('span'),b=document.createElement('button');d.append(s,b);b.onclick=()=>document.title='clicked '+d.dataset.i;inner.appendChild(d);return d}
    function render(){const f=Math.max(0,Math.floor(vp.scrollTop/RH)-OS),l=Math.min(N-1,Math.ceil((vp.scrollTop+420)/RH)+OS);while(pool.length<l-f+1)pool.push(mk());pool.forEach((d,j)=>{const k=f+j;if(k>l){d.style.display='none';return}d.style.display='';d.dataset.i=k;d.style.top=(k*RH)+'px';d.children[0].textContent='Row '+k;d.children[1].textContent='Open '+k})}vp.onscroll=render;render();</script>''')
    proc = subprocess.Popen([
        "/usr/bin/google-chrome", "--force-renderer-accessibility", "--no-sandbox",
        "--disable-gpu", "--disable-dev-shm-usage", "--no-first-run",
        "--no-default-browser-check", f"--user-data-dir={out}/profile",
        "--window-size=1000,700", page.as_uri(),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        driver = LinuxDriver()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                shot = driver.snapshot(Scope.WINDOW, "chrome")
                if any(el.title == "Open 0" for el in shot.elements):
                    break
            except Exception:
                pass  # Chrome may not have registered on AT-SPI yet.
            time.sleep(0.2)
        else:
            raise RuntimeError("no virtual rows")
        store = safety.PermissionStore(out / "permissions.json")
        for app in ("chrome", "google-chrome", driver.frontmost_app()[0]):
            if app:
                store.set_tier(app, safety.Tier.CLICK)
        runtime = server.Runtime(
            store=store, audit=safety.AuditLog(out / "audit"), driver=driver,
        )
        original_snapshot = driver.snapshot
        snapshots = []

        def snapshot(*args, **kwargs):
            start = time.monotonic()
            try:
                return original_snapshot(*args, **kwargs)
            finally:
                snapshots.append(time.monotonic() - start)

        driver.snapshot = snapshot
        method = "scroll_search_step" if hasattr(driver, "scroll_search_step") else "scroll"
        original_scroll = getattr(driver, method)
        steps = []

        def measured(*args, **kwargs):
            profile = cProfile.Profile()
            start = time.monotonic()
            if not steps:
                profile.enable()
            try:
                return original_scroll(*args, **kwargs)
            finally:
                profile.disable()
                steps.append(time.monotonic() - start)
                if len(steps) == 1:
                    print("FIRST STEP", steps[-1], flush=True)
                    pstats.Stats(profile).sort_stats("cumulative").print_stats(18)

        setattr(driver, method, measured)
        start = time.monotonic()
        result = runtime.scroll_to_find(
            "chrome", text=f"Open {args.target}", budget_s=90, max_scrolls=args.max_scrolls,
        )
        print("RESULT", result, flush=True)
        print("TIMINGS total", time.monotonic() - start, "steps", steps,
              "snapshots", snapshots, flush=True)
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)



if __name__ == "__main__":
    main()
