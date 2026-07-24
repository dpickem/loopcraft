"""Vendor-neutral role tool vocabulary and read/write policy validation (M3.5).

A role's agent definition declares the tools it may use. The control plane
**validates** those declarations — it rejects a tool it does not recognize and a
read-only role that declares a clearly *mutating local* tool. This is policy
validation, **not** runtime enforcement: adapters do not yet translate the
declared list into native per-tool allowlists, so a role is not technically
prevented from reaching another available tool.

Tools are classified as:

- ``READ`` — inspect only (e.g. ``repo-read``);
- ``EXECUTE`` — run something without mutating the repo/maker outputs (e.g.
  ``agent-spawn`` to launch sub-reviewers, ``test-run`` to run the test suite) —
  allowed for a read-only reviewer;
- ``WRITE`` — mutate local files (e.g. ``repo-write``, ``shell``) — forbidden
  for a read-only role.

Connector operations (``nv-tools.*``) are read *or* write depending on the
operation, so a connector name alone cannot be classified; they are recognized
and allowed here, and left to operation-level gating (and the tier/approval
model) rather than being rejected for a read-only role.
"""

from __future__ import annotations

from enum import StrEnum


class ToolAccess(StrEnum):
    """How a declared local tool affects state."""

    READ = "read"
    EXECUTE = "execute"
    WRITE = "write"


#: Known vendor-neutral *local* tool names -> access class. Connectors
#: (``nv-tools.*``) are intentionally absent: their read/write nature is
#: operation-dependent, so they are not auto-classified.
ROLE_TOOL_ACCESS: dict[str, ToolAccess] = {
    "repo-read": ToolAccess.READ,
    "read": ToolAccess.READ,
    "search": ToolAccess.READ,
    "grep": ToolAccess.READ,
    "web-read": ToolAccess.READ,
    "agent-spawn": ToolAccess.EXECUTE,
    "test-run": ToolAccess.EXECUTE,
    "repo-write": ToolAccess.WRITE,
    "write": ToolAccess.WRITE,
    "edit": ToolAccess.WRITE,
    "shell": ToolAccess.WRITE,
    "bash": ToolAccess.WRITE,
}

#: Namespace prefix for connector tools (operation-level read/write; allowed).
_CONNECTOR_PREFIX = "nv-tools."


def tool_access(tool: str) -> ToolAccess | None:
    """Return a tool's access class, or None when it is a connector/unclassified."""
    return ROLE_TOOL_ACCESS.get(tool)


def is_known_tool(tool: str) -> bool:
    """Return whether a declared tool is part of the known vocabulary."""
    return tool in ROLE_TOOL_ACCESS or tool.startswith(_CONNECTOR_PREFIX)


def role_tool_problems(role_name: str, tools: list[str], *, readonly: bool) -> list[str]:
    """Return policy-validation problems for a role's declared tools.

    Fails when a tool is unknown (cannot be reasoned about at all), and when a
    read-only role declares a clearly *mutating local* tool (``WRITE``). Read and
    execute tools (and connectors) are allowed for a read-only role — a reviewer
    may still spawn sub-reviewers and run the test suite.
    """
    problems: list[str] = []
    for tool in tools:
        if not is_known_tool(tool):
            problems.append(f"role '{role_name}': unknown tool '{tool}' (not in the role tool vocabulary)")
            continue
        if readonly and tool_access(tool) is ToolAccess.WRITE:
            problems.append(
                f"role '{role_name}': read-only role may not declare mutating tool '{tool}'"
            )
    return problems
