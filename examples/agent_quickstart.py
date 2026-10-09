"""Run one goal with the M1 computer-use agent and a scripted model.

This is not an LLM. ``ScriptedModel`` is a Python callable: each turn it reads
the latest accessibility snapshot out of the messages, picks element refs from
that text, and returns the next tool call. No API key is read and none is
required.

The GTK fixture (``tests/agent_fixtures/gtk_app.py``) is a note window with a
multi-line text view, an in-app save path, a format combo, and a confirmation
label. Save As does not open a system file chooser: Linux ``file_dialog`` is
unsupported, so the path is a field in the window.

    # From the repo root, on a Linux display (Xvfb is enough):
    GTK_MODULES=gail:atk-bridge NO_AT_BRIDGE=0 python examples/agent_quickstart.py

The process prints the ``RunResult`` status and the confirmation the fixture
showed. Exit 0 when the run reports success.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GTK_APP = REPO / "tests" / "agent_fixtures" / "gtk_app.py"
NOTE = "M1 live note\nsecond line\n"
_LINE = re.compile(
    r"""(?m)^[ \t]*(e\d+)\s+(\S+)\s+"([^"]*)"(?:\s+="([^"]*)")?(?:\s+\(([^)]*)\))?"""
)


def _text(messages) -> str:
    parts = []
    for message in messages or []:
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and block.get("text"):
                    parts.append(str(block["text"]))
    return "\n".join(parts)


def _pick(text: str, role: str, name: str):
    found = None
    for match in _LINE.finditer(text):
        ref, seen_role, seen_name, value, _flags = match.groups()
        if seen_role == role and seen_name == name:
            found = (ref, value)
    return found


def main() -> int:
    try:
        from a11y_computer_use.agent.core import Agent
        from a11y_computer_use.agent.models.base import ModelTurn, ToolCall
        from a11y_computer_use.agent.models.scripted import ScriptedModel
    except ImportError as exc:
        print(
            textwrap.fill(
                "This example needs a11y_computer_use.agent (Agent, the scripted "
                f"model, and the a11y-agent CLI). Those modules are not importable yet: {exc}"
            ),
            file=sys.stderr,
        )
        return 3

    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        print("Set DISPLAY to a Linux session (xvfb-run works) and launch again.", file=sys.stderr)
        return 3

    dest = Path(tempfile.gettempdir()) / "cuagent-quickstart.txt"
    dest.unlink(missing_ok=True)
    env = os.environ.copy()
    env["GTK_MODULES"] = "gail:atk-bridge"
    env["NO_AT_BRIDGE"] = "0"
    app = subprocess.Popen([sys.executable, str(GTK_APP)], env=env)

    def script(messages):
        """Scripted planner. Not an LLM. Refs come from the snapshot text."""
        text = _text(messages)
        notes = _pick(text, "textarea", "Notes")
        path = _pick(text, "textfield", "Save path")
        combo = _pick(text, "combobox", "Format")
        status = _pick(text, "statictext", "Status")
        save = _pick(text, "button", "Save")
        shown = (NOTE.replace("\n", " "),)
        if notes is None or path is None or combo is None or save is None:
            return ModelTurn(calls=[ToolCall(name="app", args={"action": "focus", "name": "cuagentfix"})])
        if (notes[1] or "") not in {shown[0], NOTE}:
            return ModelTurn(calls=[ToolCall(name="set_value", args={"ref": notes[0], "value": NOTE})])
        if (path[1] or "") != str(dest):
            return ModelTurn(calls=[ToolCall(name="set_value", args={"ref": path[0], "value": str(dest)})])
        if combo[1] != "plain":
            return ModelTurn(calls=[ToolCall(name="select", args={"ref": combo[0], "value": "plain"})])
        if status is None or status[1] != "saved plain":
            return ModelTurn(calls=[ToolCall(name="click", args={"ref": save[0]})])
        return ModelTurn(calls=[ToolCall(
            name="done",
            args={
                "answer": "saved the note",
                "conditions": [
                    {"file_exists": str(dest), "contains": NOTE},
                    {"value": {"name": "Status", "equals": "saved plain"}},
                    {"window_title_contains": "saved plain"},
                ],
            },
        )])

    trace = Path(tempfile.mkdtemp(prefix="cuagent-trace-"))
    try:
        try:
            model = ScriptedModel(script)
        except TypeError:
            model = ScriptedModel(script=script)
        agent = Agent(
            model,
            display=os.environ.get("DISPLAY"),
            max_steps=12,
            max_time_s=120,
            approve=lambda _action: True,
            trace_dir=str(trace),
        )
        result = agent.run(
            "In cuagentfix, write the note, choose plain, save it to "
            f"{dest}, and stop when the status says saved plain."
        )
    finally:
        app.terminate()
        try:
            app.wait(timeout=3)
        except subprocess.TimeoutExpired:
            app.kill()

    print(f"status={result.status} steps={result.steps} answer={result.answer}")
    print(f"trace={result.trace_dir}")
    if dest.is_file():
        print(f"file={dest.read_text()!r}")
    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
