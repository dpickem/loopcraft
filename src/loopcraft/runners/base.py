"""Runtime-agnostic runner base class.

``BaseRunner`` owns the shared headless-run lifecycle — output snapshotting,
timeout enforcement, logging, stale-output detection, and result assembly — and
defines the abstract hooks (``preflight``, ``build_command``, ``build_prompt``)
that each vendor runner implements.
"""

from __future__ import annotations

import os
import subprocess
from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from loopcraft.config import LoopcraftConfig, SourcePathError
from loopcraft.manifest import LoopManifest
from loopcraft.outputs import OutputBinding, is_safe_regular_file
from loopcraft.paths import is_lexically_under
from loopcraft.runners.capabilities import check_declared_capabilities


class RunStatus(StrEnum):
    """Closed vocabulary of normalized run statuses across all runners."""

    DONE = "done"
    STALLED = "stalled"
    FAILED = "failed"
    NEEDS_APPROVAL = "needs_approval"


class _RunnerModel(BaseModel):
    """Base model for runner data structures."""

    model_config = ConfigDict(arbitrary_types_allowed=True)


class PreflightReport(_RunnerModel):
    """Result of checking that a vendor can satisfy a loop before running it."""

    vendor: str
    ok: bool
    problems: list[str] = Field(default_factory=list)


class RunContext(_RunnerModel):
    """Everything a runner needs to execute one loop, isolated from others.

    ``extra_context`` is appended verbatim to the assembled prompt. The
    multi-model orchestrator (M3.5) uses it to hand a prior stage's output to
    the next stage and to describe the sub-agents available in an intra-run
    harness; it is empty for an ordinary single-stage run.

    ``resolved_outputs`` are the paths the agent actually writes. Under the
    worktree-local output model these live inside ``workdir``; ``output_bindings``
    (when set) map each to its durable ledger destination for post-run promotion.
    """

    config: LoopcraftConfig
    workdir: Path
    log_path: Path
    resolved_outputs: list[Path] = Field(default_factory=list)
    output_bindings: list[OutputBinding] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    extra_context: str = ""


class StageRunResult(_RunnerModel):
    """One stage's record in a multi-model pipeline run (M3.5).

    Preserved on the aggregate :class:`RunResult` and copied into the durable
    run record, so each stage stays independently observable and costed.
    """

    role: str
    vendor: str
    model: str | None = None
    status: str
    verdict: str | None = None
    exit_code: int | None = None
    tokens: int | None = None
    cost_usd: float | None = None
    log_path: str | None = None


class RunResult(_RunnerModel):
    """Normalized outcome of a headless run, across vendors.

    ``stages`` carries per-stage records for a multi-model pipeline run (empty
    for a single-model run), so aggregate status/cost never hides which stage
    did what (see the M3.5 orchestrator).
    """

    status: str
    exit_code: int | None = None
    tokens: int | None = None
    cost_usd: float | None = None
    iterations: int | None = None
    log_path: Path | None = None
    outputs: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    stages: list[StageRunResult] = Field(default_factory=list)


class BaseRunner(ABC):
    """Common headless runner behavior shared by vendor adapters.

    Vendor subclasses provide the command and prompt; the base class handles
    output directory preparation, timeout enforcement, log capture, stale-output
    detection, and normalized `RunResult` construction.
    """

    vendor: str

    @abstractmethod
    def preflight(self, loop: LoopManifest, config: LoopcraftConfig) -> PreflightReport:
        """Check this vendor can satisfy the loop (binary, auth, env, skill)."""
        raise NotImplementedError

    @abstractmethod
    def build_command(self, loop: LoopManifest, ctx: RunContext) -> list[str]:
        """Build the vendor command argv for one loop invocation."""
        raise NotImplementedError

    def build_prompt(self, loop: LoopManifest, ctx: RunContext) -> str:
        """Assemble the vendor-neutral stdin prompt for one loop run.

        The prompt (skill + runtime context + I/O contract + verify/stop
        condition + budget) is deliberately identical across vendors — that
        portability is the whole point of the manifest/adapter split, so an
        existing loop runs unchanged on Codex, Claude, or Cursor. A vendor
        adapter may override this only if its runtime needs a different framing.

        Args:
            loop: The loop manifest.
            ctx: Run context (paths, resolved outputs).

        Returns:
            The full prompt text sent to the vendor runtime on stdin.
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
        # Prefer the explicit output bindings (declared -> in-worktree write
        # path); fall back to zipping the manifest outputs with resolved paths
        # for callers that set resolved_outputs directly (e.g. legacy/tests).
        if ctx.output_bindings:
            lines.append("- outputs (write exactly these absolute paths):")
            for binding in ctx.output_bindings:
                lines.append(f"  - {binding.declared} -> {binding.write_path}")
        elif loop.outputs:
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
        if ctx.extra_context:
            lines.append("")
            lines.append(ctx.extra_context)
        return "\n".join(lines) + "\n"

    def check_declared_capabilities(self, loop: LoopManifest, config: LoopcraftConfig) -> list[str]:
        """Return runtime-neutral problems for a loop's declared dependencies.

        Validates the skill/verify assets, tools on PATH, required env vars, and
        declared auth bundles/APIs via the shared capability registries. Vendor
        runners call this from ``preflight`` and add only their own checks (e.g.
        the vendor binary and model id).

        Args:
            loop: The loop manifest to check.
            config: Resolved control-plane config.

        Returns:
            A list of problem strings (empty when all declared capabilities pass).
        """
        return check_declared_capabilities(loop, config)

    def writable_roots(self, ctx: RunContext) -> list[str]:
        """Return extra write directories that lie *outside* the run worktree.

        Output directories inside the worktree need no grant (the worktree is the
        agent's writable workspace on every vendor), so only out-of-worktree
        parents are returned. Under the worktree-local output model this is
        normally empty — outputs are promoted to the ledger after the run — which
        is what lets Codex/Claude drop ``--add-dir`` and Cursor keep its sandbox.
        """
        workdir = ctx.workdir.resolve()
        roots = {
            str(parent)
            for p in ctx.resolved_outputs
            for parent in (p.parent.resolve(),)
            if parent != workdir and not is_lexically_under(parent, workdir)
        }
        return sorted(roots)

    def _load_source_text(self, ctx: RunContext, declared: str | None) -> str:
        """Read a source-relative markdown asset (skill/verify) safely.

        Returns the file's text, or an empty string when ``declared`` is unset,
        unsafe (rejected earlier by preflight), or missing.
        """
        if not declared:
            return ""
        try:
            path = ctx.config.resolve_source_path(declared)
        except SourcePathError:
            return ""
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def run(self, loop: LoopManifest, ctx: RunContext) -> RunResult:
        """Execute the loop headless in an isolated worktree; return the result."""
        prompt = self.build_prompt(loop, ctx)
        cmd = self.build_command(loop, ctx)

        ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
        for output in ctx.resolved_outputs:
            output.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, **ctx.env}
        pre_mtimes = {
            p: (p.stat().st_mtime_ns if p.exists() else None) for p in ctx.resolved_outputs
        }

        try:
            timeout_s = loop.budget.max_runtime_s
        except ValueError:
            timeout_s = None

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
                status=RunStatus.FAILED,
                exit_code=127,
                log_path=ctx.log_path,
                problems=[f"{self.vendor} command not found on PATH"],
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
                status=RunStatus.STALLED,
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

        # An output must be a real regular file. A symlink or directory at the
        # declared path is rejected (never followed), so an agent cannot redirect
        # promotion to read an arbitrary file (see review finding 10).
        unsafe = [p for p in ctx.resolved_outputs if p.exists() and not is_safe_regular_file(p)]
        unsafe_set = set(unsafe)
        missing = [p for p in ctx.resolved_outputs if not p.exists() and p not in unsafe_set]
        stale = [
            p
            for p in ctx.resolved_outputs
            if p not in unsafe_set
            and is_safe_regular_file(p)
            and pre_mtimes[p] is not None
            and p.stat(follow_symlinks=False).st_mtime_ns == pre_mtimes[p]
        ]
        stale_set = set(stale)
        produced = [
            str(p)
            for p in ctx.resolved_outputs
            if is_safe_regular_file(p) and p not in stale_set
        ]

        if completed.returncode != 0:
            problems = [f"{self.vendor} exited {completed.returncode}"]
        else:
            problems = [f"declared output not produced: {p}" for p in missing]
            problems += [f"declared output not refreshed this run: {p}" for p in stale]
            problems += [f"declared output is not a regular file (symlink/dir refused): {p}" for p in unsafe]
        status = (
            RunStatus.DONE
            if completed.returncode == 0 and not missing and not stale and not unsafe
            else RunStatus.FAILED
        )

        # The runner writes only inside the worktree and reports what it produced
        # there; promoting those files to the durable ledger is a control-plane
        # concern (see loopcraft.outputs.promote_outputs), so any adapter — not
        # just BaseRunner subclasses — gets ledger promotion for free.
        return RunResult(
            status=status,
            exit_code=completed.returncode,
            log_path=ctx.log_path,
            outputs=produced,
            problems=problems,
        )


Runner = BaseRunner
