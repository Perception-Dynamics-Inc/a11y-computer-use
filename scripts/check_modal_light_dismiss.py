"""Check #1073 light dismissal and #1013 refusal under private Xvfb/D-Bus.

Run from the repository root with PYTHONPATH=. and FIREFOX_BINARY set when
Firefox is not on PATH. Uses AT-SPI and actual pointer clicks, not CDP input.
"""

import os
from pathlib import Path
import subprocess
import tempfile
import time
from a11y_computer_use.drivers.linux import LinuxDriver
from a11y_computer_use.schema import Point, Click, Scope, ComputerUseError


def main() -> None:
    fixture = (
        Path(__file__).resolve().parents[1] / "tests/fixtures/modal_light_dismiss.html"
    )
    driver = LinuxDriver()
    driver.ensure_trusted()
    for browser in ("chrome", "firefox"):
        with tempfile.TemporaryDirectory(prefix="1073-" + browser) as profile:
            if browser == "chrome":
                args = [
                    "google-chrome",
                    "--force-renderer-accessibility",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--password-store=basic",
                    "--window-size=1280,800",
                    f"--user-data-dir={profile}",
                    fixture.as_uri(),
                ]
            else:
                Path(profile, "user.js").write_text(
                    'user_pref("browser.shell.checkDefaultBrowser",false);user_pref("browser.startup.homepage_override.mstone","ignore");user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification",true);user_pref("browser.aboutwelcome.enabled",false);user_pref("browser.startup.firstrunSkipsHomepage",true);user_pref("browser.startup.homepage_override.once", "");user_pref("toolkit.telemetry.reportingpolicy.firstRun",false);'
                )
                args = [
                    os.environ.get("FIREFOX_BINARY", "firefox"),
                    "--no-remote",
                    "--profile",
                    profile,
                    "--width",
                    "1280",
                    "--height",
                    "800",
                    fixture.as_uri(),
                ]
            with open("/tmp/1073-" + browser + ".log", "w") as log:
                p = subprocess.Popen(args, stdout=log, stderr=log)
                try:
                    for trial in range(3):
                        deadline = time.monotonic() + 30
                        while True:
                            try:
                                snap = driver.snapshot(Scope.WINDOW, browser)
                                if any(
                                    (e.title == "Photo viewer" for e in snap.elements)
                                ):
                                    break
                            except Exception:
                                pass
                            assert time.monotonic() < deadline, "Modal did not appear"
                            time.sleep(0.3)
                        background = next(
                            (
                                e
                                for e in snap.elements
                                if e.title == "Background Action"
                                and e.role == "AXButton"
                            )
                        )
                        assert background.bounds is not None
                        for action in (
                            lambda: driver.press_element(background),
                            lambda: driver.click(background.bounds.center),
                        ):
                            try:
                                action()
                            except ComputerUseError as exc:
                                assert (
                                    exc.detail.get("reason") == "outside_active_modal"
                                ), exc
                            else:
                                raise AssertionError("Background input was allowed")
                        guarded = driver.snapshot(Scope.WINDOW, browser)
                        assert any(
                            (e.title == "Photo viewer" for e in guarded.elements)
                        )
                        assert not any(
                            (
                                "BACKGROUND ACTIVATED" in e.title
                                for e in guarded.elements
                            )
                        )
                        print(browser, "PASS background guard", trial + 1, flush=True)
                        point = Point(0, 1000, 650)
                        driver.check_modal_input(Click(point), browser)
                        driver.click(point)
                        deadline = time.monotonic() + 5
                        while True:
                            closed = driver.snapshot(Scope.WINDOW, browser)
                            if any(
                                ("Lightbox closed" in e.title for e in closed.elements)
                            ):
                                break
                            assert time.monotonic() < deadline, closed
                            time.sleep(0.1)
                        print(browser, "PASS dismiss", trial + 1, flush=True)
                        subprocess.run(["xdotool", "key", "ctrl+r"], check=True)
                        time.sleep(0.4)
                finally:
                    p.terminate()
                    p.wait(timeout=15)


if __name__ == "__main__":
    main()
