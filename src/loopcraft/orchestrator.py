"""Multi-model (maker/checker) loop orchestration (M3.5).

A loop that declares ``roles`` runs its behavior as two or more agent
definitions on possibly-different providers. Two execution paths are supported:

- **inter-stage** (the portable default): each role runs as its own ordered
  adapter invocation and hands a structured artifact to the next stage through
  the run worktree / memory ledger. Works across any mix of Codex/Claude/Cursor
  with no gateway; every stage is independently logged and costed.
- **intra-run**: the role agent definitions are compiled into the harness
  runtime's native sub-agent format and a single invocation spawns them as
  sub-agents. Cross-provider intra-run is native only on Cursor.

A single normalized :class:`ExecutionPlan` is built up front (resolving the
``--vendor`` override, per-role vendor/model, read-only policy, and output
ownership) and drives dry-run display, preflight, and execution so the paths
cannot drift.

Scope note (M3.5): the inter-stage handoff is a **structured artifact** (prior
status, promoted output paths + content digests, and captured stdout), not a Git
diff. Running a code maker/checker against a real Git worktree/diff is deferred
with the L4 build loop; see the design doc.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from loopcraft.agent_compiler import AgentCompileError, CompiledAgent, compile_agent, write_compiled_agents
from loopcraft.agents import AgentDefinition, AgentDefinitionError, load_agent_definition
from loopcraft.config import RUN_DATE_ENV, RUN_ID_ENV, LoopcraftConfig, SourcePathError
from loopcraft.manifest import Budget, ExecutionMode, Logic, LoopManifest, Role, Runtime, Vendor
from loopcraft.outputs import OutputBinding, is_safe_regular_file, plan_output_bindings, promote_outputs
from loopcraft.paths import assert_under
from loopcraft.role_tools import role_tool_problems
from loopcraft.runners import RunContext, available_vendors, get_runner
from loopcraft.runners.base import RunResult, RunStatus
from loopcraft.runners.capabilities import check_declared_capabilities

#: Runtime -> CLI binary that must be on PATH to run a role/harness on it.
_VENDOR_BINARIES: dict[str, str] = {
    Vendor.CODEX: "codex",
    Vendor.CLAUDE: "claude",
    Vendor.CURSOR: "cursor-agent",
}

#: STDOUT delimiters ``BaseRunner`` writes into a stage log, so the orchestrator
#: can lift one stage's output as handoff context for the next.
_STDOUT_START = "--- STDOUT ---\n"
_STDOUT_END = "\n--- STDERR ---"

#: Cap on handoff stdout carried between stages, to bound the next stage's prompt.
_HANDOFF_MAX_CHARS = 20_000

#: Matches an explicit reviewer verdict line (e.g. ``Verdict: PASS``).
_VERDICT_RE = re.compile(r"(?im)^\s*(?:\*\*)?verdict(?:\*\*)?\s*[:\-]?\s*(?:\*\*)?\s*(PASS|FAIL)\b")


@dataclass
class RoleStage:
    """One resolved role in an execution plan."""

    name: str
    vendor: str
    model: str | None
    agent: str
    defn: AgentDefinition
    readonly: bool
    owned_outputs: list[str]


@dataclass
class ExecutionPlan:
    """Normalized plan for a multi-model loop, shared by preflight and run."""

    mode: ExecutionMode
    harness_vendor: str
    stages: list[RoleStage]
    maker_outputs: list[str] = field(default_factory=list)


def _resolved_default(manifest: LoopManifest, default_vendor: str, override_vendor: str | None) -> str:
    """Resolve the vendor used for inheritance: override → runtime → global."""
    return override_vendor or manifest.runtime.vendor or default_vendor


def build_execution_plan(
    manifest: LoopManifest,
    config: LoopcraftConfig,
    default_vendor: str,
    *,
    override_vendor: str | None = None,
) -> tuple[ExecutionPlan, list[str]]:
    """Build one normalized execution plan and collect any planning problems.

    Resolves each role's vendor (honoring a ``--vendor`` override for inherited
    roles), loads its agent definition, classifies its declared tools, and
    computes output ownership. Problems (unloadable agent, unmappable/mismatched
    tools, ambiguous output ownership, cross-provider intra-run without a Cursor
    harness) are returned rather than raised so preflight can report them all.

    Returns:
        The plan (built best-effort) and a list of problem strings.
    """
    problems: list[str] = []
    base_vendor = _resolved_default(manifest, default_vendor, override_vendor)
    stages: list[RoleStage] = []
    owned_seen: dict[str, str] = {}

    for name, role in manifest.ordered_roles():
        vendor = role.vendor or base_vendor
        try:
            defn = _load_role_definition(config, role)
        except AgentDefinitionError as exc:
            problems.append(f"role '{name}': {exc}")
            # Fall back to a placeholder so the plan still lists the stage.
            defn = AgentDefinition(name=name)
        problems += role_tool_problems(name, defn.tools, readonly=defn.readonly)
        owned = _owned_outputs(manifest, role, defn.readonly)
        for declared in owned:
            key = declared.strip()
            if key in owned_seen and owned_seen[key] != name:
                problems.append(
                    f"role '{name}': output '{declared}' is also owned by role '{owned_seen[key]}'"
                )
            owned_seen[key] = name
        stages.append(
            RoleStage(
                name=name,
                vendor=vendor,
                model=role.model,
                agent=role.agent,
                defn=defn,
                readonly=defn.readonly,
                owned_outputs=owned,
            )
        )

    maker_outputs: list[str] = []
    for stage in stages:
        if not stage.readonly:
            for declared in stage.owned_outputs:
                if declared not in maker_outputs:
                    maker_outputs.append(declared)

    plan = ExecutionPlan(
        mode=manifest.execution,
        harness_vendor=base_vendor,
        stages=stages,
        maker_outputs=maker_outputs,
    )

    if plan.mode == ExecutionMode.INTRA_RUN:
        vendors = {stage.vendor for stage in stages}
        if len(vendors) > 1 and plan.harness_vendor != Vendor.CURSOR:
            problems.append(
                f"intra-run cross-provider roles ({sorted(vendors)}) require a Cursor "
                f"harness; harness vendor is '{plan.harness_vendor}' — use execution: "
                "inter-stage or set runtime.vendor: cursor"
            )
    return plan, problems


def _owned_outputs(manifest: LoopManifest, role: Role, readonly: bool) -> list[str]:
    """Return the declared outputs a role owns (single ownership rule).

    A read-only role owns only its own declared ``outputs``; a maker owns its
    own ``outputs`` if declared, otherwise the loop's top-level ``outputs``.
    """
    if readonly:
        return list(role.outputs)
    return list(role.outputs) if role.outputs else list(manifest.outputs)


def _load_role_definition(config: LoopcraftConfig, role: Role) -> AgentDefinition:
    """Resolve and parse a role's agent definition from the source tree.

    Raises:
        AgentDefinitionError: If the path escapes the source tree or the file
            cannot be read/parsed.
    """
    try:
        path = config.resolve_source_path(role.agent)
    except SourcePathError as exc:
        raise AgentDefinitionError(str(exc)) from exc
    return load_agent_definition(path)


def _run_stamps(ctx: RunContext) -> tuple[str, str]:
    """Return the (run_id, date) the control plane handed down via ``ctx.env``."""
    return ctx.env.get(RUN_ID_ENV, "<run_id>"), ctx.env.get(RUN_DATE_ENV, "<date>")


def _extract_stdout(log_text: str) -> str:
    """Return the STDOUT section of a stage log, truncated to the handoff cap."""
    start = log_text.find(_STDOUT_START)
    if start == -1:
        return ""
    start += len(_STDOUT_START)
    end = log_text.find(_STDOUT_END, start)
    body = (log_text[start:] if end == -1 else log_text[start:end]).strip()
    if len(body) > _HANDOFF_MAX_CHARS:
        return body[:_HANDOFF_MAX_CHARS] + "\n… (handoff truncated)"
    return body


def _read_stage_output(log_path: Path) -> str:
    """Best-effort read of a completed stage's stdout for the next stage."""
    try:
        return _extract_stdout(log_path.read_text(encoding="utf-8"))
    except OSError:
        return ""


def _digest(path: Path) -> str | None:
    """Return a short content digest for a promoted output, or None."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


@dataclass
class StageHandoff:
    """Structured artifact handed from one inter-stage stage to the next."""

    role: str
    status: str
    outputs: list[tuple[str, str | None]]  # (ledger path, content digest)
    stdout: str

    def render(self) -> str:
        """Render the handoff as prompt context for the next stage."""
        lines = [f"## Prior stage: {self.role} (status: {self.status})"]
        if self.outputs:
            lines.append("Outputs it produced in the ledger (read to continue/review):")
            lines += [f"  - {path}  (sha256:{digest or 'n/a'})" for path, digest in self.outputs]
        if self.stdout:
            lines.append("")
            lines.append("Prior stage stdout:")
            lines.append(self.stdout)
        return "\n".join(lines)


def _stage_prompt_context(stage: RoleStage, handoff: StageHandoff | None) -> str:
    """Assemble the extra prompt context for one inter-stage stage."""
    parts = [f"## Role: {stage.name}", stage.defn.prompt_body()]
    if handoff is not None:
        parts.append("")
        parts.append(handoff.render())
    return "\n".join(parts)


def _stage_manifest(manifest: LoopManifest, stage: RoleStage, budget: Budget, outputs: list[str]) -> LoopManifest:
    """Return a single-stage single-model view of a roles loop for one role.

    The role's behavior is supplied via the run context's ``extra_context`` (so
    ``verify`` and read-only policy travel with it), ``logic`` is cleared, the
    role vendor/model become the runtime, ``outputs`` are narrowed to the ones
    this stage owns, and ``budget`` carries this stage's remaining allowance.
    """
    return manifest.model_copy(
        update={
            "runtime": Runtime(
                vendor=Vendor(stage.vendor),
                model=stage.model,
                reasoning_effort=manifest.runtime.reasoning_effort,
            ),
            "logic": Logic(skill=None, verify=None),
            "outputs": outputs,
            "budget": budget,
            "roles": None,
        }
    )


def _subagent_context(stages: list[RoleStage]) -> str:
    """Describe the compiled sub-agents available to an intra-run harness."""
    parts = [
        "## Multi-model roles (intra-run)",
        "This run has the following role sub-agents compiled into the workspace; "
        "delegate each role's work to its sub-agent and compose the result:",
    ]
    for stage in stages:
        flags = " (read-only)" if stage.readonly else ""
        model_note = f", model={stage.model}" if stage.model else ""
        parts.append(f"  - {stage.name}: vendor={stage.vendor}{model_note}{flags}")
    return "\n".join(parts)


def _join_context(*chunks: str) -> str:
    """Join non-empty prompt chunks with blank-line separators."""
    return "\n\n".join(chunk for chunk in chunks if chunk)


def _hash_tree(root: Path, exclude: set[Path]) -> dict[str, str]:
    """Hash every regular file under ``root`` except paths in ``exclude``."""
    result: dict[str, str] = {}
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        resolved = path.resolve()
        if resolved in exclude:
            continue
        try:
            result[str(resolved)] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return result


def _protected_violations(before: dict[str, str], root: Path, exclude: set[Path]) -> list[str]:
    """Return problems if any protected (pre-existing) file was changed/removed.

    A read-only reviewer may create its own review output and scratch files, but
    must not modify or delete files that existed before it ran. This is the
    control-plane enforcement of the read-only contract (review finding 3).
    """
    after = _hash_tree(root, exclude)
    problems: list[str] = []
    for path, digest in before.items():
        if path not in after:
            problems.append(f"read-only role deleted a protected file: {path}")
        elif after[path] != digest:
            problems.append(f"read-only role modified a protected file: {path}")
    return problems


def _parse_verdict(text: str) -> str | None:
    """Return an explicit PASS/FAIL verdict from reviewer text, or None."""
    match = _VERDICT_RE.search(text)
    return match.group(1).upper() if match else None


def _aggregate_status(stage_statuses: list[str], reviewer_failed: bool) -> str:
    """Combine stage statuses into one normalized pipeline status.

    Most-severe wins: a failed stage (or a reviewer FAIL verdict) dominates,
    then stalled (budget/timeout), then needs-approval; otherwise done.
    """
    if not stage_statuses:
        return RunStatus.FAILED
    if reviewer_failed or any(s == RunStatus.FAILED for s in stage_statuses):
        return RunStatus.FAILED
    if any(s == RunStatus.STALLED for s in stage_statuses):
        return RunStatus.STALLED
    if any(s == RunStatus.NEEDS_APPROVAL for s in stage_statuses):
        return RunStatus.NEEDS_APPROVAL
    return RunStatus.DONE


def _stage_budget(base: Budget, remaining_s: int | None) -> Budget:
    """Return a per-stage budget capping runtime to the remaining allowance."""
    if remaining_s is None:
        return base
    return base.model_copy(update={"max_runtime": f"{remaining_s}s"})


def _safe_stage_log(workdir: Path, index: int, name: str) -> Path:
    """Return a contained per-stage log path (role names are pre-validated)."""
    log_path = (workdir / f"stage-{index}-{name}.log").resolve()
    assert_under(workdir.resolve(), log_path, label="stage log")
    return log_path


def _run_inter_stage(
    manifest: LoopManifest,
    config: LoopcraftConfig,
    ctx: RunContext,
    plan: ExecutionPlan,
) -> RunResult:
    """Run each role as an ordered stage, handing a structured artifact along."""
    run_id, date = _run_stamps(ctx)
    workdir = ctx.workdir
    total_runtime_s = _safe_max_runtime(manifest.budget)

    problems: list[str] = []
    produced: list[str] = []
    stage_records: list[dict] = []
    stage_statuses: list[str] = []
    reviewer_failed = False
    handoff: StageHandoff | None = None
    elapsed_s = 0.0

    for index, stage in enumerate(plan.stages, start=1):
        # Enforce the aggregate runtime budget across the whole pipeline.
        remaining_s = None if total_runtime_s is None else int(total_runtime_s - elapsed_s)
        if remaining_s is not None and remaining_s <= 0:
            problems.append(
                f"[{stage.name}] aggregate budget.max_runtime exhausted before stage started"
            )
            stage_statuses.append(RunStatus.STALLED)
            break

        try:
            runner = get_runner(stage.vendor)
        except ValueError as exc:
            problems.append(f"[{stage.name}] {exc}")
            stage_statuses.append(RunStatus.FAILED)
            break

        bindings = plan_output_bindings(config, workdir, stage.owned_outputs, run_id=run_id, date=date)
        stage_log = _safe_stage_log(workdir, index, stage.name)
        stage_ctx = RunContext(
            config=config,
            workdir=workdir,
            log_path=stage_log,
            resolved_outputs=[binding.write_path for binding in bindings],
            output_bindings=bindings,
            env=ctx.env,
            extra_context=_stage_prompt_context(stage, handoff),
        )
        stage_manifest = _stage_manifest(
            manifest, stage, _stage_budget(manifest.budget, remaining_s), stage.owned_outputs
        )

        # For a read-only role, snapshot every pre-existing worktree file (except
        # its own writable outputs and log) so we can prove it mutated nothing.
        protected_before: dict[str, str] = {}
        exclude = {p.resolve() for p in stage_ctx.resolved_outputs} | {stage_log.resolve()}
        if stage.readonly:
            protected_before = _hash_tree(workdir, exclude)

        stage_start = time.perf_counter()
        result = runner.run(stage_manifest, stage_ctx)
        elapsed_s += time.perf_counter() - stage_start

        stage_problems = [f"[{stage.name}] {problem}" for problem in result.problems]
        stage_status = result.status

        # Enforce the read-only boundary: a checker that touched protected files
        # fails the run and its (rejected) outputs are not promoted.
        readonly_ok = True
        if stage.readonly:
            violations = _protected_violations(protected_before, workdir, exclude)
            if violations:
                readonly_ok = False
                stage_status = RunStatus.FAILED
                stage_problems += [f"[{stage.name}] {v}" for v in violations]

        # Promote only a stage that fully succeeded and respected its contract.
        promoted: list[Path] = []
        if stage_status == RunStatus.DONE and readonly_ok:
            promoted = promote_outputs(bindings, workdir=workdir)
            produced += [str(path) for path in promoted]

        # A read-only reviewer must emit an explicit verdict; a FAIL rejects.
        verdict = None
        if stage.readonly:
            verdict = _stage_verdict(bindings, stage_log)
            if verdict == "FAIL":
                reviewer_failed = True
            elif verdict is None and stage.defn.verify:
                stage_problems.append(f"[{stage.name}] reviewer did not emit an explicit PASS/FAIL verdict")

        problems += stage_problems
        stage_statuses.append(stage_status)
        stage_records.append(
            {
                "role": stage.name,
                "vendor": stage.vendor,
                "model": stage.model,
                "status": stage_status,
                "verdict": verdict,
                "exit_code": result.exit_code,
                "tokens": result.tokens,
                "cost_usd": result.cost_usd,
                "log": str(stage_log),
            }
        )
        handoff = StageHandoff(
            role=stage.name,
            status=stage_status,
            outputs=[(str(p), _digest(p)) for p in promoted],
            stdout=_read_stage_output(stage_log),
        )
        # A failed maker leaves nothing sound to review; stop the pipeline.
        if stage_status != RunStatus.DONE and not stage.readonly:
            break

    _write_pipeline_log(ctx.log_path, manifest, stage_records)
    status = _aggregate_status(stage_statuses, reviewer_failed)
    return RunResult(
        status=status,
        log_path=ctx.log_path,
        outputs=sorted(set(produced)),
        problems=problems,
        tokens=_sum_optional(r["tokens"] for r in stage_records),
        cost_usd=_sum_optional(r["cost_usd"] for r in stage_records),
        stages=stage_records,
    )


def _run_intra_run(
    manifest: LoopManifest,
    config: LoopcraftConfig,
    ctx: RunContext,
    plan: ExecutionPlan,
) -> RunResult:
    """Compile roles into harness sub-agents and run a single invocation."""
    harness_vendor = plan.harness_vendor
    compiled: list[CompiledAgent] = []
    for stage in plan.stages:
        try:
            compiled.append(compile_agent(stage.defn, harness_vendor, stage.model, name=stage.name))
        except AgentCompileError as exc:
            return RunResult(status=RunStatus.FAILED, log_path=ctx.log_path, problems=[f"[{stage.name}] {exc}"])
    write_compiled_agents(ctx.workdir, compiled)

    runner = get_runner(harness_vendor)
    run_id, date = _run_stamps(ctx)
    declared = list(manifest.outputs) + [
        out for stage in plan.stages for out in stage.owned_outputs if out not in manifest.outputs
    ]
    bindings = plan_output_bindings(config, ctx.workdir, declared, run_id=run_id, date=date)
    harness_ctx = ctx.model_copy(
        update={
            "resolved_outputs": [binding.write_path for binding in bindings],
            "output_bindings": bindings,
            "extra_context": _join_context(ctx.extra_context, _subagent_context(plan.stages)),
        }
    )
    result = runner.run(manifest.model_copy(update={"roles": None}), harness_ctx)
    # Promote only when the harness fully succeeded (review finding 5).
    if result.status == RunStatus.DONE:
        promoted = promote_outputs(bindings, workdir=ctx.workdir)
        return result.model_copy(update={"outputs": [str(path) for path in promoted]})
    return result.model_copy(update={"outputs": []})


def _stage_verdict(bindings: list[OutputBinding], stage_log: Path) -> str | None:
    """Parse an explicit PASS/FAIL verdict from a reviewer's output or log."""
    for binding in bindings:
        if is_safe_regular_file(binding.write_path):
            verdict = _parse_verdict(binding.write_path.read_text(encoding="utf-8", errors="replace"))
            if verdict:
                return verdict
    return _parse_verdict(_read_stage_output(stage_log))


def _safe_max_runtime(budget: Budget) -> int | None:
    """Return ``budget.max_runtime_s`` or None when unset/unparseable."""
    try:
        return budget.max_runtime_s
    except ValueError:
        return None


def _sum_optional(values) -> int | float | None:  # noqa: ANN001 — mixed int/float/None stream
    """Sum a stream of optional numbers, returning None when all are None."""
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _write_pipeline_log(log_path: Path, manifest: LoopManifest, stages: list[dict]) -> None:
    """Write an aggregate log summarizing the inter-stage pipeline + metrics."""
    lines = [f"# Multi-model inter-stage pipeline: {manifest.id}", ""]
    for record in stages:
        lines.append(
            f"## stage: {record['role']}  vendor={record['vendor']}  "
            f"model={record['model'] or '(default)'}  status={record['status']}"
        )
        if record.get("verdict"):
            lines.append(f"verdict: {record['verdict']}")
        lines.append(
            f"exit_code={record['exit_code']} tokens={record['tokens']} cost_usd={record['cost_usd']}"
        )
        lines.append(f"log: {record['log']}")
        lines.append("")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(lines), encoding="utf-8")


def preflight_multi_model(
    manifest: LoopManifest,
    config: LoopcraftConfig,
    default_vendor: str,
    *,
    override_vendor: str | None = None,
) -> list[str]:
    """Validate a multi-model loop can run before executing it.

    Builds the normalized plan (so preflight and execution agree), checks the
    shared declared dependencies, and then validates per execution mode:

    - **inter-stage**: each role's *actual* adapter preflights its single-stage
      manifest (binary, model shape, capabilities).
    - **intra-run**: the harness adapter/binary is checked once and every role is
      compile-validated for the harness format.

    Args:
        manifest: The multi-model loop manifest (must declare ``roles``).
        config: Resolved control-plane config.
        default_vendor: The global default vendor for inheritance.
        override_vendor: A one-off ``--vendor`` override, if any.

    Returns:
        A list of problem strings (empty when the loop is ready to run).
    """
    if not manifest.roles:
        return []

    plan, problems = build_execution_plan(
        manifest, config, default_vendor, override_vendor=override_vendor
    )
    # Shared declared dependencies (roles present -> skill optional).
    problems += check_declared_capabilities(manifest, config)

    if plan.mode == ExecutionMode.INTRA_RUN:
        problems += _preflight_intra_run(plan, config)
    else:
        problems += _preflight_inter_stage(manifest, plan, config)
    return problems


def _preflight_binary(vendor: str, config: LoopcraftConfig, label: str) -> list[str]:
    """Check a vendor's adapter is registered and its CLI is on PATH."""
    if vendor not in available_vendors():
        return [f"{label}: no runtime adapter for vendor '{vendor}'"]
    binary = _VENDOR_BINARIES.get(vendor, vendor)
    if config.which(binary) is None:
        return [f"{label}: {binary} not found on PATH (vendor '{vendor}')"]
    return []


def _preflight_intra_run(plan: ExecutionPlan, config: LoopcraftConfig) -> list[str]:
    """Preflight the harness once and compile-validate every role for it."""
    problems = _preflight_binary(plan.harness_vendor, config, "intra-run harness")
    for stage in plan.stages:
        try:
            compile_agent(stage.defn, plan.harness_vendor, stage.model, name=stage.name)
        except AgentCompileError as exc:
            problems.append(f"role '{stage.name}': {exc}")
    return problems


def _preflight_inter_stage(
    manifest: LoopManifest, plan: ExecutionPlan, config: LoopcraftConfig
) -> list[str]:
    """Preflight each role's actual adapter using its single-stage manifest."""
    problems: list[str] = []
    for stage in plan.stages:
        problems += _preflight_binary(stage.vendor, config, f"role '{stage.name}'")
        if stage.vendor not in available_vendors():
            continue
        # Run the role's own adapter preflight so model-shape and capability
        # checks match execution exactly (same stage manifest is used to run).
        runner = get_runner(stage.vendor)
        stage_manifest = _stage_manifest(manifest, stage, manifest.budget, stage.owned_outputs)
        # The stage manifest carries the agent behavior via extra_context at run
        # time; for preflight, point logic.skill at the agent file so the shared
        # asset check confirms it resolves.
        stage_manifest = stage_manifest.model_copy(update={"logic": Logic(skill=stage.agent, verify=None)})
        report = runner.preflight(stage_manifest, config)
        problems += [f"role '{stage.name}': {p}" for p in report.problems]
    return problems


def run_multi_model(
    manifest: LoopManifest,
    config: LoopcraftConfig,
    ctx: RunContext,
    default_vendor: str,
    *,
    override_vendor: str | None = None,
) -> RunResult:
    """Execute a multi-model loop via its declared execution mode.

    Args:
        manifest: The loop manifest (must declare ``roles``).
        config: Resolved control-plane config.
        ctx: The run context built by the control plane (worktree, env, log).
        default_vendor: The global default vendor for role inheritance.
        override_vendor: A one-off ``--vendor`` override, if any.

    Returns:
        A normalized :class:`RunResult` for the whole multi-model run.
    """
    plan, _ = build_execution_plan(manifest, config, default_vendor, override_vendor=override_vendor)
    if plan.mode == ExecutionMode.INTRA_RUN:
        return _run_intra_run(manifest, config, ctx, plan)
    return _run_inter_stage(manifest, config, ctx, plan)
