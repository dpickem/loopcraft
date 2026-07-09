"""Vendor-neutral agent definitions for multi-model loop roles (M3.5).

A role in a maker/checker loop binds a **behavior** to an **execution**. The
behavior is an *agent definition*: a small, vendor-neutral spec — instructions
plus the tools it may touch, a read-only flag, and an optional verify rubric —
authored as a markdown file with a YAML frontmatter header, the same shape as a
``SKILL.md`` but scoped to one role:

    ---
    name: reviewer
    description: "Adversarial code reviewer."
    readonly: true
    tools: [nv-tools.gitlab, repo-read]
    verify: "every claimed issue cites a file:line"
    ---
    You are the checker, not the maker. Output blockers, then nits, then PASS/FAIL.

The agent definition is the single source of truth for what a role does; the
role's ``vendor``/``model`` is a swappable binding on top of it. The
:mod:`loopcraft.agent_compiler` module compiles a parsed definition into each
runtime's native sub-agent format.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

#: Fence that opens/closes the YAML frontmatter block at the top of a file.
_FRONTMATTER_FENCE = "---"


class AgentDefinitionError(Exception):
    """Raised when an agent definition cannot be parsed or is invalid."""


class AgentDefinition(BaseModel):
    """A parsed, vendor-neutral role behavior.

    Attributes:
        name: Role/agent name (used as the compiled sub-agent filename stem).
        description: One-line summary of the behavior.
        readonly: Whether the role must not edit files (a checker/reviewer). The
            compiler maps this to each runtime's read-only affordance and it is
            restated in the compiled prompt so a role can never quietly become a
            maker just by swapping its engine.
        tools: Vendor-neutral tool/collection names the role may use.
        verify: Optional stop/acceptance rubric for the role.
        instructions: The markdown body — the role's system prompt.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    readonly: bool = False
    tools: list[str] = Field(default_factory=list)
    verify: str | None = None
    instructions: str = ""

    def readonly_preamble(self) -> str:
        """Return the read-only contract line for a checker role (or empty).

        The ``readonly`` policy travels with the role, not the model, so this is
        restated in every compiled/inline prompt as defense in depth alongside
        native runtime enforcement and the control-plane post-run check.
        """
        if not self.readonly:
            return ""
        return (
            "IMPORTANT: You are a READ-ONLY review role. Do not modify source code "
            "or another role's outputs, and do not run mutating commands. You may "
            "write only your own declared review output(s). Report findings only.\n\n"
        )

    def prompt_body(self) -> str:
        """Assemble the full instruction body: preamble + instructions + verify."""
        body = self.readonly_preamble() + self.instructions
        if self.verify:
            body += f"\n\n## Acceptance criteria (verify)\n{self.verify}"
        return body


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown document into its YAML frontmatter and body.

    A document may open with a ``---`` fenced YAML block; everything after the
    closing fence is the body. When no frontmatter is present the whole document
    is the body and the metadata mapping is empty.

    Raises:
        AgentDefinitionError: If a frontmatter block is opened but never closed,
            or its YAML is not a mapping.
    """
    if not text.startswith(_FRONTMATTER_FENCE):
        return {}, text
    lines = text.splitlines()
    closing = None
    for index in range(1, len(lines)):
        if lines[index].strip() == _FRONTMATTER_FENCE:
            closing = index
            break
    if closing is None:
        raise AgentDefinitionError("frontmatter block opened with '---' but never closed")
    header_text = "\n".join(lines[1:closing])
    body = "\n".join(lines[closing + 1 :])
    try:
        meta = yaml.safe_load(header_text) or {}
    except yaml.YAMLError as exc:
        raise AgentDefinitionError(f"invalid frontmatter YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise AgentDefinitionError("frontmatter must be a mapping")
    return meta, body


def parse_agent_definition(text: str, *, name_hint: str | None = None) -> AgentDefinition:
    """Parse an agent definition from markdown-with-frontmatter text.

    Args:
        text: The raw file contents.
        name_hint: Fallback ``name`` (e.g. the filename stem) used when the
            frontmatter omits it.

    Returns:
        The parsed :class:`AgentDefinition`.

    Raises:
        AgentDefinitionError: If the frontmatter is malformed, an unknown field
            is present, or no name can be resolved.
    """
    meta, body = _split_frontmatter(text)
    payload = dict(meta)
    payload.setdefault("name", name_hint)
    payload["instructions"] = body.strip()
    if not payload.get("name"):
        raise AgentDefinitionError("agent definition requires a 'name' (in frontmatter or filename)")
    try:
        return AgentDefinition.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — normalize pydantic errors to our type
        raise AgentDefinitionError(str(exc)) from exc


def load_agent_definition(path: Path | str) -> AgentDefinition:
    """Load and parse an agent definition file from disk.

    Raises:
        AgentDefinitionError: If the file cannot be read or parsed.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentDefinitionError(f"cannot read agent definition {path}: {exc}") from exc
    return parse_agent_definition(text, name_hint=path.stem)
