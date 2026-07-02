"""Codex runtime adapter.

Implements the Codex-specific preflight probes, scoped-sandbox command
construction, and prompt assembly on top of :class:`BaseRunner`.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable

from loopcraft.config import LoopcraftConfig, SourcePathError
from loopcraft.manifest import LoopManifest
from loopcraft.probes import run_probe
from loopcraft.runners.base import (
    BaseRunner,
    PreflightReport,
    RunContext,
)

#: Tool collections map to a CLI binary that must be on PATH for preflight.
_TOOL_BINARIES = {"nv-tools": "nv-tools"}

#: Seconds allowed for a bounded capability probe (a single targeted API read).
_PROBE_TIMEOUT_S = 30

#: Model id prefixes Codex is expected to accept. Kept deliberately broad; the
#: goal is to flag an obviously wrong vendor model (e.g. ``opus``) locally, not
#: to maintain an exact catalog.
_KNOWN_CODEX_MODEL_PREFIXES = ("gpt-", "gpt5", "o1", "o3", "o4", "codex")


def _probe_nv_tools_auth(config: LoopcraftConfig) -> str | None:
    """Check the nv-tools connector is installed.

    The auth *bundle* is verified by presence of the CLI; the live credential
    check happens per declared API (e.g. the Slack probe below), so we avoid the
    slow, all-services ``nv-tools health`` that fails on unrelated services.
    """
    if shutil.which("nv-tools") is None:
        return "auth bundle 'nv-tools': nv-tools CLI not found on PATH"
    return None


def _probe_slack_api(config: LoopcraftConfig) -> str | None:
    """Verify Slack access with a bounded, read-only, single-service probe.

    Uses ``nv-tools slack list-channels --limit 1`` rather than ``nv-tools
    health`` so the check is fast and scoped to the one service the loop needs,
    instead of failing when some other nv-tools service is unhealthy.
    """
    if shutil.which("nv-tools") is None:
        return "api 'slack': requires the nv-tools connector on PATH"
    rc = run_probe(["nv-tools", "slack", "list-channels", "--limit", "1", "--format", "json"], timeout_s=_PROBE_TIMEOUT_S)
    if rc is None:
        return "api 'slack': could not run the Slack read probe (nv-tools slack list-channels)"
    if rc != 0:
        return (
            "api 'slack': Slack read probe failed — check `nv-tools slack list-channels` "
            "(auth/config)"
        )
    return None


def _probe_x_api_auth(config: LoopcraftConfig) -> str | None:
    """Verify X API credentials are present for X intelligence loops."""
    if not config.env_value("X_API_BEARER_TOKEN") and not config.env_value("X_API_OAUTH2_ACCESS_TOKEN"):
        return "auth bundle 'x-api': X_API_BEARER_TOKEN or X_API_OAUTH2_ACCESS_TOKEN is required"
    return None


def _probe_x_api(config: LoopcraftConfig) -> str | None:
    """X access is validated by the x-api auth/env checks; avoid live preflight calls."""
    return None


def _probe_arxiv_api(config: LoopcraftConfig) -> str | None:
    """arXiv is public/no-auth; the loop handles API errors in its run output."""
    return None


#: Auth-bundle probes: bundle name -> callable returning a problem string or None.
#: Injectable so tests (and later milestones) can substitute probes.
AUTH_PROBES: dict[str, Callable[[LoopcraftConfig], str | None]] = {
    "nv-tools": _probe_nv_tools_auth,
    "x-api": _probe_x_api_auth,
}

#: Declared-API probes: api name -> callable returning a problem string or None.
API_PROBES: dict[str, Callable[[LoopcraftConfig], str | None]] = {
    "slack": _probe_slack_api,
    "x": _probe_x_api,
    "arxiv": _probe_arxiv_api,
}


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
        problems: list[str] = []

        if shutil.which("codex") is None:
            problems.append("codex CLI not found on PATH (install the Codex runtime)")

        skill = loop.logic.skill
        if skill:
            try:
                skill_path = config.resolve_source_path(skill)
            except SourcePathError as exc:
                problems.append(f"logic.skill: {exc}")
            else:
                if not skill_path.exists():
                    problems.append(f"skill not found: {skill}")
        else:
            problems.append("logic.skill is required")

        verify = loop.logic.verify
        if verify:
            try:
                verify_path = config.resolve_source_path(verify)
            except SourcePathError as exc:
                problems.append(f"logic.verify: {exc}")
            else:
                if not verify_path.exists():
                    problems.append(f"verify file not found: {verify}")

        for tool in loop.depends_on.tools:
            binary = _TOOL_BINARIES.get(tool, tool)
            if shutil.which(binary) is None:
                problems.append(f"declared tool '{tool}' not found on PATH ({binary})")

        for var in loop.depends_on.env:
            if not config.env_value(var):
                problems.append(f"required env var not set: {var}")

        # Declared auth bundles and APIs: probe known ones, flag unknown ones so a
        # missing setup is caught before a headless run rather than inside the agent.
        for bundle in loop.depends_on.auth:
            probe = AUTH_PROBES.get(bundle)
            if probe is None:
                problems.append(f"no preflight probe for declared auth bundle '{bundle}'")
                continue
            problem = probe(config)
            if problem:
                problems.append(problem)

        for api in loop.depends_on.apis:
            probe = API_PROBES.get(api)
            if probe is None:
                problems.append(f"no preflight probe for declared api '{api}'")
                continue
            problem = probe(config)
            if problem:
                problems.append(problem)

        if loop.runtime.model:
            model_problem = _probe_codex_model(loop.runtime.model)
            if model_problem:
                problems.append(model_problem)

        return PreflightReport(vendor=self.vendor, ok=not problems, problems=problems)

    def build_command(self, loop: LoopManifest, ctx: RunContext) -> list[str]:
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
