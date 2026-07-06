"""``loopctl`` control-plane CLI.

Dispatches loop lifecycle commands (run/validate/apply/list/status/logs/deps)
against a resolved :class:`LoopcraftConfig`. Every command supports a consistent,
agent-friendly ``--json`` envelope (``{command, ok, exit_code, data}``) in
addition to human-readable text output.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
import traceback
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from loopcraft.cli_output import CommandOutcome, emit as _emit
from loopcraft.config import (
    ACTIVE_LOOP_ENV,
    RUN_DATE_ENV,
    RUN_ID_ENV,
    ExitCode,
    LoopcraftConfig,
)
from loopcraft.env import load_dotenv
from loopcraft.manifest import LoopManifest, ManifestError, find_manifest, load_all
from loopcraft.runners import RunContext, get_runner
from loopcraft.runners.base import PreflightReport, RunStatus
from loopcraft.runners.capabilities import content_assets
from loopcraft.store import RunRecord, Store
from loopcraft.worktree import (
    StagingError,
    prune_loop_worktrees,
    stage_loop_assets,
    worktree_dir,
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

    p_apply = sub.add_parser("apply", help="Validate manifests (deployment lands in M2).")
    p_apply.add_argument("loops_dir", nargs="?", help="Override the loops directory.")
    p_apply.add_argument("--dry-run", action="store_true", help="Validate only.")

    sub.add_parser("list", help="List known loops.")
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
    load_dotenv()
    config = LoopcraftConfig.load(args.source)
    as_json = args.json

    if args.command == "run":
        return _cmd_run(config, args.loop, vendor=args.vendor, dry_run=args.dry_run, as_json=as_json)
    if args.command == "validate":
        return _cmd_validate(config, args.loops_dir, as_json=as_json)
    if args.command == "apply":
        return _cmd_apply(config, args.loops_dir, as_json=as_json)
    if args.command == "list":
        return _cmd_list(config, as_json=as_json)
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


def _cmd_apply(config: LoopcraftConfig, loops_dir: str | None, *, as_json: bool) -> int:
    """Validate manifests; scheduler deployment lands in M2."""
    target = Path(loops_dir) if loops_dir else config.loops_dir
    catalog = load_all(target)
    manifests, problems = catalog.manifests, catalog.problems
    ok = catalog.ok
    note = "scheduler deployment (systemd units) lands in M2; M1 validates only."
    data = {
        "loops_dir": str(target),
        "validated": len(manifests),
        "ok": ok,
        "problems": problems,
        "note": note,
    }
    lines = [f"validated {len(manifests)} manifest(s) in {target}"]
    if ok:
        lines.append("all manifests valid")
        lines.append(f"note: {note}")
    rc = _emit("apply", as_json=as_json, ok=ok, rc=ExitCode.OK if ok else ExitCode.FAILURE, data=data, lines=lines)
    if not as_json and problems:
        for problem in problems:
            print(f"  FAIL {problem}", file=sys.stderr)
    return rc


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
