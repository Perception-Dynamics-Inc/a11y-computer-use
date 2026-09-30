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
