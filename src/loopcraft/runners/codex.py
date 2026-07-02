"""Codex runtime adapter.

Implements the Codex-specific preflight, scoped-sandbox command construction,
and prompt assembly on top of :class:`BaseRunner`. Runtime-neutral capability
probes (tools/auth/APIs) live in :mod:`loopcraft.runners.capabilities`.
"""

from __future__ import annotations

import shutil

from loopcraft.config import LoopcraftConfig
from loopcraft.manifest import LoopManifest
from loopcraft.runners.base import (
    BaseRunner,
    PreflightReport,
    RunContext,
)

#: Model id prefixes Codex is expected to accept. Kept deliberately broad; the
#: goal is to flag an obviously wrong vendor model (e.g. ``opus``) locally, not
#: to maintain an exact catalog.
_KNOWN_CODEX_MODEL_PREFIXES = ("gpt-", "gpt5", "o1", "o3", "o4", "codex")


def _probe_codex_model(model: str) -> str | None:
    """Flag a model id that does not look like a Codex/OpenAI model."""
    lowered = model.lower()
    if any(lowered.startswith(prefix) for prefix in _KNOWN_CODEX_MODEL_PREFIXES):
        return None
    return f"model '{model}' is not a recognized Codex model (check runtime.model)"


class CodexRunner(BaseRunner):
    """Runtime adapter that executes a loop headless via the Codex CLI."""

    vendor = "codex"

    def preflight(self, loop: LoopManifest, config: LoopcraftConfig) -> PreflightReport:
        """Check Codex can satisfy the loop before running it.

        Verifies the Codex binary, skill/verify files, declared tools, env vars,
        auth bundles, APIs, and (if pinned) the model id.

        Args:
            loop: The loop manifest to preflight.
            config: Resolved control-plane config.

        Returns:
            A report listing any problems found (empty when ready to run).
        """
        problems: list[str] = []

        if shutil.which("codex") is None:
            problems.append("codex CLI not found on PATH (install the Codex runtime)")

        # Runtime-neutral checks (skill/verify assets, tools, env, auth, apis).
        problems += self.check_declared_capabilities(loop, config)

        if loop.runtime.model:
            model_problem = _probe_codex_model(loop.runtime.model)
            if model_problem:
                problems.append(model_problem)

        return PreflightReport(vendor=self.vendor, ok=not problems, problems=problems)

    def build_command(self, loop: LoopManifest, ctx: RunContext) -> list[str]:
        """Build the ``codex exec`` argv with a scoped sandbox for this run.

        Args:
            loop: The loop manifest (supplies model/reasoning effort).
            ctx: Run context (supplies the writable output roots).

        Returns:
            The command argv; the prompt is passed on stdin (trailing ``-``).
        """
        # The run directory is an isolated asset bundle (manifest + skill assets),
        # not a git checkout, so --skip-git-repo-check is intentional.
        #
        # Sandbox is scoped, not bypassed: workspace-write confines writes to the
        # worktree plus the explicit --add-dir roots (the loop's declared output
        # dirs), and network access is enabled so declared tools like nv-tools can
        # reach their APIs. Approvals stay off (exec is non-interactive): anything
        # outside the writable roots is denied, never escalated.
        cmd = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "-s",
            "workspace-write",
            "-c",
            "sandbox_workspace_write.network_access=true",
        ]
        for root in self.writable_roots(ctx):
            cmd += ["--add-dir", root]
        if loop.runtime.model:
            cmd += ["--model", loop.runtime.model]
        if loop.runtime.reasoning_effort:
            cmd += ["-c", f'model_reasoning_effort="{loop.runtime.reasoning_effort}"']
        cmd.append("-")  # read the prompt from stdin
        return cmd

    def build_prompt(self, loop: LoopManifest, ctx: RunContext) -> str:
        """Assemble the stdin prompt: skill, runtime context, I/O contract, verify.

        Args:
            loop: The loop manifest.
            ctx: Run context (paths, resolved outputs).

        Returns:
            The full prompt text sent to Codex on stdin.
        """
        lines: list[str] = []
        skill_text = self._load_source_text(ctx, loop.logic.skill)

        lines.append(f"# Loop: {loop.name} ({loop.id})")
        lines.append(loop.description)
        lines.append("")
        lines.append("## Runtime context")
        lines.append(f"- source tree (repo with Makefile/config/src): {ctx.config.source_path}")
        lines.append(f"- run worktree (staged loop assets, current cwd): {ctx.workdir}")
        if loop.content.config:
            lines.append(f"- content definition: {loop.content.config}")
        lines.append(
            "- If the skill invokes a repo-local CLI or Makefile target, run it from the source tree."
        )
        lines.append("")
        if skill_text:
            lines.append("## Skill")
            lines.append(skill_text)
            lines.append("")

        lines.append("## I/O contract")
        lines.append(f"- tier: {loop.tier} (observe = read-only; never take irreversible actions)")
        if loop.inputs:
            lines.append("- inputs (read these):")
            for declared in loop.inputs:
                lines.append(f"  - {declared} -> {ctx.config.resolve_state_path(declared)}")
        if loop.outputs:
            lines.append("- outputs (write exactly these absolute paths):")
            for declared, resolved in zip(loop.outputs, ctx.resolved_outputs):
                lines.append(f"  - {declared} -> {resolved}")
        verify_text = self._load_source_text(ctx, loop.logic.verify)
        if verify_text:
            lines.append("")
            lines.append("## Stop condition (verify)")
            lines.append(verify_text)

        budget = loop.budget
        if budget.max_turns or budget.max_runtime:
            lines.append(
                f"- budget: max_turns={budget.max_turns}, max_runtime={budget.max_runtime}"
            )
        return "\n".join(lines) + "\n"
