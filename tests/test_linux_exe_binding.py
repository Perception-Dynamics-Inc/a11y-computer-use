"""Linux grants bound to the executable realpath (Refs the spoof issue).

Real processes stand in for the five spoofs Tester found: a comm set with
prctl, a symlink, a copied interpreter, a shebang script and a compiled
launcher, each named after a granted app. Every one must be keyed as the
interpreter or refused; the genuine binary keeps its grant.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
import time
from types import SimpleNamespace as _NS

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux /proc")

from a11y_computer_use.drivers import _linux_exe  # noqa: E402

PY = os.path.realpath(sys.executable)
NAME = "zzgrant1296"  # unique, so no other process on the box shares it


def _wait_named(name, proc, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.pid in _linux_exe.pids_named(name):
            return
        time.sleep(0.02)
    raise AssertionError(f"{name} never appeared")


@pytest.fixture
def spawn():
    procs = []

    def run(argv, name=NAME, **kw):
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, **kw)
        procs.append(proc)
        _wait_named(name, proc)
        return proc

    yield run
    for proc in procs:
        proc.kill()
        proc.wait()


def _sleeper_code(name=None):
    rename = f"open('/proc/self/comm','w').write({name!r});" if name else ""
    return f"import time;{rename}time.sleep(60)"


def _native(tmp_path):
    """A genuine native binary named NAME in a dir standing in for /usr/bin."""
    real = shutil.which("sleep")
    target = tmp_path / "bin" / NAME
    target.parent.mkdir()
    shutil.copy2(real, target)
    return os.path.realpath(target)


def test_n1_prctl_comm_is_keyed_as_the_interpreter(spawn, tmp_path) -> None:
    spawn([PY, "-c", _sleeper_code(NAME)])
    assert _linux_exe.check(NAME, _native(tmp_path)) == ("python3", None)


def test_n2_symlink_is_keyed_as_the_interpreter(spawn, tmp_path) -> None:
    link = tmp_path / NAME
    link.symlink_to(PY)
    spawn([str(link), "-c", _sleeper_code()])
    assert _linux_exe.check(NAME, _native(tmp_path)) == ("python3", None)


def test_n3_copied_interpreter_is_refused(spawn, tmp_path) -> None:
    copy = tmp_path / NAME
    shutil.copy2(PY, copy)
    spawn([str(copy), "-c", _sleeper_code()])
    key, hint = _linux_exe.check(NAME, _native(tmp_path))
    assert key == NAME and hint and str(copy) in hint


def test_n4_shebang_script_is_keyed_as_the_interpreter(spawn, tmp_path) -> None:
    script = tmp_path / NAME
    script.write_text(f"#!{PY}\n{_sleeper_code()}\n")
    script.chmod(0o755)
    spawn([str(script)])
    assert _linux_exe.check(NAME, _native(tmp_path)) == ("python3", None)


def test_n6_compiled_launcher_is_refused(spawn, tmp_path) -> None:
    gcc = shutil.which("gcc")
    config = shutil.which("python3-config")
    if not gcc or not config:
        pytest.skip("gcc and python3-config build the launcher")
    src = tmp_path / "l.c"
    src.write_text("#include <Python.h>\nint main(int c,char**v){return Py_BytesMain(c,v);}\n")
    launcher = tmp_path / NAME
    flags = subprocess.run([config, "--cflags", "--embed", "--ldflags"], capture_output=True, text=True)
    if flags.returncode:
        pytest.skip("python3-config has no --embed")
    built = subprocess.run([gcc, str(src), "-o", str(launcher), *flags.stdout.split()], capture_output=True)
    if built.returncode:
        pytest.skip("launcher did not build")
    spawn([str(launcher), "-c", _sleeper_code()])
    key, hint = _linux_exe.check(NAME, _native(tmp_path))
    assert key == NAME and hint and str(launcher) in hint


def test_the_genuine_binary_keeps_its_grant(spawn, tmp_path) -> None:
    native = _native(tmp_path)
    spawn([native, "60"])
    assert _linux_exe.check(NAME, native) == (NAME, None)


def test_a_copy_of_the_genuine_binary_elsewhere_is_refused(spawn, tmp_path) -> None:
    native = _native(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    shutil.copy2(native, other / NAME)
    spawn([str(other / NAME), "60"])
    assert _linux_exe.check(NAME, native)[1]


def test_a_spoof_beside_the_genuine_app_refuses_the_name(spawn, tmp_path) -> None:
    native = _native(tmp_path)
    spawn([native, "60"])
    spawn([PY, "-c", _sleeper_code(NAME)])
    key, hint = _linux_exe.check(NAME, native)
    assert hint, (key, hint)


def test_a_legacy_grant_needs_the_installed_binary(spawn, tmp_path, monkeypatch) -> None:
    native = _native(tmp_path)
    monkeypatch.setattr(_linux_exe, "installed_exes", lambda name: {native})
    proc = spawn([native, "60"])
    assert _linux_exe.check(NAME, None) == (NAME, None)
    proc.kill()
    proc.wait()
    other = tmp_path / "o"
    other.mkdir()
    shutil.copy2(native, other / NAME)
    spawn([str(other / NAME), "60"])
    key, hint = _linux_exe.check(NAME, None)
    assert hint and "predates executable binding" in hint


def test_system_owned_names_its_own_binary() -> None:
    sleep = os.path.realpath(shutil.which("sleep"))
    if os.stat(sleep).st_uid != 0:
        pytest.skip("sleep is not root-owned here")
    assert _linux_exe.legacy_exe_ok(os.path.basename(sleep), sleep)
    assert not _linux_exe.legacy_exe_ok("mousepad", sleep)


def test_interpreter_key() -> None:
    assert _linux_exe.interpreter_key("/usr/bin/python3.13") == "python3"
    assert _linux_exe.interpreter_key("/usr/bin/perl5.36.0") == "perl5"
    assert _linux_exe.interpreter_key("/usr/bin/gjs-console") is None
    for exe in ("/usr/bin/gjs", "/usr/bin/node", "/usr/bin/bash", "/usr/bin/dash", "/usr/bin/ruby3.1"):
        assert _linux_exe.interpreter_key(exe), exe
    for exe in ("/usr/bin/mousepad", "/usr/lib/libreoffice/program/soffice.bin", None):
        assert _linux_exe.interpreter_key(exe) is None


# --- through the Runtime gate -------------------------------------------------


def _runtime(tmp_path, monkeypatch, tiers, exes=None, deny=()):
    from a11y_computer_use import safety, server

    monkeypatch.setattr(server.sys, "platform", "linux")
    monkeypatch.setattr(server, "_running_app", lambda identifier: (None, identifier))
    store = safety.PermissionStore(tmp_path / "permissions.json")
    for app, tier in tiers.items():
        store.set_tier(app, tier, exe=(exes or {}).get(app))
    for app in deny:
        store.add_deny(app)
    return server.Runtime(store=store, audit=safety.AuditLog(tmp_path / "audit"), driver=_NS(name="linux"))


def _gate(rt, app):
    from a11y_computer_use import safety, server
    from a11y_computer_use.schema import ObserveOp, ObserveVerb

    key = rt._resolve_app(app)[1]
    decision = safety.check_action(ObserveOp(verb=ObserveVerb.SNAPSHOT, app=key), key, store=rt.store)
    return key, decision, server.refusal_text(decision)


@pytest.mark.parametrize("case", ["prctl", "symlink", "copy", "shebang"])
def test_gate_refuses_each_spoof_under_a_name_grant(spawn, tmp_path, monkeypatch, case) -> None:
    native = _native(tmp_path)
    rt = _runtime(tmp_path, monkeypatch, {NAME: "full"}, {NAME: native})
    d = tmp_path / "s"
    d.mkdir()
    if case == "prctl":
        spawn([PY, "-c", _sleeper_code(NAME)])
    elif case == "symlink":
        (d / NAME).symlink_to(PY)
        spawn([str(d / NAME), "-c", _sleeper_code()])
    elif case == "copy":
        shutil.copy2(PY, d / NAME)
        spawn([str(d / NAME), "-c", _sleeper_code()])
    else:
        (d / NAME).write_text(f"#!{PY}\n{_sleeper_code()}\n")
        (d / NAME).chmod(0o755)
        spawn([str(d / NAME)])
    key, decision, text = _gate(rt, NAME)
    assert not decision.allowed, (key, text)
    assert key != NAME


def test_gate_allows_the_genuine_binary(spawn, tmp_path, monkeypatch) -> None:
    native = _native(tmp_path)
    rt = _runtime(tmp_path, monkeypatch, {NAME: "full"}, {NAME: native})
    spawn([native, "60"])
    key, decision, _text = _gate(rt, NAME)
    assert key == NAME and decision.allowed


def test_gate_refusal_explains_the_executable(spawn, tmp_path, monkeypatch) -> None:
    native = _native(tmp_path)
    rt = _runtime(tmp_path, monkeypatch, {NAME: "full"}, {NAME: native})
    d = tmp_path / "s"
    d.mkdir()
    shutil.copy2(PY, d / NAME)
    spawn([str(d / NAME), "-c", _sleeper_code()])
    _key, _decision, text = _gate(rt, NAME)
    assert "not the granted executable" in text and str(d / NAME) in text


def test_gate_deny_wins_over_the_binding(spawn, tmp_path, monkeypatch) -> None:
    native = _native(tmp_path)
    rt = _runtime(tmp_path, monkeypatch, {NAME: "full"}, {NAME: native}, deny=(NAME,))
    spawn([native, "60"])
    key, decision, _text = _gate(rt, NAME)
    assert decision.verdict.value == "deny"


def test_a_python3_grant_covers_the_interpreter_spoof_explicitly(spawn, tmp_path, monkeypatch) -> None:
    rt = _runtime(tmp_path, monkeypatch, {"python3": "full"})
    spawn([PY, "-c", _sleeper_code(NAME)])
    key, decision, _text = _gate(rt, NAME)
    assert key == "python3" and decision.allowed


def test_gate_keeps_a_not_running_name(tmp_path, monkeypatch) -> None:
    rt = _runtime(tmp_path, monkeypatch, {NAME: "full"}, {NAME: "/usr/bin/" + NAME})
    assert rt._resolve_app(NAME)[1] == NAME


def test_the_store_round_trips_the_exe(tmp_path) -> None:
    from a11y_computer_use import safety

    store = safety.PermissionStore(tmp_path / "p.json")
    store.set_tier("mousepad", safety.Tier.FULL, exe="/usr/bin/mousepad")
    again = safety.PermissionStore(tmp_path / "p.json")
    assert again.recorded_exe("mousepad") == "/usr/bin/mousepad"
    assert again.recorded_exe("Mousepad") == "/usr/bin/mousepad"
    again.set_tier("mousepad", safety.Tier.READ)  # omitted exe keeps the binding
    assert safety.PermissionStore(tmp_path / "p.json").recorded_exe("mousepad") == "/usr/bin/mousepad"
    again.revoke("mousepad")
    assert safety.PermissionStore(tmp_path / "p.json").recorded_exe("mousepad") is None
    with pytest.raises(ValueError):
        store.set_tier("x", safety.Tier.FULL, exe="relative/x")


def test_an_old_name_only_file_still_loads(tmp_path) -> None:
    from a11y_computer_use import safety

    (tmp_path / "p.json").write_text('{"apps": {"mousepad": {"tier": "full"}}}')
    store = safety.PermissionStore(tmp_path / "p.json")
    assert store.get_tier("mousepad") is safety.Tier.FULL and store.recorded_exe("mousepad") is None


def test_grant_records_the_installed_binary(monkeypatch) -> None:
    monkeypatch.setattr(_linux_exe, "pids_named", lambda name: set())
    monkeypatch.setattr(_linux_exe, "installed_exes", lambda name: {"/usr/bin/" + name})
    assert _linux_exe.grant_exe("mousepad") == "/usr/bin/mousepad"
    assert _linux_exe.grant_exe("python3") is None


CHROME = "/opt/google/chrome/chrome"


def _procs(monkeypatch, table: dict[int, tuple[str | None, int, tuple[int, ...]]], named=None,
           zombies=None):
    """Fake /proc: pid -> (readable exe or None, PPid, NSpid)."""
    from a11y_computer_use.drivers import _linux_exe as m

    names = set(table) if named is None else set(named)
    monkeypatch.setattr(m, "pids_named", lambda n: set(names))
    monkeypatch.setattr(m, "exe_of", lambda pid: table[pid][0] if pid in table else None)
    monkeypatch.setattr(m, "_status", lambda pid: (table[pid][1], table[pid][2]) if pid in table else (None, ()))
    monkeypatch.setattr(m, "_gone", lambda pid: pid in (zombies or ()))
    return m


def test_unreadable_child_of_a_verified_process_inherits_its_exe(monkeypatch) -> None:
    """Real Chrome (as measured on the box): browser -> zygote (pid 1 of a new
    pid ns) -> zygote child -> renderers (each pid 1 of a nested ns), all unreadable."""
    m = _procs(monkeypatch, {
        10: (CHROME, 1, (10,)),
        11: (None, 10, (11, 1)),
        12: (None, 11, (12, 3)),
        13: (None, 12, (13, 14, 1)),
        14: (None, 12, (14, 26, 1)),
    })
    assert m.check("chrome", CHROME) == ("chrome", None)


def test_unreadable_process_without_a_verified_named_parent_is_refused(monkeypatch) -> None:
    """A spoof that sets PR_SET_DUMPABLE=0 has no verified chrome ancestor."""
    m = _procs(monkeypatch, {10: (CHROME, 1, (10,)), 20: (None, 500, (20,))}, named={10, 20})
    key, hint = m.check("chrome", CHROME)
    assert hint and "(unreadable)" in hint


def test_unreadable_child_of_a_spoofed_parent_is_refused(monkeypatch) -> None:
    m = _procs(monkeypatch, {30: ("/tmp/x/chrome", 1, (30,)), 31: (None, 30, (31, 1))})
    assert m.check("chrome", CHROME)[1]


def test_non_dumpable_launch_flag_child_of_real_chrome_is_refused(monkeypatch) -> None:
    """Tester run 2339: a process real Chrome fork+execs (launch flag) that
    renames itself chrome and sets PR_SET_DUMPABLE=0 stays in the browser's
    pid namespace, so it is not the browser's sandbox namespace init."""
    m = _procs(monkeypatch, {
        10: (CHROME, 1, (10,)),
        11: (None, 10, (11, 1)),      # the genuine zygote still passes
        20: (None, 10, (20,)),        # the spoof
    })
    key, hint = m.check("chrome", CHROME)
    assert hint and "(unreadable)" in hint


def test_non_dumpable_native_messaging_host_with_own_namespace_is_refused(monkeypatch) -> None:
    """A native-messaging host (non-dumpable, same ns as the browser) that
    unshares a pid namespace: its child is a namespace init, but its parent
    is the host, not the verified browser."""
    m = _procs(monkeypatch, {
        10: (CHROME, 1, (10,)),
        20: (None, 10, (20,)),
        21: (None, 20, (21, 1)),
        22: (None, 21, (22, 2)),
    })
    assert m.check("chrome", CHROME)[1]
    m = _procs(monkeypatch, {
        10: (CHROME, 1, (10,)),
        20: (None, 10, (20,)),
        21: (None, 20, (21, 1)),
    }, named={10, 21})                 # the host itself is not named chrome
    assert m.check("chrome", CHROME)[1]


def test_protocol_handler_child_namespace_under_a_foreign_exe_is_refused(monkeypatch) -> None:
    """A protocol handler (readable, its own exe) whose namespace child calls
    itself chrome: the readable ancestor is the handler, refused either way."""
    m = _procs(monkeypatch, {
        10: (CHROME, 1, (10,)),
        20: ("/usr/bin/xdg-open-handler", 10, (20,)),
        21: (None, 20, (21, 1)),
    }, named={10, 21})
    assert m.check("chrome", CHROME)[1]
    m = _procs(monkeypatch, {
        10: (CHROME, 1, (10,)),
        20: ("/home/u/chrome", 10, (20,)),
        21: (None, 20, (21, 1)),
    })
    assert m.check("chrome", CHROME)[1]


def test_unreadable_process_skipping_a_namespace_level_is_refused(monkeypatch) -> None:
    m = _procs(monkeypatch, {
        10: (CHROME, 1, (10,)),
        11: (None, 10, (11, 5, 1)),   # two levels down: not a clone by the browser
    })
    assert m.check("chrome", CHROME)[1]
    m = _procs(monkeypatch, {
        10: (CHROME, 1, (10,)),
        11: (None, 10, (11, 7)),      # deeper but not the namespace init
    })
    assert m.check("chrome", CHROME)[1]


def test_status_reads_the_kernel_ppid_and_nspid() -> None:
    import os

    from a11y_computer_use.drivers import _linux_exe as m

    ppid, nspid = m._status(os.getpid())
    assert ppid == os.getppid() and nspid and nspid[0] == os.getpid()
    assert m._status(2**22 + 12345) == (None, ())


def test_a_zombie_renderer_does_not_refuse_real_chrome(monkeypatch) -> None:
    """CI on 80b8530e: an exited renderer the zygote has not reaped yet has an
    unreadable exe; it is gone, not a spoof, so real Chrome keeps its grant."""
    m = _procs(monkeypatch, {
        10: (CHROME, 1, (10,)),
        11: (CHROME, 10, (11,)),
        12: (None, 11, (12,)),
    }, zombies={12})
    assert m.check("chrome", CHROME) == ("chrome", None)


def test_a_live_non_dumpable_process_is_not_treated_as_gone(monkeypatch) -> None:
    m = _procs(monkeypatch, {10: (CHROME, 1, (10,)), 12: (None, 10, (12,))}, zombies=set())
    assert m.check("chrome", CHROME)[1]


def test_gone_reads_the_kernel_state() -> None:
    import os
    import subprocess

    from a11y_computer_use.drivers import _linux_exe as m

    assert m._gone(os.getpid()) is False
    child = subprocess.Popen(["true"])
    for _ in range(200):
        if m._gone(child.pid):
            break
        import time
        time.sleep(0.01)
    assert m._gone(child.pid) is True      # exited, not yet reaped: a zombie
    assert m.exe_of(child.pid) is None
    child.wait()
    assert m._gone(child.pid) is True      # reaped: /proc entry gone
