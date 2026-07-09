"""Compile vendor-neutral agent definitions into runtime-native sub-agent files.

Each runtime discovers sub-agents from its own on-disk format and directory:

- Codex   -> ``.codex/agents/<name>.toml``
- Claude  -> ``.claude/agents/<name>.md`` (YAML frontmatter + system prompt)
- Cursor  -> ``.cursor/agents/<name>.yaml``

:func:`compile_agent` renders one :class:`~loopcraft.agents.AgentDefinition`
into the chosen runtime's format with the role's model attached; the agent
definition stays the single source of truth for behavior, and the vendor/model
is a swappable binding on top of it. :func:`write_compiled_agents` materializes
the rendered files into a run worktree (used by the intra-run execution path, so
one harness can spawn the roles as sub-agents).
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import BaseModel

from loopcraft.agents import AgentDefinition
from loopcraft.manifest import Vendor
from loopcraft.paths import assert_under

#: Runtime -> (sub-agent directory, filename extension) for compiled agents.
_VENDOR_LAYOUT: dict[str, tuple[str, str]] = {
    Vendor.CODEX: (".codex/agents", "toml"),
    Vendor.CLAUDE: (".claude/agents", "md"),
    Vendor.CURSOR: (".cursor/agents", "yaml"),
}


class AgentCompileError(Exception):
    """Raised when an agent definition cannot be compiled for a vendor."""


class CompiledAgent(BaseModel):
    """One agent definition rendered into a runtime-native sub-agent file.

    Attributes:
        vendor: Target runtime the file is formatted for.
        relpath: Worktree-relative destination path (e.g.
            ``.claude/agents/reviewer.md``).
        content: The rendered file contents.
    """

    vendor: str
    relpath: str
    content: str


def _readonly_preamble(defn: AgentDefinition) -> str:
    """Return a leading instruction that restates a role's read-only contract.

    The ``readonly`` flag travels with the *role*, not the model, so the
    reviewer's constraint survives an engine swap. Runtimes vary in how strictly
    they enforce a read-only sub-agent, so the contract is also stated in the
    prompt as defense in depth.
    """
    if not defn.readonly:
        return ""
    return (
        "IMPORTANT: You are a READ-ONLY review role. Do not modify source code or "
        "another role's outputs, and do not run mutating commands. You may write "
        "only your own declared review output(s). Report findings only.\n\n"
    )


def _compile_codex(defn: AgentDefinition, model: str | None) -> str:
    """Render an agent definition as a Codex ``.codex/agents/*.toml`` file."""
    instructions = _readonly_preamble(defn) + defn.instructions
    lines = [
        f"name = {json.dumps(defn.name)}",
        f"description = {json.dumps(defn.description)}",
        f"read_only = {str(defn.readonly).lower()}",
    ]
    if model:
        lines.append(f"model = {json.dumps(model)}")
    if defn.tools:
        rendered = ", ".join(json.dumps(tool) for tool in defn.tools)
        lines.append(f"tools = [{rendered}]")
    # A TOML basic string (json.dumps) escapes quotes/newlines safely, so
    # arbitrary instruction text round-trips without a fragile multi-line block.
    lines.append(f"instructions = {json.dumps(instructions)}")
    return "\n".join(lines) + "\n"


def _compile_claude(defn: AgentDefinition, model: str | None) -> str:
    """Render an agent definition as a Claude ``.claude/agents/*.md`` file."""
    header: dict[str, object] = {"name": defn.name, "description": defn.description}
    if defn.tools:
        header["tools"] = ", ".join(defn.tools)
    if model:
        header["model"] = model
    frontmatter = yaml.safe_dump(header, sort_keys=False).strip()
    body = _readonly_preamble(defn) + defn.instructions
    return f"---\n{frontmatter}\n---\n{body}\n"


def _compile_cursor(defn: AgentDefinition, model: str | None) -> str:
    """Render an agent definition as a Cursor ``.cursor/agents/*.yaml`` file."""
    doc: dict[str, object] = {
        "name": defn.name,
        "description": defn.description,
        "readonly": defn.readonly,
    }
    if model:
        doc["model"] = model
    if defn.tools:
        doc["tools"] = list(defn.tools)
    doc["prompt"] = _readonly_preamble(defn) + defn.instructions
    return yaml.safe_dump(doc, sort_keys=False)


def compile_agent(defn: AgentDefinition, vendor: str, model: str | None) -> CompiledAgent:
    """Compile one agent definition into a runtime-native sub-agent file.

    Args:
        defn: The parsed, vendor-neutral agent definition.
        vendor: Target runtime (``codex`` / ``claude`` / ``cursor``).
        model: Model id to attach to the compiled agent (role binding), or None.

    Returns:
        The rendered :class:`CompiledAgent` (vendor, worktree-relative path,
        contents).

    Raises:
        AgentCompileError: If the vendor has no known sub-agent layout.
    """
    layout = _VENDOR_LAYOUT.get(vendor)
    if layout is None:
        raise AgentCompileError(
            f"no sub-agent format for vendor '{vendor}' (known: {sorted(_VENDOR_LAYOUT)})"
        )
    directory, ext = layout
    if vendor == Vendor.CODEX:
        content = _compile_codex(defn, model)
    elif vendor == Vendor.CLAUDE:
        content = _compile_claude(defn, model)
    else:
        content = _compile_cursor(defn, model)
    return CompiledAgent(vendor=vendor, relpath=f"{directory}/{defn.name}.{ext}", content=content)


def write_compiled_agents(workdir: Path, compiled: list[CompiledAgent]) -> list[Path]:
    """Write compiled sub-agent files into a run worktree.

    Each destination is confirmed to remain under ``workdir`` before writing, so
    a crafted agent name can never escape the run directory.

    Args:
        workdir: The run worktree root.
        compiled: Rendered agents to materialize.

    Returns:
        The list of written destination paths.

    Raises:
        ValueError: If a compiled file would resolve outside the worktree.
    """
    workdir = workdir.resolve()
    written: list[Path] = []
    for agent in compiled:
        dest = (workdir / agent.relpath).resolve()
        assert_under(workdir, dest, label="compiled agent")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(agent.content, encoding="utf-8")
        written.append(dest)
    return written
