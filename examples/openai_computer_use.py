"""Drop-in executor for an OpenAI computer-use loop (Responses API).

The model keeps its native ``computer`` tool; computerUse executes each
``computer_call`` through the gated Runtime, snapping clicks to accessibility
refs when one is under the point, and answers with a ``computer_call_output``.

Run with a key:

    OPENAI_API_KEY=... python examples/openai_computer_use.py "Open the File menu"

Without a key (or without the ``openai`` package installed) the script replays
a scripted ``computer_call`` through the adapter instead.

``OPENAI_COMPUTER_MODEL`` picks the model (default from the docs at the time of
writing: ``gpt-5.4``). ``OPENAI_COMPUTER_PREVIEW=1`` switches to the deprecated
``computer_use_preview`` tool shape with the ``computer-use-preview`` model.
Backend selection is the Runtime's (``COMPUTERUSE_DRIVER``, ``COMPUTERUSE_APP``).
"""

from __future__ import annotations

import json
import os
import sys

from computeruse import server
from computeruse.adapters import OpenAIComputerAdapter

MAX_TURNS = 40

_SCRIPTED_CALL = {
    "type": "computer_call",
    "call_id": "call_scripted",
    "status": "completed",
    "actions": [
        {"type": "screenshot"},
        {"type": "click", "x": 100, "y": 100, "button": "left"},
        {"type": "type", "text": "hello from computerUse"},
        {"type": "keypress", "keys": ["ENTER"]},
        {"type": "scroll", "x": 400, "y": 300, "scroll_x": 0, "scroll_y": 200},
    ],
}


def scripted(adapter: OpenAIComputerAdapter) -> None:
    print("no OPENAI_API_KEY / openai package: replaying a scripted computer_call\n")
    output, results = adapter.handle_call(_SCRIPTED_CALL)
    for result in results:
        # Result.text already starts with the error code on failure; don't prefix it twice.
        text = result.text or (f"<png {len(result.png)} bytes>" if result.png else "")
        print(f"{result.action:12} -> {'ok: ' + text if result.ok else text}")
    print("\ncomputer_call_output:", json.dumps({
        **output, "output": {**output["output"], "image_url": "data:image/png;base64,..."},
    }, indent=2))


def live(adapter: OpenAIComputerAdapter, task: str) -> None:
    from openai import OpenAI

    client = OpenAI()
    preview = os.environ.get("OPENAI_COMPUTER_PREVIEW") == "1"
    model = os.environ.get("OPENAI_COMPUTER_MODEL", "computer-use-preview" if preview else "gpt-5.4")
    tools = [adapter.tool_definition(preview=preview)]
    response = client.responses.create(
        model=model, tools=tools, truncation="auto",
        input=[{"role": "user", "content": task}],
    )
    for _turn in range(MAX_TURNS):
        calls = [item for item in response.output if item.type == "computer_call"]
        if not calls:
            print(getattr(response, "output_text", "") or "(done)")
            return
        outputs = []
        for call in calls:
            acknowledge = False
            pending = getattr(call, "pending_safety_checks", None) or []
            if pending:
                # The model flagged this call (malicious_instructions, irrelevant_domain,
                # sensitive_domain). The adapter runs nothing until the host acknowledges;
                # here a human decides, and declining ends the loop.
                for check in pending:
                    print(f"safety check {getattr(check, 'code', '?')}: {getattr(check, 'message', '')}")
                if input("run this flagged call? [y/N] ").strip().lower() != "y":
                    print("stopped: pending safety checks were not acknowledged")
                    return
                acknowledge = True
            output_item, results = adapter.handle_call(
                call.model_dump(), acknowledge_safety_checks=acknowledge)
            for result in results:
                print(f"{result.action:12} -> {'ok: ' + result.text if result.ok else result.text}")
            outputs.append(output_item)
        response = client.responses.create(
            model=model, tools=tools, truncation="auto",
            previous_response_id=response.id, input=outputs,
        )
    print("stopped after the turn limit")


def main() -> int:
    runtime = server.Runtime()
    adapter = OpenAIComputerAdapter(runtime, app=os.environ.get("COMPUTERUSE_APP"))
    task = " ".join(sys.argv[1:]) or "Take a screenshot and describe what you see."
    try:
        import openai  # noqa: F401
    except ImportError:
        scripted(adapter)
        return 0
    if not os.environ.get("OPENAI_API_KEY"):
        scripted(adapter)
        return 0
    live(adapter, task)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
