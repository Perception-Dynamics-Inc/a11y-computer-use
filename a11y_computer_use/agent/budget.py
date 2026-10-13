"""Step and time caps for one task class.

The benchmark's uniform budget is 40 steps and 160 seconds. A heavier class
raises both so a finished task is not left at ``max_time``. ``desktop_multi_file``
is 300 seconds, and its step cap is ``40 * 300 / 160`` (75). Every other class
keeps the 40-step, 160-second baseline. The subgoal stuck budget and the hard
``max_time`` deadline still apply inside these caps.

``classify_goal`` reads the goal only to choose a budget. It does not choose
a click target, and it does not change an action.
"""

from __future__ import annotations

from dataclasses import dataclass

# Today's uniform budget. Heavier classes scale from this pair.
_BASELINE_STEPS = 40
_BASELINE_TIME_S = 160.0
# The plan's desktop multi-file example. Steps scale with that time cap.
_MULTI_FILE_TIME_S = 300.0
_MULTI_FILE_STEPS = _BASELINE_STEPS * int(_MULTI_FILE_TIME_S) // int(_BASELINE_TIME_S)

# SDK defaults when the caller names no class. These are not a task class.
DEFAULT_MAX_STEPS = 50
DEFAULT_MAX_TIME_S = 900.0

_ALIASES = {
    "desktop_multifile": "desktop_multi_file",
    "multi_file": "desktop_multi_file",
    "multifile": "desktop_multi_file",
    "file_dialog": "file_chooser",
}


@dataclass(frozen=True)
class TaskBudget:
    """The step cap and the wall-clock cap for one class."""

    task_class: str
    max_steps: int
    max_time_s: float


def _budget(name: str, steps: int, time_s: float) -> TaskBudget:
    return TaskBudget(name, steps, float(time_s))


# Stable order is the order callers see in errors and ``--task-class``.
_ORDER: tuple[str, ...] = (
    "browser",
    "browser_table",
    "browser_scroll",
    "file_chooser",
    "desktop",
    "desktop_multi_file",
    "canvas",
)

TASK_CLASS_CAPS: dict[str, TaskBudget] = {
    "browser": _budget("browser", _BASELINE_STEPS, _BASELINE_TIME_S),
    "browser_table": _budget("browser_table", _BASELINE_STEPS, _BASELINE_TIME_S),
    "browser_scroll": _budget("browser_scroll", _BASELINE_STEPS, _BASELINE_TIME_S),
    "file_chooser": _budget("file_chooser", _BASELINE_STEPS, _BASELINE_TIME_S),
    "desktop": _budget("desktop", _BASELINE_STEPS, _BASELINE_TIME_S),
    "desktop_multi_file": _budget("desktop_multi_file", _MULTI_FILE_STEPS, _MULTI_FILE_TIME_S),
    "canvas": _budget("canvas", _BASELINE_STEPS, _BASELINE_TIME_S),
}


def known_task_classes() -> tuple[str, ...]:
    """Class names ``--task-class`` and ``limits.task_class`` accept."""
    return _ORDER


def normalize_task_class(name: str) -> str:
    """Fold case, hyphens, and the file-dialog alias. ``auto`` stays ``auto``."""
    token = " ".join(name.strip().casefold().replace("-", " ").replace("_", " ").split())
    token = token.replace(" ", "_")
    if token == "auto":
        return "auto"
    return _ALIASES.get(token, token)


def caps_for(task_class: str) -> TaskBudget:
    """The budget for ``task_class``. ``auto`` is not a class."""
    key = normalize_task_class(task_class)
    found = TASK_CLASS_CAPS.get(key)
    if found is None:
        known = ", ".join(_ORDER)
        raise ValueError(f"task_class must be one of: {known}")
    return found


def _has(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def _looks_browser(text: str) -> bool:
    return _has(text, (
        "chrome", "firefox", "browser", "webpage", "web page", "https://", "http://",
        " in a tab", " the tab",
    ))


def _is_desktop_multi_file(text: str) -> bool:
    """Several files on the desktop. A single save is not this class."""
    explicit = (
        "multiple files", "multi-file", "multi file", "several files",
        "each file", "all the files", "every file", "these files", "those files",
    )
    if _has(text, explicit):
        return True
    if "files" not in text:
        return False
    desktop = ("thunar", "nautilus", "pcmanfm", "file manager", "files app", "finder")
    if _has(text, desktop):
        return True
    if _looks_browser(text):
        return False
    return _has(text, ("copy", "move", "rename")) and _has(text, ("folder", "directory"))


def _is_file_chooser(text: str) -> bool:
    return _has(text, (
        "file chooser", "file dialog", "file picker", "choose a file",
        "choose the file", "choose_file", "upload dialog", "open dialog",
    ))


def _is_canvas(text: str) -> bool:
    return _has(text, ("canvas", "drawn on"))


def _is_browser_table(text: str) -> bool:
    return _looks_browser(text) and _has(text, ("table", "infobox", "spreadsheet"))


def _is_browser_scroll(text: str) -> bool:
    return _looks_browser(text) and _has(text, (
        "scroll", "paginate", "pagination", "next page",
    ))


def classify_goal(goal: str) -> str:
    """Pick a class from the goal's wording.

    The result is a budget name. It is not an element, a ref, or a click.
    An unmatched goal uses the desktop baseline.
    """
    text = goal.casefold()
    if _is_desktop_multi_file(text):
        return "desktop_multi_file"
    if _is_file_chooser(text):
        return "file_chooser"
    if _is_canvas(text):
        return "canvas"
    if _is_browser_table(text):
        return "browser_table"
    if _is_browser_scroll(text):
        return "browser_scroll"
    if _looks_browser(text):
        return "browser"
    return "desktop"
