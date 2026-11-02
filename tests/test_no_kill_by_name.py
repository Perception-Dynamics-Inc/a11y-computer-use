"""CI guard: nothing in the repo stops processes by name, command line or user.

The live suites run on shared boxes. Stopping by name (pkill, killall,
``pgrep ... | kill``, ``kill $(pidof ...)``, or a /proc comm/cmdline scan that
then kills) takes down other people's processes. Tests stop only the pids
they launched: see ``tests/_owned_processes.py``.
"""
from __future__ import annotations

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
        if _TOOLS.search(code) or _PIPE.search(code) or _PY_TOOL.search(code):
            found.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    if path.suffix == ".py":
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
    }
    for name, text in samples.items():
        path = ROOT / "tests" / name
        assert kill_by_name_sites(path, text), name
    ok = "proc.kill()\nos.killpg(proc.pid, signal.SIGTERM)\npid=$(pgrep -x budgie-wm | head -1)\n"
    assert not kill_by_name_sites(ROOT / "tests" / "ok.sh", ok)
