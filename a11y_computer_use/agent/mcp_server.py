"""Agent-level MCP server: ``a11y-agent mcp``.

Tools are ``run_goal``, ``get_run``, ``cancel_run``, and ``approve``. This is
not the desktop MCP server. That server's tool list is unchanged.
"""

from __future__ import annotations

from a11y_computer_use.agent.service import DisplayBusy, RunStore

_INSTRUCTIONS = """Goal-level computer-use agent. run_goal starts one goal and returns a run id.
get_run returns the same JSON object as a11y-agent run --json once the run has
finished, and status "running" before that. cancel_run asks the run to stop.
approve answers a pending approval; if nobody answers before the timeout, the
action is denied. One run may be active per display.
"""


def build_agent_mcp(store: RunStore | None = None):
    """Construct the agent MCP server. The caller owns ``store``."""
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError

    from a11y_computer_use import __version__

    shared = store if store is not None else RunStore()
    server = FastMCP("a11y-agent", instructions=_INSTRUCTIONS)
    server._mcp_server.version = __version__

    @server.tool()
    def run_goal(
        goal: str,
        model: str,
        display: str | None = None,
        max_steps: int = 50,
        max_time_s: float = 900,
        allowed_domains: list[str] | None = None,
        blocked_domains: list[str] | None = None,
        allow_exec: bool = False,
        allow_payments: bool = False,
    ) -> dict:
        """Start one goal. Returns ``{"id": ...}``. 409 when that display is busy.

        ``allow_payments`` lets a payment click wait for ``approve`` instead of
        stopping as ``needs_human``. It is off by default.
        """
        body = {
            "goal": goal,
            "model": model,
            "display": display,
            "limits": {"max_steps": max_steps, "max_time_s": max_time_s},
            "allow_exec": allow_exec,
            "allow_payments": allow_payments,
        }
        if allowed_domains is not None:
            body["allowed_domains"] = allowed_domains
        if blocked_domains is not None:
            body["blocked_domains"] = blocked_domains
        try:
            record = shared.start(body)
        except DisplayBusy as exc:
            raise ToolError(
                f"409 a run is already active on this display ({exc.display}); run_id={exc.run_id}"
            ) from exc
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return {"id": record.id}

    @server.tool()
    def get_run(run_id: str) -> dict:
        """Return the run's status. Finished runs match ``a11y-agent run --json``."""
        record = shared.get(run_id)
        if record is None:
            raise ToolError(f"404 run not found: {run_id}")
        return shared.view(record)

    @server.tool()
    def cancel_run(run_id: str) -> dict:
        """Ask a run to stop. The run ends with reason cancelled."""
        record = shared.cancel(run_id)
        if record is None:
            raise ToolError(f"404 run not found: {run_id}")
        return {"id": record.id, "cancel": True}

    @server.tool()
    def approve(run_id: str, approval_id: str, approve: bool) -> dict:
        """Answer one pending approval. True allows the action; false denies it.

        ``get_run`` lists each waiting approval with the same ``target`` as the
        HTTP ``approval_required`` event: role, fenced name, fenced window,
        fenced page URL when the target is in a browser, and reason
        (payment, send, delete, quit, or exec).
        """
        outcome = shared.resolve_approval(run_id, approval_id, approve)
        if outcome == "missing":
            raise ToolError(f"404 approval not found: {approval_id}")
        if outcome == "answered":
            raise ToolError(f"409 approval already answered: {approval_id}")
        return {"approval_id": approval_id, "approve": approve}

    return server


def main() -> None:
    """Serve the agent MCP tools on stdio."""
    build_agent_mcp().run()


__all__ = ["build_agent_mcp", "main"]
