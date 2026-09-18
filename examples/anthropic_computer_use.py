"""Drop-in executor for an Anthropic computer-use loop.

The model keeps its native ``computer`` toolset; a11y-computer-use executes each
action through the gated Runtime and, when a click lands on an element of the
accessibility tree, snaps it to that element's ref (no cursor movement).

Run with a key:

    ANTHROPIC_API_KEY=... python examples/anthropic_computer_use.py "Open the File menu"

Without a key (or without the ``anthropic`` package installed) the script
replays a scripted action sequence through the adapter instead, so the wiring
and the result shapes are visible with nothing but this repo.

Backend selection is the Runtime's: the OS driver by default, or
``A11Y_COMPUTER_USE_DRIVER=browser A11Y_COMPUTER_USE_CDP_ENDPOINT=http://127.0.0.1:9222``
for a Chromium tab. ``A11Y_COMPUTER_USE_APP`` names the app whose tree backs
snap-to-ref (default: the frontmost app, or the bound tab on the browser).
"""

from __future__ import annotations

import json
import os
import sys

from a11y_computer_use import server
from a11y_computer_use.adapters import AnthropicComputerAdapter

MODEL = "claude-opus-5"  # supports computer_toolset_20260801 without a beta header
MAX_TURNS = 40

_SCRIPTED = [
    ("screenshot", {}),
    ("left_click", {"coordinate": [100, 100]}),
    ("type", {"text": "hello from a11y-computer-use"}),
    ("key", {"text": "Return"}),
    ("scroll", {"coordinate": [400, 300], "scroll_direction": "down", "scroll_amount": 2}),
    ("screenshot", {}),
]


def _describe(result) -> str:
    if result.png is not None and not result.text:
        return f"<png {len(result.png)} bytes>"
    return result.text


def scripted(adapter: AnthropicComputerAdapter) -> None:
    print("no ANTHROPIC_API_KEY / anthropic package: replaying a scripted sequence\n")
    for name, payload in _SCRIPTED:
        result = adapter.handle(payload, name=name)
        # Result.text already starts with the error code on failure; don't prefix it twice.
        print(f"{name:12} {json.dumps(payload):58} -> {'ok: ' if result.ok else ''}{_describe(result)}")


def live(adapter: AnthropicComputerAdapter, task: str) -> None:
    import anthropic

    client = anthropic.Anthropic()
    tools = [adapter.tool_definition()]  # {"type": "computer_toolset_20260801"}
    messages: list[dict] = [{"role": "user", "content": task}]
    for _turn in range(MAX_TURNS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            thinking={"type": "adaptive"},
            tools=tools,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_uses:
            print(next((b.text for b in response.content if b.type == "text"), "(done)"))
            return
        # The docs' batch rule: run the blocks in order, stop at the first failure,
        # answer every block in ONE user message.
        results: list[dict] = []
        failed = False
        for block in tool_uses:
            if failed:
                results.append({
                    "type": "tool_result", "tool_use_id": block.id, "toolset_name": "computer",
                    "is_error": True,
                    "content": "Not executed: an earlier computer action in this turn failed.",
                })
                continue
            tool_result = adapter.handle_tool_use(block)
            failed = bool(tool_result.get("is_error"))
            print(f"{block.name:14} {json.dumps(block.input)[:60]:60} -> "
                  f"{'error' if failed else 'ok'}")
            results.append(tool_result)
        messages.append({"role": "user", "content": results})
    print("stopped after the turn limit")


def main() -> int:
    runtime = server.Runtime()
    adapter = AnthropicComputerAdapter(runtime, app=os.environ.get("A11Y_COMPUTER_USE_APP"))
    task = " ".join(sys.argv[1:]) or "Take a screenshot and describe what you see."
    try:
        import anthropic  # noqa: F401
    except ImportError:
        scripted(adapter)
        return 0
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        scripted(adapter)
        return 0
    live(adapter, task)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
