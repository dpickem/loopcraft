from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable

from ..config import LoopcraftConfig, SourcePathError
from ..manifest import LoopManifest
from .base import (
    PreflightReport,
    RunContext,
    RunResult,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_STALLED,
)

#: Tool collections map to a CLI binary that must be on PATH for preflight.
_TOOL_BINARIES = {"nv-tools": "nv-tools"}

#: Seconds allowed for a bounded capability probe (a single targeted API read).
_PROBE_TIMEOUT_S = 30

#: Model id prefixes Codex is expected to accept. Kept deliberately broad; the
#: goal is to flag an obviously wrong vendor model (e.g. ``opus``) locally, not
#: to maintain an exact catalog.
_KNOWN_CODEX_MODEL_PREFIXES = ("gpt-", "gpt5", "o1", "o3", "o4", "codex")


def _run_probe(cmd: list[str]) -> int | None:
    """Run a bounded, read-only probe command; return its exit code or None.

    None means the probe could not be executed (missing binary or timeout); the
    caller decides how to report that.
    """
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_PROBE_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return completed.returncode


def _probe_nv_tools_auth() -> str | None:
    """Check the nv-tools connector is installed.

    The auth *bundle* is verified by presence of the CLI; the live credential
    check happens per declared API (e.g. the Slack probe below), so we avoid the
    slow, all-services ``nv-tools health`` that fails on unrelated services.
    """
    if shutil.which("nv-tools") is None:
        return "auth bundle 'nv-tools': nv-tools CLI not found on PATH"
    return None


def _probe_slack_api() -> str | None:
    """Verify Slack access with a bounded, read-only, single-service probe.

    Uses ``nv-tools slack list-channels --limit 1`` rather than ``nv-tools
    health`` so the check is fast and scoped to the one service the loop needs,
    instead of failing when some other nv-tools service is unhealthy.
    """
    if shutil.which("nv-tools") is None:
        return "api 'slack': requires the nv-tools connector on PATH"
    rc = _run_probe(
        ["nv-tools", "slack", "list-channels", "--limit", "1", "--format", "json"]
    )
    if rc is None:
        return "api 'slack': could not run the Slack read probe (nv-tools slack list-channels)"
    if rc != 0:
        return (
            "api 'slack': Slack read probe failed — check `nv-tools slack list-channels` "
            "(auth/config)"
        )
    return None


#: Auth-bundle probes: bundle name -> callable returning a problem string or None.
#: Injectable so tests (and later milestones) can substitute probes.
AUTH_PROBES: dict[str, Callable[[], str | None]] = {"nv-tools": _probe_nv_tools_auth}

#: Declared-API probes: api name -> callable returning a problem string or None.
API_PROBES: dict[str, Callable[[], str | None]] = {"slack": _probe_slack_api}


def _probe_codex_model(model: str) -> str | None:
    """Flag a model id that does not look like a Codex/OpenAI model."""
    lowered = model.lower()
    if any(lowered.startswith(prefix) for prefix in _KNOWN_CODEX_MODEL_PREFIXES):
        return None
    return f"model '{model}' is not a recognized Codex model (check runtime.model)"


class CodexRunner:
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

        for tool in loop.depends_on.tools:
            binary = _TOOL_BINARIES.get(tool, tool)
            if shutil.which(binary) is None:
                problems.append(f"declared tool '{tool}' not found on PATH ({binary})")

        for var in loop.depends_on.env:
            if not os.environ.get(var):
                problems.append(f"required env var not set: {var}")

        # Declared auth bundles and APIs: probe known ones, flag unknown ones so a
        # missing setup is caught before a headless run rather than inside the agent.
        for bundle in loop.depends_on.auth:
            probe = AUTH_PROBES.get(bundle)
            if probe is None:
                problems.append(f"no preflight probe for declared auth bundle '{bundle}'")
                continue
            problem = probe()
            if problem:
                problems.append(problem)

        for api in loop.depends_on.apis:
            probe = API_PROBES.get(api)
            if probe is None:
                problems.append(f"no preflight probe for declared api '{api}'")
                continue
            problem = probe()
            if problem:
                problems.append(problem)

        if loop.runtime.model:
            model_problem = _probe_codex_model(loop.runtime.model)
            if model_problem:
                problems.append(model_problem)

        return PreflightReport(vendor=self.vendor, ok=not problems, problems=problems)

    def run(self, loop: LoopManifest, ctx: RunContext) -> RunResult:
        prompt = self._build_prompt(loop, ctx)
        cmd = self._build_command(loop, ctx)

        ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, **ctx.env}

        # Snapshot declared outputs before the run so a successful exit that did
        # not actually (re)write an output is caught, instead of passing on stale
        # files left by a prior run.
        pre_mtimes = {
            p: (p.stat().st_mtime_ns if p.exists() else None) for p in ctx.resolved_outputs
        }

        # Enforce the manifest's runtime budget so an unattended run can't hang.
        try:
            timeout_s = loop.budget.max_runtime_s
        except ValueError:
            timeout_s = None  # malformed durations are caught by manifest validation

        try:
            completed = subprocess.run(
                cmd,
                input=prompt,
                cwd=str(ctx.workdir),
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_s,
            )
        except FileNotFoundError:
            ctx.log_path.write_text(prompt, encoding="utf-8")
            return RunResult(
                status=STATUS_FAILED,
                exit_code=127,
                log_path=ctx.log_path,
                problems=["codex CLI not found on PATH"],
            )
        except subprocess.TimeoutExpired as exc:
            partial_out = exc.stdout or ""
            partial_err = exc.stderr or ""
            if isinstance(partial_out, bytes):
                partial_out = partial_out.decode("utf-8", "replace")
            if isinstance(partial_err, bytes):
                partial_err = partial_err.decode("utf-8", "replace")
            log = (
                f"$ {' '.join(cmd)}\n\n--- PROMPT ---\n{prompt}\n\n"
                f"--- TIMEOUT after {timeout_s}s ---\n"
                f"--- STDOUT (partial) ---\n{partial_out}\n"
                f"--- STDERR (partial) ---\n{partial_err}\n"
            )
            ctx.log_path.write_text(log, encoding="utf-8")
            return RunResult(
                status=STATUS_STALLED,
                exit_code=None,
                log_path=ctx.log_path,
                problems=[
                    f"exceeded budget.max_runtime ({loop.budget.max_runtime}); "
                    f"aborted after {timeout_s}s"
                ],
            )

        log = (
            f"$ {' '.join(cmd)}\n\n--- PROMPT ---\n{prompt}\n\n"
            f"--- STDOUT ---\n{completed.stdout}\n--- STDERR ---\n{completed.stderr}\n"
        )
        ctx.log_path.write_text(log, encoding="utf-8")

        missing = [p for p in ctx.resolved_outputs if not p.exists()]
        stale = [
            p
            for p in ctx.resolved_outputs
            if p.exists()
            and pre_mtimes[p] is not None
            and p.stat().st_mtime_ns == pre_mtimes[p]
        ]
        stale_set = set(stale)
        produced = [str(p) for p in ctx.resolved_outputs if p.exists() and p not in stale_set]

        if completed.returncode != 0:
            problems = [f"codex exited {completed.returncode}"]
        else:
            problems = [f"declared output not produced: {p}" for p in missing]
            problems += [f"declared output not refreshed this run: {p}" for p in stale]
        status = (
            STATUS_DONE
            if completed.returncode == 0 and not missing and not stale
            else STATUS_FAILED
        )

        return RunResult(
            status=status,
            exit_code=completed.returncode,
            log_path=ctx.log_path,
            outputs=produced,
            problems=problems,
        )

    def _build_command(self, loop: LoopManifest, ctx: RunContext) -> list[str]:
        # The run directory is an isolated asset bundle (manifest + skill assets),
        # not a git checkout, so --skip-git-repo-check is intentional here.
        cmd = ["codex", "exec", "--skip-git-repo-check"]
        if loop.runtime.model:
            cmd += ["--model", loop.runtime.model]
        cmd.append("-")  # read the prompt from stdin
        return cmd

    def _build_prompt(self, loop: LoopManifest, ctx: RunContext) -> str:
        lines: list[str] = []
        skill_text = ""
        if loop.logic.skill:
            try:
                skill_path = ctx.config.resolve_source_path(loop.logic.skill)
            except SourcePathError:
                skill_path = None  # unsafe paths are rejected earlier by preflight
            if skill_path is not None and skill_path.exists():
                skill_text = skill_path.read_text(encoding="utf-8")

        lines.append(f"# Loop: {loop.name} ({loop.id})")
        lines.append(loop.description)
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
        if loop.logic.verify:
            lines.append(f"- stop condition: {loop.logic.verify}")

        budget = loop.budget
        if budget.max_turns or budget.max_runtime:
            lines.append(
                f"- budget: max_turns={budget.max_turns}, max_runtime={budget.max_runtime}"
            )
        return "\n".join(lines) + "\n"
