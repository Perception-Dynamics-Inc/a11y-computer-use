"""Real Thunar navigation regression; run under an isolated X11 session."""
import os
import shutil
import subprocess
import sys
import time

import pytest

from a11y_computer_use import safety, server
from a11y_computer_use.drivers.linux import LinuxDriver


@pytest.mark.skipif(sys.platform != 'linux' or not os.environ.get('DISPLAY'),
                    reason='requires Linux X11 desktop')
def test_thunar_set_path_then_return_navigates(tmp_path):
    binary = shutil.which('thunar')
    if not binary:
        pytest.skip('Thunar is not installed')
    driver = LinuxDriver()
    driver.ensure_trusted()
    destination = tmp_path / 'navigation-destination'
    destination.mkdir()
    proc = subprocess.Popen([binary, str(tmp_path)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        store = safety.PermissionStore(tmp_path / 'permissions.json')
        store.set_tier('thunar', safety.Tier.FULL)
        runtime = server.Runtime(store=store, audit=safety.AuditLog(tmp_path / 'audit'), driver=driver)
        deadline = time.monotonic() + 10
        while True:
            try:
                runtime.desktop_snapshot('thunar')
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.2)
        runtime.key('ctrl+l', app='thunar')
        runtime.desktop_snapshot('thunar')
        entry = next(e for e in runtime._current.elements if e.editable and e.focused)
        result = runtime.set_value(entry.ref, str(destination))
        assert result.outcome == 'confirmed', (result, result.evidence)
        result = runtime.key('Return', app='thunar')
        assert result.outcome == 'confirmed', (result, result.evidence)
        runtime.desktop_snapshot('thunar')
        snap = runtime._current
        assert any(e.role == 'AXWindow' and destination.name in e.title for e in snap.elements)
        assert any(e.editable and str(e.value).rstrip('/') == str(destination) for e in snap.elements)
    finally:
        proc.terminate()
        proc.wait(timeout=5)
