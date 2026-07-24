"""Compile vendor-neutral agent definitions into runtime-native sub-agent files.

Each runtime discovers sub-agents from its own on-disk format and directory. The
formats below track the current runtime documentation (verified against Cursor
`https://cursor.com/docs/subagents.md` and Codex
`https://developers.openai.com/codex/subagents`):

- **Cursor** -> ``.cursor/agents/<name>.md`` — Markdown with YAML frontmatter
  (``name``/``description``/``model``/``readonly``) and a Markdown prompt body.
- **Claude** -> ``.claude/agents/<name>.md`` — Markdown with YAML frontmatter
  (``name``/``description``/``tools``/``model``) and a prompt body.
- **Codex** -> ``.codex/agents/<name>.toml`` — TOML with ``name``/``description``/
  ``developer_instructions`` (plus ``model`` and ``sandbox_mode = "read-only"``
  for a read-only role).

The compiled file's ``name`` and filename come from the manifest **role key**
(canonical, unique, filename-safe), not the agent definition's own ``name``, so
two roles can never collide on one destination. A role's ``verify`` rubric is
compiled into the instructions so the stop/acceptance contract travels with the
behavior. :func:`write_compiled_agents` materializes the files into a worktree.
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
    Vendor.CURSOR: (".cursor/agents", "md"),
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


def _compile_codex(name: str, defn: AgentDefinition, model: str | None) -> str:
    """Render a Codex ``.codex/agents/*.toml`` custom-agent file.

    Uses the current Codex schema: required ``name``/``description``/
    ``developer_instructions``, optional ``model``, and ``sandbox_mode =
    "read-only"`` to enforce a read-only role.
    """
    lines = [
        f"name = {json.dumps(name)}",
        f"description = {json.dumps(defn.description)}",
    ]
    if model:
        lines.append(f"model = {json.dumps(model)}")
    if defn.readonly:
        lines.append('sandbox_mode = "read-only"')
    # A TOML basic string (json.dumps) escapes quotes/newlines safely.
    lines.append(f"developer_instructions = {json.dumps(defn.prompt_body())}")
    return "\n".join(lines) + "\n"


def _compile_markdown(name: str, defn: AgentDefinition, model: str | None, *, cursor: bool) -> str:
    """Render a Cursor/Claude Markdown sub-agent (YAML frontmatter + body).

    Cursor supports a native ``readonly`` field; Claude carries ``tools`` and
    relies on the preamble for read-only intent.
    """
    header: dict[str, object] = {"name": name, "description": defn.description}
    if cursor:
        if model:
            header["model"] = model
        if defn.readonly:
            header["readonly"] = True
    else:  # Claude
        if defn.tools:
            header["tools"] = ", ".join(defn.tools)
        if model:
            header["model"] = model
    frontmatter = yaml.safe_dump(header, sort_keys=False).strip()
    return f"---\n{frontmatter}\n---\n{defn.prompt_body()}\n"


def compile_agent(
    defn: AgentDefinition, vendor: str, model: str | None, *, name: str | None = None
) -> CompiledAgent:
    """Compile one agent definition into a runtime-native sub-agent file.

    Args:
        defn: The parsed, vendor-neutral agent definition.
        vendor: Target runtime (``codex`` / ``claude`` / ``cursor``).
        model: Model id to attach to the compiled agent (role binding), or None.
        name: Canonical compiled name (the manifest role key). Defaults to the
            agent definition's own name when omitted.

    Returns:
        The rendered :class:`CompiledAgent`.

    Raises:
        AgentCompileError: If the vendor has no known sub-agent layout.
    """
    layout = _VENDOR_LAYOUT.get(vendor)
    if layout is None:
        raise AgentCompileError(
            f"no sub-agent format for vendor '{vendor}' (known: {sorted(_VENDOR_LAYOUT)})"
        )
    directory, ext = layout
    compiled_name = name or defn.name
    if vendor == Vendor.CODEX:
        content = _compile_codex(compiled_name, defn, model)
    else:
        content = _compile_markdown(compiled_name, defn, model, cursor=vendor == Vendor.CURSOR)
    return CompiledAgent(vendor=vendor, relpath=f"{directory}/{compiled_name}.{ext}", content=content)


def write_compiled_agents(workdir: Path, compiled: list[CompiledAgent]) -> list[Path]:
    """Write compiled sub-agent files into a run worktree.

    Each destination is confirmed to remain under ``workdir`` and to be unique
    before writing, so a crafted agent name can neither escape the run directory
    nor silently overwrite another role's file.

    Args:
        workdir: The run worktree root.
        compiled: Rendered agents to materialize.

    Returns:
        The list of written destination paths.

    Raises:
        ValueError: If a compiled file would resolve outside the worktree.
        AgentCompileError: If two compiled agents target the same destination.
    """
    workdir = workdir.resolve()
    written: list[Path] = []
    seen: set[Path] = set()
    for agent in compiled:
        dest = (workdir / agent.relpath).resolve()
        assert_under(workdir, dest, label="compiled agent")
        if dest in seen:
            raise AgentCompileError(f"two roles compile to the same file: {agent.relpath}")
        seen.add(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(agent.content, encoding="utf-8")
        written.append(dest)
    return written
