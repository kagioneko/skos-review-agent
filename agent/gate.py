"""The agent's own capability gate - the same idea SKOS checks for, applied
to this agent while it runs.

Designed small on purpose (lesson from gating a large runtime after the
fact): the agent has four tools, each with a capability label reviewed here,
and anything not in this table is refused.

Session taint lives in ADK session state, so it survives the confirmation
round-trip and is per session:
- reading outside content (a reference page) sets `untrusted_read`;
- reading the user's config sets `sensitive_read`;
- a tool that sends data out is then held for human approval:
    CAPGRAPH-001  outside content + private data read, then a send
    CAPGRAPH-004  private data read, then a send
Taint is only ever set, never cleared within a session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Caps:
    reads_untrusted: bool = False  # content outsiders can write
    reads_sensitive: bool = False  # the user's private data
    egress: bool = False           # sends data out
    exec: bool = False             # runs code
    persistence: bool = False      # writes something loaded later


TOOL_CAPS: dict[str, Caps] = {
    "scan_mcp_config": Caps(reads_sensitive=True),
    "reassess": Caps(),
    # only bundled pages, by id - no URL, so reading cannot carry data out
    "read_reference": Caps(reads_untrusted=True),
    "send_report": Caps(egress=True),
}

UNTRUSTED = "gate:untrusted_read"
SENSITIVE = "gate:sensitive_read"
HOLDS = "gate:holds"


def _state(ctx: Any) -> Any:
    return ctx.state


def hold_reason(state: Any, caps: Caps) -> str | None:
    """Why a call with ``caps`` must wait for a human, or None."""
    untrusted = bool(state.get(UNTRUSTED))
    sensitive = bool(state.get(SENSITIVE))
    if caps.egress and untrusted and sensitive:
        return ("CAPGRAPH-001: outside content and private data have been read in this "
                "session; sending needs your approval")
    if caps.exec and untrusted:
        return "CAPGRAPH-002: outside content has been read; running code needs your approval"
    if caps.persistence and untrusted:
        return "CAPGRAPH-003: outside content has been read; a persistent write needs your approval"
    if caps.egress and sensitive:
        return "CAPGRAPH-004: private data has been read; sending needs your approval"
    return None


def before_tool(tool: Any, args: dict[str, Any], tool_context: Any) -> dict[str, Any] | None:
    """ADK before_tool_callback: refuse unknown tools, record what the call
    reads (before it runs, so a failed call still counts as read)."""
    caps = TOOL_CAPS.get(tool.name)
    if caps is None:
        return {"error": f"refused by the gate: '{tool.name}' has no capability label"}
    state = _state(tool_context)
    if caps.reads_untrusted:
        state[UNTRUSTED] = True
    if caps.reads_sensitive:
        state[SENSITIVE] = True
    return None


def require_approval(tool_name: str):
    """A FunctionTool ``require_confirmation`` callable for ``tool_name``:
    True when the session's taint means this call must wait for a human.
    The reason is recorded in state for the UI."""
    caps = TOOL_CAPS[tool_name]

    def check(**kwargs: Any) -> bool:
        ctx = kwargs.get("tool_context")
        if ctx is None:
            return True  # no session to judge by: ask
        reason = hold_reason(_state(ctx), caps)
        if reason is None:
            return False
        holds = list(_state(ctx).get(HOLDS) or [])
        holds.append({"tool": tool_name, "reason": reason})
        _state(ctx)[HOLDS] = holds[-20:]
        return True

    return check
