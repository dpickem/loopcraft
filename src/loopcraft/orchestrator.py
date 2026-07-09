"""Multi-model (maker/checker) loop orchestration (M3.5).

A loop that declares ``roles`` runs its behavior as two or more agent
definitions on possibly-different providers. Two execution paths are supported:

- **inter-stage** (the portable default): each role runs as its own ordered
  adapter invocation and hands its output to the next stage through the run
  worktree / memory ledger. This works across any mix of Codex/Claude/Cursor
  with no cross-provider gateway, and every stage is independently logged.
- **intra-run**: the role agent definitions are compiled into the harness
  runtime's native sub-agent format and a single invocation spawns them as
  sub-agents. Cross-provider intra-run is native only on Cursor.

:func:`preflight_multi_model` validates a roles loop; :func:`run_multi_model`
executes it and returns a normalized :class:`RunResult`.
"""

from __future__ import annotations

from pathlib import Path

from loopcraft.agent_compiler import (
    AgentCompileError,
    CompiledAgent,
    compile_agent,
    write_compiled_agents,
)
from loopcraft.agents import AgentDefinition, AgentDefinitionError, load_agent_definition
from loopcraft.config import RUN_DATE_ENV, RUN_ID_ENV, LoopcraftConfig, SourcePathError
from loopcraft.manifest import ExecutionMode, Logic, LoopManifest, Role, Runtime, Vendor
from loopcraft.outputs import plan_output_bindings, promote_outputs
from loopcraft.runners import RunContext, available_vendors, get_runner
from loopcraft.runners.base import RunResult, RunStatus
from loopcraft.runners.capabilities import check_declared_capabilities

#: Runtime -> CLI binary that must be on PATH to run a role on that vendor.
_VENDOR_BINARIES: dict[str, str] = {
    Vendor.CODEX: "codex",
    Vendor.CLAUDE: "claude",
    Vendor.CURSOR: "cursor-agent",
}

#: STDOUT delimiters used by ``BaseRunner`` when it writes a stage log, so the
#: orchestrator can lift one stage's output as handoff context for the next.
_STDOUT_START = "--- STDOUT ---\n"
_STDOUT_END = "\n--- STDERR ---"

#: Cap on handoff text carried between stages, to bound the next stage's prompt.
_HANDOFF_MAX_CHARS = 20_000


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


def _stage_manifest(
    manifest: LoopManifest, role: Role, vendor: str, stage_outputs: list[str]
) -> LoopManifest:
    """Return a single-stage view of a roles loop for one role.

    The role's agent definition becomes the stage's ``logic.skill`` (so the
    shared prompt builder loads it as the stage instructions), the role's
    vendor/model become the stage runtime, and ``outputs`` are narrowed to the
    ones this stage owns. ``roles`` is cleared so the stage runs as an ordinary
    single-model invocation.
    """
    return manifest.model_copy(
        update={
            "runtime": Runtime(
                vendor=Vendor(vendor),
                model=role.model,
                reasoning_effort=manifest.runtime.reasoning_effort,
            ),
            "logic": Logic(skill=role.agent, verify=None),
            "outputs": stage_outputs,
            "roles": None,
        }
    )


def _stage_context(
    role_name: str,
    defn: AgentDefinition,
    handoff: str,
    prior_outputs: list[Path],
) -> str:
    """Build the extra prompt context handed to one inter-stage stage."""
    parts = [f"## Multi-model stage: {role_name}"]
    if defn.readonly:
        parts.append(
            "You are a READ-ONLY reviewer stage: do not modify source code or the "
            "maker's outputs, and do not run mutating commands. You may write only "
            "your own declared review output(s) listed above."
        )
    if prior_outputs:
        parts.append("Prior stage wrote these ledger outputs — read them to continue/review:")
        parts += [f"  - {path}" for path in prior_outputs]
    if handoff:
        parts.append("")
        parts.append("## Prior stage output")
        parts.append(handoff)
    return "\n".join(parts)


def _subagent_context(role_summaries: list[tuple[str, str, str | None, bool]]) -> str:
    """Describe the compiled sub-agents available to an intra-run harness."""
    parts = [
        "## Multi-model roles (intra-run)",
        "This run has the following role sub-agents compiled into the workspace; "
        "delegate each role's work to its sub-agent and compose the result:",
    ]
    for name, vendor, model, readonly in role_summaries:
        flags = " (read-only)" if readonly else ""
        model_note = f", model={model}" if model else ""
        parts.append(f"  - {name}: vendor={vendor}{model_note}{flags}")
    return "\n".join(parts)


def _join_context(*chunks: str) -> str:
    """Join non-empty prompt chunks with blank-line separators."""
    return "\n\n".join(chunk for chunk in chunks if chunk)


def _write_pipeline_log(
    log_path: Path,
    manifest: LoopManifest,
    stages: list[tuple[str, str, str | None, Path, str]],
) -> None:
    """Write an aggregate log summarizing the inter-stage pipeline."""
    lines = [f"# Multi-model inter-stage pipeline: {manifest.id}", ""]
    for name, vendor, model, stage_log, status in stages:
        lines.append(f"## stage: {name}  vendor={vendor}  model={model or '(default)'}  status={status}")
        lines.append(f"log: {stage_log}")
        lines.append("")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(lines), encoding="utf-8")


def _run_stamps(ctx: RunContext) -> tuple[str, str]:
    """Return the (run_id, date) the control plane handed down via ``ctx.env``."""
    return ctx.env.get(RUN_ID_ENV, "<run_id>"), ctx.env.get(RUN_DATE_ENV, "<date>")


def _stage_declared_outputs(manifest: LoopManifest, role: Role, readonly: bool) -> list[str]:
    """Return the declared outputs a stage owns.

    A read-only role owns only its own declared ``outputs`` (e.g. review notes).
    A maker owns its own ``outputs`` if declared, otherwise the loop's top-level
    ``outputs``.
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


def _run_inter_stage(
    manifest: LoopManifest,
    config: LoopcraftConfig,
    ctx: RunContext,
    default_vendor: str,
) -> RunResult:
    """Run each role as an ordered stage, handing output through the ledger."""
    run_id, date = _run_stamps(ctx)
    # The maker's promoted ledger destinations, handed to the reviewer to read.
    maker_ledger = [
        config.resolve_state_template(out, run_id=run_id, date=date) for out in manifest.outputs
    ]
    problems: list[str] = []
    produced: list[str] = []
    statuses: list[str] = []
    stage_records: list[tuple[str, str, str | None, Path, str]] = []
    handoff = ""

    for index, (name, role) in enumerate(manifest.ordered_roles()):
        vendor = manifest.role_vendor(role, default_vendor)
        try:
            runner = get_runner(vendor)
        except ValueError as exc:
            problems.append(f"[{name}] {exc}")
            statuses.append(RunStatus.FAILED)
            break
        try:
            defn = _load_role_definition(config, role)
        except AgentDefinitionError as exc:
            problems.append(f"[{name}] {exc}")
            statuses.append(RunStatus.FAILED)
            break

        # A role writes only inside the worktree (bound outputs are promoted to
        # the ledger afterwards). A read-only reviewer owns only its own outputs
        # and is handed the maker's ledger paths to inspect, not to edit.
        stage_declared = _stage_declared_outputs(manifest, role, defn.readonly)
        bindings = plan_output_bindings(config, ctx.workdir, stage_declared, run_id=run_id, date=date)
        prior_outputs = maker_ledger if defn.readonly else []
        stage_ctx = RunContext(
            config=config,
            workdir=ctx.workdir,
            log_path=ctx.workdir / f"stage-{index + 1}-{name}.log",
            resolved_outputs=[binding.write_path for binding in bindings],
            output_bindings=bindings,
            env=ctx.env,
            extra_context=_stage_context(name, defn, handoff, prior_outputs),
        )
        result = runner.run(_stage_manifest(manifest, role, vendor, stage_declared), stage_ctx)
        # Promote this stage's worktree outputs to the ledger before the next
        # stage runs, so a reviewer reads the maker's promoted ledger files.
        promoted = promote_outputs(bindings)

        statuses.append(result.status)
        produced += [str(path) for path in promoted]
        problems += [f"[{name}] {problem}" for problem in result.problems]
        stage_records.append((name, vendor, role.model, stage_ctx.log_path, result.status))
        handoff = _read_stage_output(stage_ctx.log_path)
        # If a maker stage fails there is nothing sound to review; stop early.
        if result.status != RunStatus.DONE and not defn.readonly:
            break

    _write_pipeline_log(ctx.log_path, manifest, stage_records)
    ok = bool(statuses) and all(status == RunStatus.DONE for status in statuses)
    return RunResult(
        status=RunStatus.DONE if ok else RunStatus.FAILED,
        log_path=ctx.log_path,
        outputs=sorted(set(produced)),
        problems=problems,
    )


def _run_intra_run(
    manifest: LoopManifest,
    config: LoopcraftConfig,
    ctx: RunContext,
    default_vendor: str,
) -> RunResult:
    """Compile roles into harness sub-agents and run a single invocation."""
    harness_vendor = manifest.effective_vendor(default_vendor)
    compiled: list[CompiledAgent] = []
    summaries: list[tuple[str, str, str | None, bool]] = []
    for name, role in manifest.ordered_roles():
        try:
            defn = _load_role_definition(config, role)
        except AgentDefinitionError as exc:
            return RunResult(status=RunStatus.FAILED, log_path=ctx.log_path, problems=[f"[{name}] {exc}"])
        try:
            compiled.append(compile_agent(defn, harness_vendor, role.model))
        except AgentCompileError as exc:
            return RunResult(status=RunStatus.FAILED, log_path=ctx.log_path, problems=[f"[{name}] {exc}"])
        summaries.append((name, harness_vendor, role.model, defn.readonly))

    write_compiled_agents(ctx.workdir, compiled)
    runner = get_runner(harness_vendor)
    # The single harness writes the loop's outputs plus every role's own outputs,
    # staged in the worktree and promoted to the ledger after the run.
    run_id, date = _run_stamps(ctx)
    declared = list(manifest.outputs) + [
        out for _, role in manifest.ordered_roles() for out in role.outputs
    ]
    bindings = plan_output_bindings(config, ctx.workdir, declared, run_id=run_id, date=date)
    harness_ctx = ctx.model_copy(
        update={
            "resolved_outputs": [binding.write_path for binding in bindings],
            "output_bindings": bindings,
            "extra_context": _join_context(ctx.extra_context, _subagent_context(summaries)),
        }
    )
    result = runner.run(manifest.model_copy(update={"roles": None}), harness_ctx)
    promoted = promote_outputs(bindings)
    return result.model_copy(update={"outputs": [str(path) for path in promoted]})


def preflight_multi_model(
    manifest: LoopManifest, config: LoopcraftConfig, default_vendor: str
) -> list[str]:
    """Validate a multi-model loop can run before executing it.

    Checks the shared declared dependencies (tools/auth/env/apis/content), and
    per role: a runtime adapter exists and its binary is on PATH, and the role's
    agent definition resolves, exists, and parses. For an intra-run loop whose
    roles span more than one vendor, the harness must be Cursor.

    Args:
        manifest: The multi-model loop manifest (must declare ``roles``).
        config: Resolved control-plane config.
        default_vendor: The global default vendor for inheritance.

    Returns:
        A list of problem strings (empty when the loop is ready to run).
    """
    if not manifest.roles:
        return []

    problems = check_declared_capabilities(manifest, config)
    harness_vendor = manifest.effective_vendor(default_vendor)
    vendors_seen: set[str] = set()

    for name, role in manifest.ordered_roles():
        vendor = manifest.role_vendor(role, default_vendor)
        vendors_seen.add(vendor)
        if vendor not in available_vendors():
            problems.append(f"role '{name}': no runtime adapter for vendor '{vendor}'")
        else:
            binary = _VENDOR_BINARIES.get(vendor, vendor)
            if config.which(binary) is None:
                problems.append(f"role '{name}': {binary} not found on PATH (vendor '{vendor}')")
        try:
            path = config.resolve_source_path(role.agent)
        except SourcePathError as exc:
            problems.append(f"role '{name}': {exc}")
            continue
        if not path.is_file():
            problems.append(f"role '{name}': agent definition not found: {role.agent}")
            continue
        try:
            load_agent_definition(path)
        except AgentDefinitionError as exc:
            problems.append(f"role '{name}': {exc}")

    if (
        manifest.execution == ExecutionMode.INTRA_RUN
        and len(vendors_seen) > 1
        and harness_vendor != Vendor.CURSOR
    ):
        problems.append(
            f"intra-run cross-provider roles ({sorted(vendors_seen)}) require a Cursor "
            f"harness; harness vendor is '{harness_vendor}' — use execution: inter-stage "
            "or set runtime.vendor: cursor"
        )
    return problems


def run_multi_model(
    manifest: LoopManifest,
    config: LoopcraftConfig,
    ctx: RunContext,
    default_vendor: str,
) -> RunResult:
    """Execute a multi-model loop via its declared execution mode.

    Args:
        manifest: The loop manifest (must declare ``roles``).
        config: Resolved control-plane config.
        ctx: The run context built by the control plane (worktree, resolved
            outputs, env, aggregate log path).
        default_vendor: The global default vendor for role inheritance.

    Returns:
        A normalized :class:`RunResult` for the whole multi-model run.
    """
    if manifest.execution == ExecutionMode.INTRA_RUN:
        return _run_intra_run(manifest, config, ctx, default_vendor)
    return _run_inter_stage(manifest, config, ctx, default_vendor)
