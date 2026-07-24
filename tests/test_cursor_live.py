"""Opt-in live smoke test: Cursor spawns a cross-provider sub-agent (M3.5).

This is the one exit-criterion check that cannot be proven offline — it runs a
real, paid ``cursor-agent`` invocation and asserts that the sub-agent compiled by
:mod:`loopcraft.agent_compiler` is discovered and spawned on a *different
provider's* model than the main agent (e.g. a GPT main agent spawning a
Claude-Opus reviewer). Per ``CONTRIBUTING.md`` the default suite stays offline,
so this test is skipped unless explicitly enabled:

    LOOPCRAFT_LIVE_CURSOR=1 uv run pytest tests/test_cursor_live.py -q

Optional overrides (defaults reflect a plan that honors per-sub-agent models):

    LOOPCRAFT_LIVE_CURSOR_MAIN_MODEL=gpt-5.5-high
    LOOPCRAFT_LIVE_CURSOR_SUB_MODEL=claude-opus-4-8-high

Note: per-sub-agent model selection is plan-dependent. On legacy request-based
plans without Max Mode, Cursor may run sub-agents on the parent/Composer model
regardless of the compiled ``model`` field; this test asserts the cross-provider
binding and will fail on such plans (that is the intended signal).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from loopcraft.agents import parse_agent_definition
from loopcraft.agent_compiler import compile_agent, write_compiled_agents

_MAIN_MODEL = os.environ.get("LOOPCRAFT_LIVE_CURSOR_MAIN_MODEL", "gpt-5.5-high")
_SUB_MODEL = os.environ.get("LOOPCRAFT_LIVE_CURSOR_SUB_MODEL", "claude-opus-4-8-high")

pytestmark = pytest.mark.skipif(
    not os.environ.get("LOOPCRAFT_LIVE_CURSOR") or shutil.which("cursor-agent") is None,
    reason="live Cursor smoke test (set LOOPCRAFT_LIVE_CURSOR=1 with cursor-agent authenticated)",
)


def _task_spawns(stream_path: Path) -> list[dict]:
    """Return every sub-agent task tool call recorded in a stream-json log."""
    spawns: list[dict] = []
    for line in stream_path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        task = event.get("taskToolCall") if isinstance(event, dict) else None
        if task and isinstance(task.get("args"), dict):
            spawns.append(task["args"])
    return spawns


def test_cursor_spawns_cross_provider_subagent(tmp_path: Path) -> None:
    """A GPT main agent spawns the compiled Opus reviewer sub-agent (cross-provider)."""
    # Compile the reviewer sub-agent with our real compiler (Opus binding).
    defn = parse_agent_definition(
        "---\nname: reviewer\ndescription: Independent reviewer. Use PROACTIVELY to review text.\n"
        "readonly: true\n---\nYou are the reviewer sub-agent. Output exactly: Verdict: PASS",
        name_hint="reviewer",
    )
    compiled = compile_agent(defn, "cursor", _SUB_MODEL, name="reviewer")
    write_compiled_agents(tmp_path, [compiled])
    assert (tmp_path / ".cursor" / "agents" / "reviewer.md").exists()

    stream = tmp_path / "stream.jsonl"
    completed = subprocess.run(
        [
            "cursor-agent", "-p", "--force", "--model", _MAIN_MODEL,
            "--output-format", "stream-json",
            "Delegate to the 'reviewer' subagent to review the string 'hello'. "
            "Return only what the reviewer reports.",
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    stream.write_text(completed.stdout + completed.stderr, encoding="utf-8")

    spawns = _task_spawns(stream)
    reviewer_spawns = [
        s for s in spawns if (s.get("subagentType", {}).get("custom", {}).get("name") == "reviewer")
    ]
    assert reviewer_spawns, f"reviewer sub-agent was not spawned; events: {stream.read_text()[:2000]}"
    # The compiled cross-provider model binding is honored by the runtime.
    assert any(s.get("model") == _SUB_MODEL for s in reviewer_spawns), (
        f"reviewer did not run on {_SUB_MODEL}: {[s.get('model') for s in reviewer_spawns]}"
    )
    assert _SUB_MODEL != _MAIN_MODEL  # cross-provider by construction
