"""``loopctl`` control-plane CLI.

Dispatches loop lifecycle commands (run/validate/apply/list/status/logs/deps)
against a resolved :class:`LoopcraftConfig`. Every command supports a consistent,
agent-friendly ``--json`` envelope (``{command, ok, exit_code, data}``) in
addition to human-readable text output.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import traceback
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from loopcraft.cli_output import CommandOutcome, emit as _emit, render_table
from loopcraft.config import (
    ACTIVE_LOOP_ENV,
    RUN_DATE_ENV,
    RUN_ID_ENV,
    ExitCode,
    LoopcraftConfig,
    SystemdScope,
)
from loopcraft.deploy import (
    DeploymentPlan,
    environment_file_health,
    install_units,
    plan_deployment,
    systemd_unit_dir,
    write_units,
)
from loopcraft.env import load_dotenv
from loopcraft.manifest import CadenceType, LoopManifest, ManifestError, find_manifest, load_all
from loopcraft.paths import assert_under
from loopcraft.runners import RunContext, get_runner
from loopcraft.runners.base import PreflightReport, RunStatus
from loopcraft.runners.capabilities import (
    API_GUIDANCE,
    API_PROBES,
    AUTH_GUIDANCE,
    AUTH_PROBES,
    content_assets,
)
from loopcraft.store import RunRecord, Store
from loopcraft.worktree import (
    StagingError,
    prune_loop_worktrees,
    stage_loop_assets,
    worktree_dir,
    worktrees_root,
)


class FailurePhase(StrEnum):
    """Closed vocabulary of pre/mid-execution phases a run can fail in."""

    PREFLIGHT = "preflight"
    STAGING = "staging"
    EXECUTION = "execution"


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, load config, and dispatch to the selected command."""
    parser = argparse.ArgumentParser(prog="loopctl", description="Loopcraft control plane.")
    parser.add_argument("--source", help="Source tree root (default: auto-detect / LOOPCRAFT_SOURCE).")
    parser.add_argument("--json", action="store_true", help="Emit a structured JSON result envelope.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run one loop now, headless.")
    p_run.add_argument("loop", help="Loop id (filename stem in loops/).")
    p_run.add_argument("--vendor", help="Override the runtime vendor for this run.")
    p_run.add_argument("--dry-run", action="store_true", help="Preflight + show the invocation; do not execute.")

    p_validate = sub.add_parser("validate", help="Validate all manifests in loops/.")
    p_validate.add_argument("loops_dir", nargs="?", help="Override the loops directory.")

    p_init = sub.add_parser("init", help="Bootstrap the memory tree and check the source tree.")
    p_init.add_argument("--host", help="Host label for this deployment (informational).")
    p_init.add_argument(
        "--no-git", action="store_true", help="Do not `git init` the memory tree."
    )

    sub.add_parser("auth", help="Report credential status + guidance for the fleet's deps.")

    p_apply = sub.add_parser("apply", help="Validate the fleet + render/install systemd units.")
    p_apply.add_argument("loops_dir", nargs="?", help="Override the loops directory.")
    p_apply.add_argument("--dry-run", action="store_true", help="Validate + plan only; write nothing.")
    p_apply.add_argument("--out", help="Directory to render units into (default: <memory>/var/systemd).")
    p_apply.add_argument(
        "--install", action="store_true", help="Install + enable the rendered units via systemctl."
    )
    p_apply.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Validate manifests + DAG only; skip per-loop auth/tool preflight.",
    )
    p_apply.add_argument(
        "--render-invalid",
        action="store_true",
        help="Render diagnostic units even when preflight/env checks fail (default: don't write).",
    )
    p_apply.add_argument(
        "--allow-source-output",
        action="store_true",
        help="Permit --out to point inside the source tree (default: refused).",
    )

    sub.add_parser("list", help="List known loops.")
    sub.add_parser("fleet", help="Show all loops in a formatted table (schedule, last run, install state).")
    sub.add_parser("status", help="Show fleet status (last run per loop).")

    p_logs = sub.add_parser("logs", help="Print the last run log for a loop.")
    p_logs.add_argument("loop", help="Loop id.")

    p_deps = sub.add_parser("deps", help="Dependency helpers.")
    p_deps.add_argument("action", choices=["check"], help="check: probe runtimes/tools on PATH.")
    p_deps.add_argument(
        "--loop",
        help="Also run the selected loop's adapter preflight (auth/api/model/skill).",
    )

    args = parser.parse_args(argv)
    # Resolve config first so the source tree's own .env is loaded (not a random
    # .env in the invocation cwd), keeping credentials tied to LOOPCRAFT_SOURCE.
    config = LoopcraftConfig.load(args.source)
    load_dotenv(config.source_path / ".env")
    as_json = args.json

    if args.command == "run":
        return _cmd_run(config, args.loop, vendor=args.vendor, dry_run=args.dry_run, as_json=as_json)
    if args.command == "validate":
        return _cmd_validate(config, args.loops_dir, as_json=as_json)
    if args.command == "init":
        return _cmd_init(config, host=args.host, git=not args.no_git, as_json=as_json)
    if args.command == "auth":
        return _cmd_auth(config, as_json=as_json)
    if args.command == "apply":
        return _cmd_apply(
            config,
            args.loops_dir,
            dry_run=args.dry_run,
            out=args.out,
            install=args.install,
            skip_preflight=args.skip_preflight,
            render_invalid=args.render_invalid,
            allow_source_output=args.allow_source_output,
            as_json=as_json,
        )
    if args.command == "list":
        return _cmd_list(config, as_json=as_json)
    if args.command == "fleet":
        return _cmd_fleet(config, as_json=as_json)
    if args.command == "status":
        return _cmd_status(config, as_json=as_json)
    if args.command == "logs":
        return _cmd_logs(config, args.loop, as_json=as_json)
    if args.command == "deps":
        return _cmd_deps_check(config, loop_id=args.loop, as_json=as_json)
    return ExitCode.INVALID


def _lookup_loop(config: LoopcraftConfig, loop_id: str) -> LoopManifest | CommandOutcome:
    """Resolve and fully validate one loop by id for run/preflight commands.

    Shared by ``run`` and ``deps check --loop`` so both apply the identical
    ``find + parse/schema + manifest.validate()`` sequence — a loop that ``run``
    would reject can never be reported as ready by the dependency check.

    Returns:
        The manifest on success, or a :class:`CommandOutcome` describing the
        structured failure to emit.
    """
    try:
        manifest = find_manifest(config.loops_dir, loop_id)
    except ManifestError as exc:
        return CommandOutcome(
            rc=ExitCode.INVALID,
            data={"loop": loop_id, "error": str(exc)},
            lines=[f"error: {exc}"],
        )
    if manifest is None:
        message = f"loop '{loop_id}' not found in {config.loops_dir}"
        return CommandOutcome(
            rc=ExitCode.INVALID,
            data={"loop": loop_id, "error": message},
            lines=[f"error: {message}"],
        )
    problems = manifest.validate()
    if problems:
        return CommandOutcome(
            rc=ExitCode.INVALID,
            data={"loop": loop_id, "invalid_manifest": problems},
            lines=[f"invalid manifest: {problem}" for problem in problems],
        )
    return manifest


def _cmd_run(
    config: LoopcraftConfig,
    loop_id: str,
    *,
    vendor: str | None,
    dry_run: bool,
    as_json: bool,
) -> int:
    """Run one loop headless (or preflight it via ``--dry-run``)."""
    # Programmatic anti-recursion guard: the control plane marks the loop it is
    # executing via ACTIVE_LOOP_ENV, so a skill that (incorrectly) re-enters
    # `loopctl run` for its own loop is refused instead of recursing.
    active_loop = config.env_value(ACTIVE_LOOP_ENV)
    if active_loop == loop_id:
        message = (
            f"recursion guard: loop '{loop_id}' is already executing this loop "
            f"({ACTIVE_LOOP_ENV} is set); use the loop's direct CLI instead of "
            "re-entering the control plane"
        )
        return _emit(
            "run",
            as_json=as_json,
            ok=False,
            rc=ExitCode.INVALID,
            data={"loop": loop_id, "error": message},
            lines=[f"error: {message}"],
        )

    lookup = _lookup_loop(config, loop_id)
    if isinstance(lookup, CommandOutcome):
        return _emit(
            "run", as_json=as_json, ok=False, rc=lookup.rc, data=lookup.data, lines=lookup.lines
        )
    manifest = lookup

    effective_vendor = vendor or manifest.effective_vendor(config.default_vendor)
    try:
        runner = get_runner(effective_vendor)
    except ValueError as exc:
        return _emit(
            "run",
            as_json=as_json,
            ok=False,
            rc=ExitCode.INVALID,
            data={"loop": loop_id, "error": str(exc)},
            lines=[f"error: {exc}"],
        )

    preflight = _safe_preflight(runner, manifest, config, effective_vendor)

    if dry_run:
        return _run_dry_run(config, manifest, effective_vendor, preflight, as_json=as_json)

    return _run_execute(config, manifest, runner, effective_vendor, preflight, as_json=as_json)


def _safe_preflight(
    runner, manifest: LoopManifest, config: LoopcraftConfig, effective_vendor: str
) -> PreflightReport:
    """Run an adapter preflight, normalizing exceptions to a failing report.

    Shared by ``run`` and ``deps check --loop`` so a faulty adapter produces the
    same structured ``preflight raised <Type>: <message>`` problem in both
    commands instead of a traceback in one of them.
    """
    try:
        return runner.preflight(manifest, config)
    except Exception as exc:  # noqa: BLE001 — a faulty adapter must not escape as a traceback
        return PreflightReport(
            vendor=effective_vendor,
            ok=False,
            problems=[f"preflight raised {type(exc).__name__}: {exc}"],
        )


def _run_dry_run(
    config: LoopcraftConfig,
    manifest: LoopManifest,
    effective_vendor: str,
    preflight,
    *,
    as_json: bool,
) -> int:
    """Report the planned invocation and preflight result without executing."""
    resolved_outputs = [
        config.resolve_state_template(o, run_id="<run_id>", date="<date>")
        for o in manifest.outputs
    ]
    data = {
        "loop": manifest.id,
        "vendor": effective_vendor,
        "model": manifest.runtime.model,
        "resolved_outputs": [str(p) for p in resolved_outputs],
        "preflight": {"ok": preflight.ok, "problems": preflight.problems},
    }
    lines = [
        f"loop:    {manifest.id}",
        f"vendor:  {effective_vendor}  model: {manifest.runtime.model or '(default)'}",
        f"outputs: {[str(p) for p in resolved_outputs] or '(none)'}",
        f"preflight: {'OK' if preflight.ok else 'PROBLEMS'}",
        *[f"  - {problem}" for problem in preflight.problems],
    ]
    return _emit("run", as_json=as_json, ok=preflight.ok, rc=ExitCode.OK if preflight.ok else ExitCode.FAILURE, data=data, lines=lines)


def _run_execute(
    config: LoopcraftConfig,
    manifest: LoopManifest,
    runner,
    effective_vendor: str,
    preflight,
    *,
    as_json: bool,
) -> int:
    """Stage assets, execute the loop, and record the run."""
    store = Store(config)
    run_id = store.new_run_id()
    started = datetime.now(UTC)
    start_perf = time.perf_counter()
    resolved_outputs = [
        config.resolve_state_template(o, run_id=run_id, date=started.date().isoformat())
        for o in manifest.outputs
    ]

    if not preflight.ok:
        return _record_run_failure(
            store,
            manifest,
            effective_vendor,
            preflight.problems,
            run_id,
            started,
            start_perf,
            phase=FailurePhase.PREFLIGHT,
            as_json=as_json,
        )

    # One finalization boundary around the worktree lifecycle: staging errors and
    # unexpected adapter faults become normalized failed run records instead of
    # tracebacks, and retention pruning runs whenever a worktree was created —
    # including the failure paths. Process-control exceptions (KeyboardInterrupt,
    # SystemExit) are BaseException and deliberately not caught.
    worktree: Path | None = None
    try:
        try:
            worktree = worktree_dir(config, manifest.id, run_id)
            worktree.mkdir(parents=True, exist_ok=True)
            # Extra assets referenced by the effective content config (e.g. the
            # X following snapshot) are part of the staged bundle too.
            stage_loop_assets(
                config, manifest, worktree, extra_assets=content_assets(manifest, config)
            )
        except (StagingError, ValueError, OSError) as exc:
            return _record_run_failure(
                store,
                manifest,
                effective_vendor,
                [str(exc)],
                run_id,
                started,
                start_perf,
                phase=FailurePhase.STAGING,
                as_json=as_json,
            )
        ctx = RunContext(
            config=config,
            workdir=worktree,
            log_path=worktree / "run.log",
            resolved_outputs=resolved_outputs,
            # Hand the control-plane run id and resolved run date to any direct
            # CLI the loop invokes so its run-scoped history archives and dated
            # digests match the manifest's {{run_id}}/{{date}} outputs — even
            # when the run crosses 00:00 UTC. ACTIVE_LOOP_ENV marks this loop as
            # executing so a nested `loopctl run <same-loop>` is refused.
            env={
                RUN_ID_ENV: run_id,
                RUN_DATE_ENV: started.date().isoformat(),
                ACTIVE_LOOP_ENV: manifest.id,
            },
        )

        try:
            result = runner.run(manifest, ctx)
        except Exception as exc:  # noqa: BLE001 — the attempt must not vanish from history
            ctx.log_path.parent.mkdir(parents=True, exist_ok=True)
            ctx.log_path.write_text(traceback.format_exc(), encoding="utf-8")
            return _record_run_failure(
                store,
                manifest,
                effective_vendor,
                [f"runner raised {type(exc).__name__}: {exc}"],
                run_id,
                started,
                start_perf,
                phase=FailurePhase.EXECUTION,
                log_path=str(ctx.log_path),
                as_json=as_json,
            )
        ended = datetime.now(UTC)

        record = RunRecord(
            run_id=run_id,
            loop=manifest.id,
            vendor=effective_vendor,
            model=manifest.runtime.model,
            status=result.status,
            started_at=started.isoformat(),
            ended_at=ended.isoformat(),
            duration_s=round(time.perf_counter() - start_perf, 3),
            exit_code=result.exit_code,
            tokens=result.tokens,
            cost_usd=result.cost_usd,
            iterations=result.iterations,
            inputs=manifest.inputs,
            outputs=result.outputs,
            declared_outputs=manifest.outputs,
            log_path=str(result.log_path) if result.log_path else None,
            problems=result.problems,
        )
        record_path = store.record_run(record)
    finally:
        if worktree is not None:
            try:
                prune_loop_worktrees(config, manifest.id, keep_last=config.worktree_keep_last)
            except OSError as exc:
                print(f"warning: worktree pruning failed: {exc}", file=sys.stderr)

    ok = result.status == RunStatus.DONE
    data = {
        "loop": manifest.id,
        "status": result.status,
        "log": str(result.log_path) if result.log_path else None,
        "run_record": str(record_path),
        "outputs": result.outputs,
        "problems": result.problems,
    }
    lines = [
        f"loop:   {manifest.id}",
        f"status: {result.status}",
        f"log:    {result.log_path}",
        f"run record: {record_path}",
        *[f"output: {produced}" for produced in result.outputs],
    ]
    rc = _emit("run", as_json=as_json, ok=ok, rc=ExitCode.OK if ok else ExitCode.FAILURE, data=data, lines=lines)
    if not as_json:
        for problem in result.problems:
            print(f"  ! {problem}", file=sys.stderr)
    return rc


def _record_run_failure(
    store: Store,
    manifest: LoopManifest,
    effective_vendor: str,
    problems: list[str],
    run_id: str,
    started: datetime,
    start_perf: float,
    *,
    phase: FailurePhase,
    as_json: bool,
    log_path: str | None = None,
) -> int:
    """Persist a run record for a failed attempt and report it.

    No execution completed, so ``outputs`` (files actually produced) is empty;
    the manifest's declared contract is preserved separately in
    ``declared_outputs`` so a failed run never claims false provenance.

    Args:
        phase: Which :class:`FailurePhase` the run failed in.
        log_path: Optional log written for the failure (e.g. a traceback).
    """
    record = RunRecord(
        run_id=run_id,
        loop=manifest.id,
        vendor=effective_vendor,
        model=manifest.runtime.model,
        status=RunStatus.FAILED,
        started_at=started.isoformat(),
        ended_at=datetime.now(UTC).isoformat(),
        duration_s=round(time.perf_counter() - start_perf, 3),
        inputs=manifest.inputs,
        outputs=[],
        declared_outputs=manifest.outputs,
        log_path=log_path,
        problems=problems,
    )
    path = store.record_run(record)
    data = {
        "loop": manifest.id,
        "status": RunStatus.FAILED,
        "phase": phase,
        "problems": problems,
        "log": log_path,
        "run_record": str(path),
    }
    if not as_json:
        print(f"{phase} failed; loop not run:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(f"run record: {path}", file=sys.stderr)
    return _emit("run", as_json=as_json, ok=False, rc=ExitCode.FAILURE, data=data, lines=[])


def _cmd_validate(config: LoopcraftConfig, loops_dir: str | None, *, as_json: bool) -> int:
    """Validate every manifest in the loops directory."""
    target = Path(loops_dir) if loops_dir else config.loops_dir
    catalog = load_all(target)
    manifests, problems = catalog.manifests, catalog.problems
    ok = catalog.ok
    data = {
        "loops_dir": str(target),
        "validated": len(manifests),
        "ok": ok,
        "problems": problems,
    }
    lines = [f"validated {len(manifests)} manifest(s) in {target}"]
    if ok:
        lines.append("all manifests valid")
    rc = _emit("validate", as_json=as_json, ok=ok, rc=ExitCode.OK if ok else ExitCode.FAILURE, data=data, lines=lines)
    if not as_json and problems:
        for problem in problems:
            print(f"  FAIL {problem}", file=sys.stderr)
    return rc


def _cmd_init(config: LoopcraftConfig, *, host: str | None, git: bool, as_json: bool) -> int:
    """Bootstrap the memory tree and confirm the source tree is usable.

    Creates the ledger, run-record, and artifact directories in the memory tree
    (idempotent), optionally initializes it as a git repo, and reports whether
    the source tree has the expected ``loops/`` directory. Never writes to the
    source tree.
    """
    created: list[str] = []
    for directory in (
        config.ledger_dir,
        config.runs_dir,
        config.artifacts_dir,
        config.systemd_stage_dir,
    ):
        if not directory.exists():
            created.append(str(directory))
        directory.mkdir(parents=True, exist_ok=True)

    git_status = "skipped"
    if git:
        git_status = _git_init_memory(config.memory_path)

    source_ok = config.loops_dir.is_dir()
    ok = source_ok and not git_status.startswith("error")
    data = {
        "host": host or config.host,
        "source_path": str(config.source_path),
        "memory_path": str(config.memory_path),
        "created": created,
        "git": git_status,
        "source_ok": source_ok,
        "ok": ok,
    }
    lines = [
        f"host:   {host or config.host}",
        f"source: {config.source_path} ({'ok' if source_ok else 'MISSING loops/'})",
        f"memory: {config.memory_path}",
        f"git:    {git_status}",
    ]
    lines += [f"created {path}" for path in created] or ["memory tree already initialized"]
    if not source_ok:
        lines.append(f"error: no loops/ directory under {config.source_path}")
    return _emit(
        "init",
        as_json=as_json,
        ok=ok,
        rc=ExitCode.OK if ok else ExitCode.FAILURE,
        data=data,
        lines=lines,
    )


def _git_init_memory(memory_path: Path) -> str:
    """Best-effort ``git init`` of the memory tree; return a status string.

    The memory tree is the git-versioned ledger, so a fresh host should have it
    under version control. Returns ``"already a repo"``, ``"initialized"``,
    ``"unavailable (git not on PATH)"``, or an ``"error: ..."`` string — a git
    failure is reported, never raised, so init stays best-effort.
    """
    if (memory_path / ".git").exists():
        return "already a repo"
    if shutil.which("git") is None:
        return "unavailable (git not on PATH)"
    try:
        completed = subprocess.run(
            ["git", "init"],
            cwd=str(memory_path),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"error: {exc}"
    return "initialized" if completed.returncode == 0 else f"error: {completed.stderr.strip()}"


def _cmd_auth(config: LoopcraftConfig, *, as_json: bool) -> int:
    """Report credential status and guidance for every declared fleet dependency.

    Aggregates the ``auth`` bundles, ``apis``, and ``env`` vars declared across
    all loadable loops, probes each once (read-only), and reports which are
    satisfied on this host and how to fix the ones that are not. This is the
    guided, non-interactive credential check the design's ``loopctl auth`` step
    performs before ``apply``.
    """
    catalog = load_all(config.loops_dir)
    auth_bundles: set[str] = set()
    apis: set[str] = set()
    env_vars: set[str] = set()
    for manifest in catalog.manifests:
        auth_bundles.update(manifest.depends_on.auth)
        apis.update(manifest.depends_on.apis)
        env_vars.update(manifest.depends_on.env)

    # A scheduled service reads its credentials from scheduler.environment_file,
    # not the operator's .env/shell, so `auth` reports the *scheduled* credential
    # model — the same one apply's preflight enforces. Probes and env checks run
    # against a scheduled config; the file's own health is surfaced too.
    scheduled = config.for_scheduled_preflight()
    _, env_file_problems = environment_file_health(config)

    items: list[dict[str, Any]] = []
    for bundle in sorted(auth_bundles):
        items.append(_auth_item("auth", bundle, _probe_auth_bundle(scheduled, bundle), AUTH_GUIDANCE))
    for api in sorted(apis):
        items.append(_auth_item("api", api, _probe_declared_api(scheduled, api), API_GUIDANCE))
    for var in sorted(env_vars):
        problem = None if scheduled.env_value(var) else f"env var not in scheduled environment: {var}"
        items.append(_auth_item("env", var, problem, {}))
    if config.scheduler.environment_file:
        problem = env_file_problems[0] if env_file_problems else None
        items.append(
            _auth_item("env-file", config.scheduler.environment_file, problem, {})
        )

    missing = [item for item in items if not item["ok"]]
    ok = not missing
    lines = [f"{'[ok ]' if item['ok'] else '[MISS]'} {item['kind']}:{item['name']}" for item in items]
    lines += [f"       ^ {item['problem']} — {item['guidance']}" for item in missing]
    if not items:
        lines = ["no declared auth/api/env dependencies across the fleet"]
    return _emit(
        "auth",
        as_json=as_json,
        ok=ok,
        rc=ExitCode.OK if ok else ExitCode.FAILURE,
        data={"items": items, "missing": [item["name"] for item in missing]},
        lines=lines,
    )


def _auth_item(kind: str, name: str, problem: str | None, guidance: dict[str, str]) -> dict[str, Any]:
    """Build one auth-report item for a probed dependency."""
    return {
        "kind": kind,
        "name": name,
        "ok": problem is None,
        "problem": problem,
        "guidance": guidance.get(name, "configure this dependency on the host"),
    }


def _probe_auth_bundle(config: LoopcraftConfig, bundle: str) -> str | None:
    """Probe one declared auth bundle; return a problem string or None."""
    probe = AUTH_PROBES.get(bundle)
    if probe is None:
        return f"no probe for auth bundle '{bundle}'"
    return probe(config)


def _probe_declared_api(config: LoopcraftConfig, api: str) -> str | None:
    """Probe one declared API; return a problem string or None."""
    probe = API_PROBES.get(api)
    if probe is None:
        return f"no probe for api '{api}'"
    return probe(config)


def _cmd_apply(
    config: LoopcraftConfig,
    loops_dir: str | None,
    *,
    dry_run: bool,
    out: str | None,
    install: bool,
    skip_preflight: bool,
    render_invalid: bool,
    allow_source_output: bool,
    as_json: bool,
) -> int:
    """Validate the fleet, render systemd units, and optionally install them.

    The full pre-deploy check runs first (manifest schema, cross-loop DAG, the
    scheduled environment, and — unless ``--skip-preflight`` — each loop's
    auth/tool preflight), so an unmet dependency is reported here at ``apply``,
    not at runtime. Default ``apply`` is side-effect-free unless the plan is
    fully clean: with ``--dry-run`` nothing is written; a plan with unmet
    dependencies writes nothing unless ``--render-invalid`` is given (diagnostic
    rendering); a clean plan writes the staging units, and ``--install``
    additionally enables them via systemctl.
    """
    target = Path(loops_dir) if loops_dir else None
    plan = plan_deployment(config, loops_dir=target, run_preflight=not skip_preflight)
    out_dir = Path(out) if out else config.systemd_stage_dir

    # Generated units are build/deploy artifacts; refuse to write them into the
    # source tree unless explicitly allowed (source/memory separation).
    if out and not allow_source_output:
        problem = _source_output_problem(config, out_dir)
        if problem:
            return _emit(
                "apply",
                as_json=as_json,
                ok=False,
                rc=ExitCode.INVALID,
                data={"loops_dir": plan.loops_dir, "error": problem},
                lines=[f"error: {problem}"],
            )

    planned_units = [unit.filename for lu in plan.units for unit in lu.units]
    data: dict[str, Any] = {
        "loops_dir": plan.loops_dir,
        "validated": len(plan.units),
        "ok": plan.ok,
        "preflight_ran": plan.preflight_ran,
        "manifest_problems": plan.manifest_problems,
        "render_problems": plan.render_problems,
        "env_problems": plan.env_problems,
        "preflight_problems": plan.preflight_problems,
        "triggers": [{"loop": lu.loop, "trigger": lu.trigger} for lu in plan.units],
    }
    lines = [
        f"planned {len(plan.units)} loop(s) from {plan.loops_dir}",
        *[f"  {lu.loop}: {lu.trigger}" for lu in plan.units],
        *_env_file_guidance(config),
    ]

    if dry_run:
        data["planned_units"] = planned_units
        lines.append(f"dry run: {len(planned_units)} unit(s) would be written (nothing written)")
        rc = _apply_emit(plan, data, lines, as_json=as_json)
        # Like every other failing path, surface *why* a dry run is nonzero
        # (e.g. an unresolvable loopctl_bin) instead of only the unit count.
        _print_apply_problems(plan, as_json=as_json)
        return rc

    if not plan.renderable:
        # Structural manifest or render problems: refuse to write partial units.
        lines.append("not rendered: fix the manifest/render problems above")
        rc = _apply_emit(plan, data, lines, as_json=as_json)
        _print_apply_problems(plan, as_json=as_json)
        return rc

    # Default apply is side-effect-free unless the plan is fully clean. A plan
    # blocked only by unmet dependencies (preflight/env) renders diagnostics
    # solely on explicit request, so `fleet` never reports `staged` for a loop
    # whose deployment was rejected.
    if not plan.ok and not render_invalid:
        data["written"] = []
        lines.append(
            "not written: unmet dependencies block deployment "
            "(fix the problems above, or use --render-invalid to render diagnostics)"
        )
        rc = _apply_emit(plan, data, lines, as_json=as_json)
        _print_apply_problems(plan, as_json=as_json)
        return rc

    written = write_units(plan, out_dir)
    data["out_dir"] = str(out_dir)
    data["written"] = [str(p) for p in written]
    label = "rendered" if plan.ok else "rendered (diagnostic; deployment still blocked)"
    lines.append(f"{label} {len(written)} unit(s) into {out_dir}")

    if install:
        rc = _apply_install(config, plan, data, lines, as_json=as_json)
        _print_apply_problems(plan, as_json=as_json)
        return rc

    rc = _apply_emit(plan, data, lines, as_json=as_json)
    _print_apply_problems(plan, as_json=as_json)
    return rc


def _source_output_problem(config: LoopcraftConfig, out_dir: Path) -> str | None:
    """Return a problem if ``out_dir`` would write generated units into source.

    Uses lexical and resolved containment so neither a direct source-tree path
    nor a symlink into it slips through.
    """
    source = config.source_path
    try:
        Path(os.path.normpath(out_dir.expanduser())).relative_to(os.path.normpath(source))
        lexical_under = True
    except ValueError:
        lexical_under = False
    resolved_under = True
    try:
        assert_under(source, out_dir, label="apply --out")
    except ValueError:
        resolved_under = False
    if lexical_under or resolved_under:
        return (
            f"--out '{out_dir}' is inside the source tree ({source}); generated "
            "units belong under the memory tree (use --allow-source-output to override)"
        )
    return None


def _env_file_guidance(config: LoopcraftConfig) -> list[str]:
    """Return human guidance about the scheduled secrets file, if relevant.

    For system scope with a ``User=`` and a configured ``environment_file``,
    readability by that user cannot be verified portably, so we surface an
    explicit reminder rather than a false pass.
    """
    scheduler = config.scheduler
    if (
        scheduler.scope == SystemdScope.SYSTEM
        and scheduler.user
        and scheduler.environment_file
    ):
        return [
            f"note: ensure user '{scheduler.user}' can read "
            f"{scheduler.environment_file} (the service runs as that user)"
        ]
    return []


def _apply_install(
    config: LoopcraftConfig,
    plan: DeploymentPlan,
    data: dict[str, Any],
    lines: list[str],
    *,
    as_json: bool,
) -> int:
    """Install rendered units when the plan is fully clean; report the result."""
    if not plan.ok:
        lines.append("refusing --install: the plan has unmet dependencies (see problems)")
        data["installed"] = False
        return _emit("apply", as_json=as_json, ok=False, rc=ExitCode.FAILURE, data=data, lines=lines)

    result = install_units(config, plan)
    data["install"] = result.model_dump()
    data["unit_dir"] = str(systemd_unit_dir(config))
    if result.ok:
        lines.append(f"installed {len(result.installed)} unit(s); enabled {result.enabled}")
    else:
        lines.append("install had problems:")
        lines += [f"  - {problem}" for problem in result.problems]
    ok = plan.ok and result.ok
    return _emit("apply", as_json=as_json, ok=ok, rc=ExitCode.OK if ok else ExitCode.FAILURE, data=data, lines=lines)


def _apply_emit(plan: DeploymentPlan, data: dict[str, Any], lines: list[str], *, as_json: bool) -> int:
    """Emit an apply outcome whose exit code reflects the plan's overall status."""
    return _emit(
        "apply",
        as_json=as_json,
        ok=plan.ok,
        rc=ExitCode.OK if plan.ok else ExitCode.FAILURE,
        data=data,
        lines=lines,
    )


def _print_apply_problems(plan: DeploymentPlan, *, as_json: bool) -> None:
    """Print each blocking problem to stderr in human mode."""
    if as_json:
        return
    for problem in plan.problems:
        print(f"  FAIL {problem}", file=sys.stderr)


def _cmd_list(config: LoopcraftConfig, *, as_json: bool) -> int:
    """List known loops with tier and effective vendor.

    Manifest problems (unparseable or invalid manifests) degrade the result:
    every loadable loop is still listed as partial data, but the problems are
    reported and the exit code is nonzero so a broken manifest can never
    silently vanish from the fleet view.
    """
    catalog = load_all(config.loops_dir)
    manifests, problems = catalog.manifests, catalog.problems
    ok = catalog.ok
    loops = [
        {
            "id": m.id,
            "tier": str(m.tier),
            "vendor": m.effective_vendor(config.default_vendor),
            "name": m.name,
        }
        for m in manifests
    ]
    if not manifests:
        lines = [f"no loops found in {config.loops_dir}"]
    else:
        lines = [
            f"{m.id:24} tier={m.tier:8} vendor={m.effective_vendor(config.default_vendor):7} {m.name}"
            for m in manifests
        ]
    rc = _emit(
        "list",
        as_json=as_json,
        ok=ok,
        rc=ExitCode.OK if ok else ExitCode.FAILURE,
        data={"loops": loops, "problems": problems},
        lines=lines,
    )
    if not as_json:
        for problem in problems:
            print(f"  ! {problem}", file=sys.stderr)
    return rc


#: Column headers for the ``fleet`` table, in display order.
_FLEET_COLUMNS = ["LOOP", "NAME", "TIER", "VENDOR", "TRIGGER", "LAST RUN", "INSTALLED"]


def _loop_trigger(manifest: LoopManifest) -> str:
    """Return a compact trigger summary for the fleet table.

    Cron loops show their cron expression (what the manifest declares and the
    rendered ``OnCalendar`` derives from); other cadences show their type.
    """
    if manifest.cadence.type == CadenceType.CRON and manifest.cadence.at:
        return manifest.cadence.at
    return str(manifest.cadence.type)


def _install_state(config: LoopcraftConfig, loop_id: str) -> str:
    """Report whether a loop's systemd units are installed, staged, or absent.

    ``installed`` means units exist in the configured systemd unit directory;
    ``staged`` means they were only rendered into the memory-tree staging dir by
    ``apply`` (not yet installed); ``no`` means neither is present.
    """
    prefix = config.scheduler.unit_prefix
    candidates = (
        ("installed", systemd_unit_dir(config)),
        ("staged", config.systemd_stage_dir),
    )
    for label, directory in candidates:
        try:
            if directory.is_dir() and any(directory.glob(f"{prefix}{loop_id}.*")):
                return label
        except OSError:
            continue
    return "no"


def _cmd_fleet(config: LoopcraftConfig, *, as_json: bool) -> int:
    """Show every known loop in a formatted table.

    Combines each manifest's identity/cadence with its last run (from the store)
    and its systemd install state (from the unit/staging directories). Like
    ``list``/``status``, manifest problems degrade the result instead of hiding
    it: loadable loops are still tabulated, problems are surfaced, and the exit
    code is nonzero when any manifest is broken.
    """
    catalog = load_all(config.loops_dir)
    manifests, problems = catalog.manifests, catalog.problems
    ok = catalog.ok
    store = Store(config)

    loops: list[dict[str, Any]] = []
    rows: list[list[str]] = []
    for manifest in manifests:
        latest = store.latest_run(manifest.id)
        vendor = manifest.effective_vendor(config.default_vendor)
        trigger = _loop_trigger(manifest)
        installed = _install_state(config, manifest.id)
        last_run = f"{latest.status} {latest.started_at[:10]}" if latest else "never"
        loops.append(
            {
                "id": manifest.id,
                "name": manifest.name,
                "tier": str(manifest.tier),
                "vendor": vendor,
                "cadence": str(manifest.cadence.type),
                "trigger": trigger,
                "last_status": latest.status if latest else None,
                "last_started_at": latest.started_at if latest else None,
                "installed": installed,
            }
        )
        rows.append([manifest.id, manifest.name, str(manifest.tier), vendor, trigger, last_run, installed])

    if manifests:
        lines = render_table(_FLEET_COLUMNS, rows)
    else:
        lines = [f"no loops found in {config.loops_dir}"]
    rc = _emit(
        "fleet",
        as_json=as_json,
        ok=ok,
        rc=ExitCode.OK if ok else ExitCode.FAILURE,
        data={"loops": loops, "problems": problems},
        lines=lines,
    )
    if not as_json:
        for problem in problems:
            print(f"  ! {problem}", file=sys.stderr)
    return rc


def _cmd_status(config: LoopcraftConfig, *, as_json: bool) -> int:
    """Show the last run per loop.

    Like ``list``, manifest problems degrade the result instead of hiding it:
    loadable loops are still reported, problems are surfaced, and the exit code
    is nonzero — the fleet view must stay trustworthy exactly when configuration
    is broken.
    """
    catalog = load_all(config.loops_dir)
    manifests, problems = catalog.manifests, catalog.problems
    ok = catalog.ok
    store = Store(config)
    loops: list[dict[str, Any]] = []
    lines: list[str] = []
    if not manifests:
        lines.append(f"no loops found in {config.loops_dir}")
    for manifest in manifests:
        latest = store.latest_run(manifest.id)
        loops.append(
            {
                "id": manifest.id,
                "last_status": latest.status if latest else None,
                "last_started_at": latest.started_at if latest else None,
            }
        )
        summary = f"{latest.status} @ {latest.started_at}" if latest else "never run"
        lines.append(f"{manifest.id:24} {summary}")
    rc = _emit(
        "status",
        as_json=as_json,
        ok=ok,
        rc=ExitCode.OK if ok else ExitCode.FAILURE,
        data={"loops": loops, "problems": problems},
        lines=lines,
    )
    if not as_json:
        for problem in problems:
            print(f"  ! {problem}", file=sys.stderr)
    return rc


def _cmd_logs(config: LoopcraftConfig, loop_id: str, *, as_json: bool) -> int:
    """Print the last run log for a loop."""
    store = Store(config)
    latest = store.latest_run(loop_id)
    if latest is None:
        return _emit(
            "logs",
            as_json=as_json,
            ok=False,
            rc=ExitCode.FAILURE,
            data={"loop": loop_id, "error": "no runs recorded"},
            lines=[f"no runs recorded for '{loop_id}'"],
        )
    if not latest.log_path or not Path(latest.log_path).exists():
        return _emit(
            "logs",
            as_json=as_json,
            ok=False,
            rc=ExitCode.FAILURE,
            data={"loop": loop_id, "run_id": latest.run_id, "error": "no log on disk"},
            lines=[f"run {latest.run_id} has no log on disk"],
        )
    # Run records are hand-editable ledger files, so `logs` must not become an
    # arbitrary file reader: the log must live under the loopcraft log root.
    log_root = worktrees_root(config)
    try:
        assert_under(log_root, Path(latest.log_path), label="run log")
    except ValueError:
        return _emit(
            "logs",
            as_json=as_json,
            ok=False,
            rc=ExitCode.INVALID,
            data={
                "loop": loop_id,
                "run_id": latest.run_id,
                "error": f"log path outside allowed root ({log_root}): {latest.log_path}",
            },
            lines=[f"refusing to read log outside {log_root}: {latest.log_path}"],
        )
    log_text = Path(latest.log_path).read_text(encoding="utf-8")
    return _emit(
        "logs",
        as_json=as_json,
        ok=True,
        rc=ExitCode.OK,
        data={"loop": loop_id, "run_id": latest.run_id, "log_path": latest.log_path, "log": log_text},
        lines=[log_text],
    )


def _probe_dependency_table(table: dict[str, str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Probe a name->binary table on PATH; return (per-dep records, missing names)."""
    records: list[dict[str, Any]] = []
    missing: list[str] = []
    for name, binary in table.items():
        found = binary if binary.startswith("/") else shutil.which(binary)
        records.append({"name": name, "binary": binary, "found": bool(found)})
        if not found:
            missing.append(name)
    return records, missing


def _cmd_deps_check(config: LoopcraftConfig, *, loop_id: str | None = None, as_json: bool = False) -> int:
    """Check dependencies, or (with ``--loop``) only that loop's preflight.

    Without ``--loop``: required M1 binaries are probed and a missing one fails
    the check, while optional/future-runtime binaries are reported but never fail
    it. With ``--loop <id>``: only the selected loop's adapter preflight runs, so
    the check reflects exactly that loop's declared runtime and dependencies.
    """
    if loop_id:
        outcome = _preflight_loop(config, loop_id)
        return _emit(
            "deps.check",
            as_json=as_json,
            ok=outcome.rc == ExitCode.OK,
            rc=outcome.rc,
            data={"loop": loop_id, "preflight": outcome.data},
            lines=[line.lstrip("\n") for line in outcome.lines],
        )

    required, missing = _probe_dependency_table(config.dependencies)
    optional, optional_missing = _probe_dependency_table(config.optional_dependencies)
    rc = ExitCode.FAILURE if missing else ExitCode.OK

    lines = [f"[{'ok ' if dep['found'] else 'MISSING'}] {dep['name']}" for dep in required]
    lines += [
        f"[{'ok ' if dep['found'] else 'optional'}] {dep['name']} (future runtime)"
        for dep in optional
    ]

    data = {
        "dependencies": required,
        "missing": missing,
        "optional_dependencies": optional,
        "optional_missing": optional_missing,
    }
    return _emit("deps.check", as_json=as_json, ok=rc == ExitCode.OK, rc=rc, data=data, lines=lines)


def _preflight_loop(config: LoopcraftConfig, loop_id: str) -> CommandOutcome:
    """Run a loop's adapter preflight; return a structured command outcome.

    Uses the same lookup-and-validate sequence as ``run``, so a manifest that
    ``run`` would reject (schema or semantic validation) reports the identical
    structured failure here instead of an adapter ``OK``.
    """
    lookup = _lookup_loop(config, loop_id)
    if isinstance(lookup, CommandOutcome):
        return lookup
    manifest = lookup
    vendor = manifest.effective_vendor(config.default_vendor)
    try:
        runner = get_runner(vendor)
    except ValueError as exc:
        return CommandOutcome(
            rc=ExitCode.INVALID,
            data={"loop": loop_id, "error": str(exc)},
            lines=[f"error: {exc}"],
        )
    report = _safe_preflight(runner, manifest, config, vendor)
    lines = [
        f"\npreflight {loop_id} ({vendor}): {'OK' if report.ok else 'PROBLEMS'}",
        *[f"  - {problem}" for problem in report.problems],
    ]
    data = {"loop": loop_id, "vendor": vendor, "ok": report.ok, "problems": report.problems}
    return CommandOutcome(
        rc=ExitCode.OK if report.ok else ExitCode.FAILURE, data=data, lines=lines
    )


if __name__ == "__main__":
    raise SystemExit(main())
