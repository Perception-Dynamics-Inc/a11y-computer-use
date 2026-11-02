"""CI guard: nothing in the repo stops processes by name, command line or user.

The live suites run on shared boxes. Stopping by name (pkill, killall,
``pgrep ... | kill``, ``kill $(pidof ...)``, or a /proc comm/cmdline scan that
then kills) takes down other people's processes. Tests stop only the pids
they launched: see ``tests/_owned_processes.py``.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN = ("tests", "scripts", "a11y_computer_use", "examples", ".github")
SUFFIXES = {".py", ".sh", ".bash", ".yml", ".yaml", ".ps1", ".toml", ".cfg", ""}
THIS = Path(__file__).resolve()

_TOOLS = re.compile(r"(?:^|[;&|(`$]|\s)(pkill|killall)\s+[-\w$\"']")
_PIPE = re.compile(r"\b(pgrep|pidof)\b[^\n]*\|\s*(xargs\s+)?kill\b|\bkill\b[^\n]*\$\(\s*(pgrep|pidof)\b")
_PY_TOOL = re.compile(r"""\[\s*["'](pkill|killall)["']""")
_COMM_READ = re.compile(r"""/proc/[^\n]*/(comm|cmdline)|["'](comm|cmdline)["']""")
_KILL = re.compile(r"\bos\.kill(pg)?\(|\.send_signal\(")
_WINDOW_PID = re.compile(r"getwindowpid|_NET_WM_PID|wmctrl\s+-lp")
_OWNED = "# owned pid:"


def _unrecorded_kills(text: str) -> list[int]:
    """Lines of os.kill/os.killpg whose pid is not a recorded launch's ``.pid``.

    A pid found any other way (a name or comm snapshot, a set difference of
    such snapshots, a window's title or _NET_WM_PID) is not ours. A probe with
    signal 0 sends nothing. A helper that stops members of a recorded session
    or tree marks the call ``# owned pid: <why>`` for review.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    lines = text.splitlines()
    bad = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"kill", "killpg"}
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "os"):
            continue
        args = node.args
        if len(args) > 1 and isinstance(args[1], ast.Constant) and args[1].value == 0:
            continue
        if args and isinstance(args[0], ast.Attribute) and args[0].attr == "pid":
            continue
        if _OWNED in lines[node.lineno - 1]:
            continue
        bad.append(node.lineno)
    return bad


def _files():
    for top in SCAN:
        base = ROOT / top
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if (path.is_file() and path.suffix in SUFFIXES and path.resolve() != THIS
                    and "__pycache__" not in path.parts):
                yield path


def _functions(text: str):
    """Top-level and nested Python def blocks, by indentation."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"(\s*)(async\s+)?def\s+\w+", line)
        if not m:
            continue
        indent = len(m.group(1))
        body = [line]
        for nxt in lines[i + 1:]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                break
            body.append(nxt)
        yield i + 1, "\n".join(body)


def kill_by_name_sites(path: Path, text: str) -> list[str]:
    found = []
    for n, line in enumerate(text.splitlines(), 1):
        code = line.split("#", 1)[0] if path.suffix in {".py", ".sh", ".bash", ".yml", ".yaml", ""} else line
        window_kill = _WINDOW_PID.search(code) and re.search(r"\bkill\b", code)
        if _TOOLS.search(code) or _PIPE.search(code) or _PY_TOOL.search(code) or window_kill:
            found.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    if path.suffix == ".py":
        for n in _unrecorded_kills(text):
            found.append(f"{path.relative_to(ROOT)}:{n}: kills a pid that is not a recorded launch")
        for n, body in _functions(text):
            if _COMM_READ.search(body) and _KILL.search(body) and "session_members" not in body:
                found.append(f"{path.relative_to(ROOT)}:{n}: reads process names and kills")
    return found


def test_no_process_is_stopped_by_name() -> None:
    sites = []
    for path in _files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        sites += kill_by_name_sites(path, text)
    assert not sites, "kill by name is forbidden; stop recorded pids:\n" + "\n".join(sites)


def test_guard_catches_the_forbidden_forms(tmp_path) -> None:
    samples = {
        "a.sh": "pkill -f soffice\n",
        "b.sh": "killall evince\n",
        "c.sh": "pgrep -x thunar | xargs kill\n",
        "d.sh": "kill $(pidof thunar)\n",
        "e.py": 'subprocess.run(["pkill", "-x", "evince"])\n',
        "f.py": ("def stop():\n    for e in os.listdir('/proc'):\n"
                 "        if open(f'/proc/{e}/comm').read() == 'x':\n"
                 "            os.kill(int(e), 9)\n"),
        # name-snapshot diff: new same-named pids are not ours
        "g.py": ("before = _display_pids('mousepad')\nlaunch()\n"
                 "for pid in _display_pids('mousepad') - before:\n    os.kill(pid, 15)\n"),
        # a pid read from a window's title row
        "h.py": ("for row in driver.windows():\n    if row['title'] == 'x':\n"
                 "        os.kill(int(row['pid']), 15)\n"),
        "i.py": "child = find_window_pid('cualaunchwin')\nos.killpg(child, signal.SIGTERM)\n",
        "j.sh": "kill $(xdotool search --name foo getwindowpid)\n",
    }
    for name, text in samples.items():
        path = ROOT / "tests" / name
        assert kill_by_name_sites(path, text), name
    ok = "proc.kill()\nos.killpg(proc.pid, signal.SIGTERM)\npid=$(pgrep -x budgie-wm | head -1)\n"
    assert not kill_by_name_sites(ROOT / "tests" / "ok.sh", ok)
    ok_py = ("proc = subprocess.Popen(argv)\nos.killpg(proc.pid, signal.SIGTERM)\n"
             "os.kill(pid, 0)\nos.kill(member, 9)  # owned pid: our session's member\n")
    assert not kill_by_name_sites(ROOT / "tests" / "ok.py", ok_py)
