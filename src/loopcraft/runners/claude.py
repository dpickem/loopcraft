"""Claude Code runtime adapter.

Implements the Claude-specific preflight and headless command construction on
top of :class:`BaseRunner`. The prompt and the runtime-neutral capability checks
(skill/verify assets, tools, env, auth, APIs) are shared via ``BaseRunner`` and
:mod:`loopcraft.runners.capabilities`, so an existing loop runs unchanged when
its ``runtime.vendor`` is switched to ``claude`` — that portability is the whole
point of M3.
"""

from __future__ import annotations

from loopcraft.config import LoopcraftConfig
from loopcraft.manifest import LoopManifest
from loopcraft.runners.base import BaseRunner, PreflightReport, RunContext

#: Claude Code alias model values (resolve to the latest of each family).
_KNOWN_CLAUDE_ALIASES = frozenset({"sonnet", "opus", "haiku"})
#: Prefix for pinned full Claude model names (e.g. ``claude-opus-4-8``). Kept
#: deliberately broad: the goal is to flag an obviously wrong vendor model (e.g.
#: a ``gpt-*`` slug) locally, not to track an exact catalog.
_KNOWN_CLAUDE_PREFIXES = ("claude-",)


def _probe_claude_model(model: str) -> str | None:
    """Flag a model id that does not look like a Claude Code model."""
    lowered = model.lower()
    if lowered in _KNOWN_CLAUDE_ALIASES or lowered.startswith(_KNOWN_CLAUDE_PREFIXES):
        return None
    return (
        f"model '{model}' is not a recognized Claude model "
        "(use an alias like 'sonnet'/'opus' or a 'claude-*' name; check runtime.model)"
    )


class ClaudeRunner(BaseRunner):
    """Runtime adapter that executes a loop headless via the Claude Code CLI."""

    vendor = "claude"

    def preflight(self, loop: LoopManifest, config: LoopcraftConfig) -> PreflightReport:
        """Check Claude Code can satisfy the loop before running it.

        Verifies the ``claude`` binary (on the scheduled/operator PATH per
        ``config.which``), the shared declared capabilities, and (if pinned) the
        model id.

        Returns:
            A report listing any problems found (empty when ready to run).
        """
        problems: list[str] = []
        if config.which("claude") is None:
            problems.append("claude CLI not found on PATH (install Claude Code)")
        problems += self.check_declared_capabilities(loop, config)
        if loop.runtime.model:
            model_problem = _probe_claude_model(loop.runtime.model)
            if model_problem:
                problems.append(model_problem)
        return PreflightReport(vendor=self.vendor, ok=not problems, problems=problems)

    def build_command(self, loop: LoopManifest, ctx: RunContext) -> list[str]:
        """Build the headless ``claude -p`` argv for one loop invocation.

        Runs non-interactively (``-p``) reading the prompt from stdin (the base
        runner pipes it), with the loop's declared output directories granted as
        extra writable roots. ``--permission-mode acceptEdits`` auto-approves
        file edits without a prompt; a loop that also drives tools needing broader
        permission (e.g. arbitrary bash) may require a wider mode on the host.

        Returns:
            The command argv; the prompt is supplied on stdin by the base runner.
        """
        cmd = ["claude", "-p", "--output-format", "text"]
        if loop.runtime.model:
            cmd += ["--model", loop.runtime.model]
        if loop.runtime.reasoning_effort:
            cmd += ["--effort", loop.runtime.reasoning_effort]
        for root in self.writable_roots(ctx):
            cmd += ["--add-dir", root]
        cmd += ["--permission-mode", "acceptEdits"]
        return cmd
