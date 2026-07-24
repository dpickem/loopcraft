"""Tests for agent-definition parsing and the runtime-native compiler (M3.5)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from loopcraft.agent_compiler import (
    AgentCompileError,
    compile_agent,
    write_compiled_agents,
)
from loopcraft.agents import (
    AgentDefinitionError,
    load_agent_definition,
    parse_agent_definition,
)
from loopcraft.role_tools import ToolAccess, role_tool_problems, tool_access

_REVIEWER = """---
name: reviewer
description: "Adversarial reviewer."
readonly: true
tools: [repo-read]
verify: "cites file:line"
---
You are the checker, not the maker.
"""


def test_parse_agent_definition_frontmatter_and_body() -> None:
    """Frontmatter fields parse and the body becomes instructions."""
    defn = parse_agent_definition(_REVIEWER)
    assert defn.name == "reviewer"
    assert defn.readonly is True
    assert defn.tools == ["repo-read"]
    assert defn.verify == "cites file:line"
    assert defn.instructions == "You are the checker, not the maker."


def test_readonly_reviewer_can_spawn_review_agents_and_run_tests() -> None:
    """Reviewer orchestration/test capabilities are executable, not mutating."""
    tools = ["repo-read", "agent-spawn", "test-run"]
    assert tool_access("agent-spawn") is ToolAccess.EXECUTE
    assert tool_access("test-run") is ToolAccess.EXECUTE
    assert role_tool_problems("reviewer", tools, readonly=True) == []


def test_parse_uses_name_hint_when_frontmatter_absent() -> None:
    """A document without frontmatter falls back to the filename-stem name hint."""
    defn = parse_agent_definition("just instructions", name_hint="implementer")
    assert defn.name == "implementer"
    assert defn.instructions == "just instructions"
    assert defn.readonly is False


def test_parse_requires_a_name() -> None:
    """Parsing fails when neither frontmatter nor a hint supplies a name."""
    with pytest.raises(AgentDefinitionError):
        parse_agent_definition("no name anywhere")


def test_parse_rejects_unclosed_frontmatter() -> None:
    """An opened but unclosed frontmatter block is an error."""
    with pytest.raises(AgentDefinitionError):
        parse_agent_definition("---\nname: x\nstill going")


def test_parse_rejects_unknown_field() -> None:
    """Unknown frontmatter keys are rejected (extra=forbid)."""
    with pytest.raises(AgentDefinitionError):
        parse_agent_definition("---\nname: x\nbogus: 1\n---\nbody")


def test_load_agent_definition(tmp_path: Path) -> None:
    """load_agent_definition reads and parses a file, defaulting name to the stem."""
    path = tmp_path / "reviewer.md"
    path.write_text("body only", encoding="utf-8")
    defn = load_agent_definition(path)
    assert defn.name == "reviewer"
    assert defn.instructions == "body only"


def test_compile_claude_produces_frontmatter_and_model() -> None:
    """Claude compilation yields frontmatter with the attached model + body."""
    defn = parse_agent_definition(_REVIEWER)
    compiled = compile_agent(defn, "claude", "opus")
    assert compiled.relpath == ".claude/agents/reviewer.md"
    assert compiled.content.startswith("---\n")
    header = yaml.safe_load(compiled.content.split("---\n")[1])
    assert header["name"] == "reviewer"
    assert header["model"] == "opus"
    assert "READ-ONLY" in compiled.content  # readonly preamble present


def test_compile_cursor_is_markdown_with_model() -> None:
    """Cursor compilation yields a Markdown sub-agent (frontmatter + body)."""
    defn = parse_agent_definition(_REVIEWER)
    compiled = compile_agent(defn, "cursor", "claude-opus-4-8")
    assert compiled.relpath == ".cursor/agents/reviewer.md"
    assert compiled.content.startswith("---\n")
    header = yaml.safe_load(compiled.content.split("---\n")[1])
    assert header["name"] == "reviewer"
    assert header["model"] == "claude-opus-4-8"
    assert header["readonly"] is True  # Cursor supports native read-only
    assert "READ-ONLY" in compiled.content


def test_compile_codex_is_valid_toml_with_current_schema() -> None:
    """Codex compilation uses developer_instructions + sandbox_mode (read-only)."""
    tomllib = pytest.importorskip("tomllib")
    defn = parse_agent_definition(_REVIEWER)
    compiled = compile_agent(defn, "codex", "gpt-5.5")
    assert compiled.relpath == ".codex/agents/reviewer.toml"
    doc = tomllib.loads(compiled.content)
    assert doc["name"] == "reviewer"
    assert doc["model"] == "gpt-5.5"
    assert doc["sandbox_mode"] == "read-only"
    assert "developer_instructions" in doc
    assert "cites file:line" in doc["developer_instructions"]  # verify travels


def test_compile_uses_canonical_name_override() -> None:
    """The compiled name/filename come from the role key, not the def name."""
    defn = parse_agent_definition(_REVIEWER)  # def name is 'reviewer'
    compiled = compile_agent(defn, "cursor", None, name="checker")
    assert compiled.relpath == ".cursor/agents/checker.md"
    header = yaml.safe_load(compiled.content.split("---\n")[1])
    assert header["name"] == "checker"


def test_compile_rejects_unknown_vendor() -> None:
    """An unknown vendor has no sub-agent layout and is rejected."""
    defn = parse_agent_definition(_REVIEWER)
    with pytest.raises(AgentCompileError):
        compile_agent(defn, "gemini", "x")


def test_write_compiled_agents_materializes_files(tmp_path: Path) -> None:
    """Compiled agents are written under the worktree at their relpath."""
    defn = parse_agent_definition(_REVIEWER)
    compiled = [compile_agent(defn, "cursor", "opus")]
    written = write_compiled_agents(tmp_path, compiled)
    assert written[0] == (tmp_path / ".cursor/agents/reviewer.md").resolve()
    assert written[0].read_text(encoding="utf-8")


def test_write_compiled_agents_rejects_escape(tmp_path: Path) -> None:
    """A compiled agent path that escapes the worktree is refused."""
    defn = parse_agent_definition(_REVIEWER)
    compiled = [compile_agent(defn, "cursor", "opus")]
    compiled[0].relpath = "../escape.md"
    with pytest.raises(ValueError):
        write_compiled_agents(tmp_path, compiled)


def test_write_compiled_agents_rejects_duplicate_destination(tmp_path: Path) -> None:
    """Two agents targeting the same file are refused (no silent overwrite)."""
    defn = parse_agent_definition(_REVIEWER)
    compiled = [compile_agent(defn, "cursor", "opus"), compile_agent(defn, "cursor", "opus")]
    with pytest.raises(AgentCompileError):
        write_compiled_agents(tmp_path, compiled)
