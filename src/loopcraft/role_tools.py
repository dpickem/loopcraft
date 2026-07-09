"""Vendor-neutral role tool vocabulary and access classification (M3.5).

A role's agent definition declares the tools it may use. The control plane
classifies each declared tool as read-only or writing so it can (a) reject a
read-only reviewer that asks for a mutating tool, and (b) fail preflight for a
tool it cannot map to a runtime permission. This is what makes ``tools`` a
capability contract rather than prompt decoration.
"""

from __future__ import annotations

from enum import StrEnum


class ToolAccess(StrEnum):
    """Whether a declared tool can mutate state."""

    READ = "read"
    WRITE = "write"


#: Known vendor-neutral tool names -> access class. Connectors under the
#: ``nv-tools.`` namespace are treated as writing (they can mutate remote state).
ROLE_TOOL_ACCESS: dict[str, ToolAccess] = {
    "repo-read": ToolAccess.READ,
    "repo-write": ToolAccess.WRITE,
    "read": ToolAccess.READ,
    "search": ToolAccess.READ,
    "grep": ToolAccess.READ,
    "web-read": ToolAccess.READ,
    "write": ToolAccess.WRITE,
    "edit": ToolAccess.WRITE,
    "shell": ToolAccess.WRITE,
    "bash": ToolAccess.WRITE,
}

#: Namespace prefix for connector tools, all treated as writing.
_CONNECTOR_PREFIX = "nv-tools."


def tool_access(tool: str) -> ToolAccess | None:
    """Return a tool's access class, or None when it is unknown/unmappable."""
    if tool in ROLE_TOOL_ACCESS:
        return ROLE_TOOL_ACCESS[tool]
    if tool.startswith(_CONNECTOR_PREFIX):
        return ToolAccess.WRITE
    return None


def role_tool_problems(role_name: str, tools: list[str], *, readonly: bool) -> list[str]:
    """Return problems for a role's declared tools.

    Fails when a tool cannot be mapped to a runtime permission, and when a
    read-only role declares a writing tool (which would contradict its
    contract).
    """
    problems: list[str] = []
    for tool in tools:
        access = tool_access(tool)
        if access is None:
            problems.append(
                f"role '{role_name}': tool '{tool}' cannot be mapped to a runtime "
                "permission (unknown tool)"
            )
        elif readonly and access is ToolAccess.WRITE:
            problems.append(
                f"role '{role_name}': read-only role may not declare writing tool '{tool}'"
            )
    return problems
