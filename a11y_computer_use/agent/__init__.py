"""Computer-use agent package.

``run_task`` is the reference planner loop (cu-arena and ``a11y-computer-use
agent``). ``Agent`` is the goal runner behind ``a11y-agent``.
"""

from a11y_computer_use.agent.actions import Action, ReservedPermission, tool_schemas
from a11y_computer_use.agent.core import Agent
from a11y_computer_use.agent.events import Event
from a11y_computer_use.agent.reference import (
    DONE_TOOL,
    OBSERVATION_TOOLS,
    AgentResult,
    Step,
    _bound_history,  # h2h.run_pixel_task calls agent._bound_history
    compact_history,
    notes_block,
    run_task,
    system_prompt,
    tool_specs,
)
from a11y_computer_use.agent.result import RunResult, StepRecord

__all__ = [
    "DONE_TOOL",
    "OBSERVATION_TOOLS",
    "Action",
    "Agent",
    "AgentResult",
    "Event",
    "ReservedPermission",
    "RunResult",
    "Step",
    "StepRecord",
    "compact_history",
    "notes_block",
    "run_task",
    "system_prompt",
    "tool_schemas",
    "tool_specs",
]
