"""a11y_computer_use — accessibility-first computer use for AI agents on macOS,
Windows, Linux, and Chromium (CDP).

Pruned a11y-tree snapshots with element refs, a vision/pixel fallback, and a
safety layer, exposed as an MCP server + CLI. See PLAN.md for architecture.
"""

try:
    from importlib.metadata import version as _dist_version

    __version__ = _dist_version("a11y-computer-use")
except Exception:  # noqa: BLE001 - a source tree without installed metadata
    __version__ = "0.0.0+unknown"


def _install_agent_models_finder() -> None:
    """Let ``a11y_computer_use.agent.models`` import beside the legacy agent module.

    ``a11y_computer_use/agent.py`` is still the ``a11y_computer_use.agent`` module
    (the reference loop). The M1 model package lives in ``agent/models/``. Python
    will not load a submodule of a module that has no ``__path__``. Until the
    core PR adds ``agent/__init__.py`` (and this finder steps aside), load
    ``agent.py`` with that directory as its package search path.
    """
    import importlib.util
    import sys
    from pathlib import Path

    class _AgentModuleFinder:
        def find_spec(self, fullname, path, target=None):  # noqa: ANN001
            if fullname != "a11y_computer_use.agent":
                return None
            base = Path(__file__).resolve().parent
            if (base / "agent" / "__init__.py").is_file():
                return None
            py = base / "agent.py"
            pkg = base / "agent"
            if not py.is_file() or not (pkg / "models" / "__init__.py").is_file():
                return None
            return importlib.util.spec_from_file_location(
                fullname,
                py,
                submodule_search_locations=[str(pkg)],
            )

    if not any(type(finder).__name__ == "_AgentModuleFinder" for finder in sys.meta_path):
        sys.meta_path.insert(0, _AgentModuleFinder())


_install_agent_models_finder()
